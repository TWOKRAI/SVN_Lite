"""Каркас операций записи (Task AG.5): план, токен, подтверждение, журнал.

Каждая запись описывается `WriteSpec` (проверить и составить план: `prepare`; выполнить: `execute`) и регистрируется в
реестре парой операций `<имя>_plan` (только чтение, ничего не меняет) и `<имя>_apply` (запись). Порядок:

    политика (режим, разрешение, папка) -> поиск проекта (строго) -> проверки -> план и токен     (`*_plan`)
    токен -> занять папку -> те же проверки заново -> сверка с планом -> политика с учётом лимита записей -> запись
    -> журнал действий -> сброс кэша состояния -> `agent/last_change.json`                          (`*_apply`)

Режимы (agent_policy): `write_confirm` требует токен плана, `write_full` его не требует (план составляется и
применяется за один вызов); лимиты, разрешения, журнал и проверки одинаковы. Папку на время записи занимает
`publish.mark_busy` (межпроцессный замок): занята другой программой - код 8, ничего не тронуто. Операции создания и
отправки проекта берут замок сами (`own_lock=True`).

Ответ плана: `data.plan_id` (токен), `data.requires_confirmation`, что именно будет сделано. Повторное применение того же
токена возвращает прежний ответ с `data.already_applied=true` и ничего не пишет.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .. import publish
from ..agent_policy import deny
from ..config import data_dir
from . import plan as plans
from . import projects as read_ops
from . import texts, texts_write
from .context import OpsContext
from .errors import OpsError
from .registry import REGISTRY, Operation, Param
from .resolve import ProjectRef, resolve_project
from .result import EXIT_NO_RIGHTS, ErrorInfo, Result

log = logging.getLogger("svnhelper.ops")

WRITE_TIMEOUT = 1800.0          # секунд на всю запись (получение большого проекта дольше обычного чтения)
LAST_CHANGE = "last_change.json"
PLAN_TEXT_HINT = "Для выполнения вызовите «{apply}» с токеном {token}."


@dataclass
class Prepared:
    """Итог проверок: что именно будет сделано. Строится заново и на плане, и на применении."""
    action: str                         # разрешение политики (agent_policy.ACTIONS)
    path: str                           # папка, которую затрагивает запись (занимается на время применения)
    summary: str                        # что будет сделано, словами сотрудника
    signature: str                      # отпечаток того, что будет сделано (файлы, номера): план и применение должны совпасть
    detail: dict = field(default_factory=dict)      # данные плана для ответа
    warnings: list = field(default_factory=list)
    untrusted: list = field(default_factory=list)
    noop: bool = False                  # делать нечего: токен не выдаётся, запись не считается
    line: str = ""                      # main | branch | tag (для политики)
    audit: dict = field(default_factory=dict)       # files, comment... для журнала действий
    state: Any = None                   # внутренние объекты для execute (в ответ не попадают)


@dataclass
class WriteSpec:
    name: str
    prepare: Callable[..., Prepared]                # (ctx, **params) -> Prepared
    execute: Callable[..., Result]                  # (ctx, prepared, **params) -> Result (data, text, warnings)
    own_lock: bool = False                          # папку занимает само ядро (создание, отправка)
    needs_svn: bool = True


def signature_of(*parts: Any) -> str:
    import hashlib
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# Общие проверки
# --------------------------------------------------------------------------

def check_policy(ctx: OpsContext, prepared: Prepared, consume: bool) -> None:
    """Политика для записи: режим, разрешение, папка, версия (тег), лимит записей в час (при consume=True считается)."""
    ctx.policy.check(prepared.action, prepared.path or None, base_dirs=ctx.project_dirs, line=prepared.line,
                     consume=consume)


def early_policy(ctx: OpsContext, action: str) -> None:
    """Отказ политики до поиска проекта: режим и разрешение (папка проверяется позже, когда проект известен)."""
    ctx.policy.check(action, None, base_dirs=ctx.project_dirs, consume=False)


def project_target(ctx: OpsContext, project: str, action: str, kinds: tuple, wrong_text: str = "",
                   allow_tag: bool = False) -> tuple:
    """(ref, probe) проекта для записи: строгий поиск, нужный вид папки, не версия (тег; allow_tag: переключение с версии
    разрешено). Занятость папки проверяет каркас."""
    early_policy(ctx, action)
    ref: ProjectRef = resolve_project(ctx, project, strict=True)
    probe = read_ops.probe_folder(ctx, ref)
    if probe.kind not in kinds:
        raise OpsError("wrong_kind", 7, wrong_text or texts.ERRORS["wrong_kind"][0],
                       data={"kind": probe.kind})
    if probe.line == "tag" and not allow_tag:
        raise OpsError("read_only_line", EXIT_NO_RIGHTS)
    ctx.policy.check(action, probe.path, base_dirs=ctx.project_dirs, line="" if allow_tag else probe.line, consume=False)
    return ref, probe


def fresh_status(ctx: OpsContext, probe: read_ops.Probe, server: bool):
    """Состояние папки прямо сейчас (без кэша): правки, новое на сервере (server=True), конфликты, занятые файлы."""
    from .context import deadline
    from .. import localrepo
    rules = localrepo.local_rules(ctx.config.filter) if probe.kind == read_ops.KIND_LOCAL_HISTORY else ctx.config.filter
    with deadline(ctx) as token:
        return ctx.client.status(probe.path, check_server=server, rules=rules, cancel=token)


def relative(path: str, root: str) -> str:
    return read_ops.display_path(path, root)


def pick_files(candidates: list, root: str, wanted: Optional[list], key: Callable[[Any], str]) -> list:
    """Из candidates выбирает по списку путей `wanted` (файл или папка внутри проекта); без списка - все. Путь, которого
    нет среди candidates: OpsError not_found_here со списком."""
    if not wanted:
        return list(candidates)
    shown = [(item, relative(key(item), root).casefold()) for item in candidates]
    chosen, missing = [], []
    for raw in wanted:
        rel = str(raw).replace("\\", "/").strip().strip("/").casefold()
        hits = [item for item, name in shown if name == rel or name.startswith(rel + "/")]
        if not rel or not hits:
            missing.append(str(raw))
        chosen.extend(hit for hit in hits if hit not in chosen)
    if missing:
        raise OpsError("not_found_here", 1, f"{texts.ERRORS['not_found_here'][0]} {', '.join(missing[:5])}",
                       texts.ERRORS["not_found_here"][1], data={"missing": missing[:20]})
    return chosen


def mark_comment(ctx: OpsContext, message: str) -> str:
    """Комментарий для сервера: с пометкой «по просьбе ИИ-помощника», если политика её требует."""
    message = message.strip()
    return f"{message} (по просьбе ИИ-помощника)" if getattr(ctx.policy, "mark_comments", True) else message


def check_comment(ctx: OpsContext, message: str) -> str:
    message = (message or "").strip()
    need = getattr(ctx.policy, "min_comment_chars", 10)
    if len(message) < need:
        deny("comment_short", need=need)
    return message


# --------------------------------------------------------------------------
# План и применение
# --------------------------------------------------------------------------

def _result_from_dict(tool: str, stored: dict) -> Result:
    error = stored.get("error")
    info = ErrorInfo(error["kind"], error.get("message", ""), error.get("next_step", ""), error.get("retryable", False),
                     error.get("exit_code", 1), error.get("next_action", "")) if error else None
    return Result(tool, ok=not error, data=dict(stored.get("data") or {}), text=stored.get("text", ""),
                  warnings=list(stored.get("warnings") or []), untrusted_fields=list(stored.get("untrusted_fields") or []),
                  error=info)


def _plan_func(spec: WriteSpec) -> Callable:
    def run_plan(ctx: OpsContext, **params) -> Result:
        prepared = spec.prepare(ctx, **params)
        check_policy(ctx, prepared, consume=False)
        if not spec.own_lock and prepared.path and publish.is_busy(prepared.path):
            from .. import folderlock
            raise folderlock.FolderBusy(prepared.path, folderlock.holder_info(prepared.path))
        confirm = bool(getattr(ctx.policy, "confirm", True))
        data = {"plan_id": "", "noop": prepared.noop, "requires_confirmation": confirm, **prepared.detail}
        text = prepared.summary
        if not prepared.noop:
            ttl = getattr(ctx.policy, "plan_ttl_minutes", 10)
            made = plans.create(spec.name, prepared.path, params, prepared.signature, prepared.summary, ttl,
                                ctx.client_name)
            data.update(plan_id=made.id, expires_in_minutes=ttl)
            text += " " + PLAN_TEXT_HINT.format(apply=spec.name + "_apply", token=made.id)
            ctx.audit.update(plan_id=made.id)
        ctx.audit.update(prepared.audit)
        return Result(spec.name + "_plan", data=data, text=text, warnings=list(prepared.warnings),
                      untrusted_fields=list(prepared.untrusted))
    return run_plan


def _apply_func(spec: WriteSpec) -> Callable:
    tool = spec.name + "_apply"

    def run_apply(ctx: OpsContext, **params) -> Result:
        token = params.pop("token", None)
        ctx.timeout = max(ctx.timeout, WRITE_TIMEOUT)
        ctx.started = time.monotonic()
        stored: Optional[plans.Plan] = None
        early_policy(ctx, texts_write_action(spec.name, params))
        if token:
            stored = _load_plan(spec, token, params)
            if stored.state == plans.APPLIED:
                ctx.audit.update(plan_id=stored.id)
                again = _result_from_dict(tool, stored.result or {})
                again.data["already_applied"] = True
                return again
            if stored.state != plans.PLANNED:
                deny("plan_used")
            if stored.expired:
                deny("plan_expired")
        elif getattr(ctx.policy, "confirm", True):
            deny("need_plan")
        guard = contextlib.nullcontext() if spec.own_lock else None
        path_hint = None
        if guard is None:
            path_hint = _early_path(spec, ctx, params)
            guard = publish.mark_busy(path_hint, "agent")
        with guard:
            return _locked_apply(spec, ctx, params, stored, tool)
    return run_apply


def texts_write_action(name: str, params: dict) -> str:
    """Разрешение политики, нужное операции, до разбора проекта (режим и разрешение проверяются раньше всего)."""
    if name == "save":
        return "save_local" if params.get("local") else "save_server"
    if name == "create_project":
        return "create_server" if params.get("storage") == "server" else "create_local"
    return {"get_new": "get_new", "lock": "lock", "unlock": "lock", "branch": "branches", "switch": "branches",
            "rollback": "rollback", "publish": "publish"}[name]


def _early_path(spec: WriteSpec, ctx: OpsContext, params: dict) -> str:
    """Папка, которую надо занять до проверок: проект ищется строго по названию или пути."""
    key = params.get("project") or params.get("folder") or ""
    return resolve_project(ctx, key, strict=True).path


def _load_plan(spec: WriteSpec, token: str, params: dict) -> plans.Plan:
    stored = plans.load(token)
    if stored is None or stored.op != spec.name:
        deny("plan_unknown")
    if stored.state == plans.APPLIED:
        return stored
    return stored if _same_call(stored, params) else _mismatch()


def _mismatch() -> plans.Plan:
    deny("plan_mismatch")
    raise AssertionError("unreachable")


def _same_call(stored: plans.Plan, params: dict) -> bool:
    return plans.fingerprint(stored.op, stored.path, params) == stored.fingerprint


def _locked_apply(spec: WriteSpec, ctx: OpsContext, params: dict, stored: Optional[plans.Plan], tool: str) -> Result:
    prepared = spec.prepare(ctx, **params)
    ctx.audit.update(prepared.audit)
    if stored is not None and stored.fingerprint != plans.fingerprint(spec.name, prepared.path, params):
        deny("plan_mismatch")
    if stored is not None and prepared.signature != stored.signature:
        deny("plan_stale")
    if prepared.noop:
        return Result(tool, data={"noop": True, **prepared.detail}, text=prepared.summary,
                      warnings=list(prepared.warnings), untrusted_fields=list(prepared.untrusted))
    op_id = uuid.uuid4().hex[:16]
    ctx.audit.update(op_id=op_id, plan_id=stored.id if stored else "")
    if stored is not None and plans.claim(stored.id) is None:
        deny("plan_used")
    try:
        check_policy(ctx, prepared, consume=True)
        result = spec.execute(ctx, prepared, **params)
    except BaseException as exc:
        if stored is not None:
            plans.finish(stored, failure=type(exc).__name__)
        raise
    result.tool = tool
    result.data.setdefault("op_id", op_id)
    result.untrusted_fields = list(result.untrusted_fields or prepared.untrusted)
    read_ops.clear_status_cache(prepared.path)
    _note_change(prepared, tool)
    if stored is not None:
        plans.finish(stored, result=result.to_dict())
    return result


def _note_change(prepared: Prepared, tool: str) -> None:
    """Окно узнаёт по этому файлу, что проект изменили снаружи (AG.0d, шаг 4). Сбой записи не мешает операции."""
    try:
        target = data_dir() / "agent" / LAST_CHANGE
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"ts": time.time(), "tool": tool, "path": prepared.path}, ensure_ascii=False)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        log.warning("Метка последнего изменения не записана", exc_info=True)


def register(spec: WriteSpec) -> WriteSpec:
    """Регистрирует `<имя>_plan` и `<имя>_apply` в реестре по описанию из texts_write.WRITE_OPERATIONS."""
    meta = texts_write.WRITE_OPERATIONS[spec.name]
    params = [Param(*item) for item in meta["params"]]
    needs = spec.needs_svn
    plan_op = Operation(spec.name + "_plan", meta["title_ru"] + texts_write.PLAN_SUFFIX_RU,
                        meta["description_ru"] + " " + texts_write.PLAN_NOTE_RU, list(meta["examples_ru"]),
                        list(params), True, needs, _plan_func(spec))
    apply_op = Operation(spec.name + "_apply", meta["title_ru"] + texts_write.APPLY_SUFFIX_RU,
                         meta["description_ru"] + " " + texts_write.APPLY_NOTE_RU, list(meta["examples_ru"]),
                         [*params, Param(*texts_write.TOKEN_PARAM)], False, needs, _apply_func(spec))
    REGISTRY[plan_op.name] = plan_op
    REGISTRY[apply_op.name] = apply_op
    return spec
