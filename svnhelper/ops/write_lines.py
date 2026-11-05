"""Ветки и версии: создать на сервере и переключить папку (Task AG.5, разрешение `branches`).

Создание это серверное копирование текущей линии проекта (ничего не удаляется и не перезаписывается: занятое имя
отказ). Переключение требует чистой папки: ни несохранённых правок, ни занятых вами файлов. Версии (теги) помощник
создаёт, но не меняет и не пишет в них.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .. import audit as junk_audit
from .. import server
from ..svn_core import CheckoutProgress, ErrorKind, SvnError
from .context import OpsContext, deadline
from .errors import OpsError, invalid_args
from .projects import KIND_PROJECT, display_path
from .result import ErrorInfo, Result, clean_name
from .writing import Prepared, WriteSpec, fresh_status, mark_comment, project_target, register, signature_of

log = logging.getLogger("svnhelper.ops")

_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*")
BRANCH_KINDS = {"branch": "branch", "version": "tag", "tag": "tag"}
SWITCH_KINDS = {"main": "trunk", "branch": "branch", "version": "tag", "tag": "tag"}
LINE_WORD = {"branch": "ветку", "tag": "версию"}
LINE_WORD_NOM = {"branch": "ветка", "tag": "версия"}
WC_LOCKED = "Папка проекта осталась в недоделанном состоянии после прошлого сбоя."
WC_LOCKED_STEP = "Откройте SVN-lite: программа починит папку. Помощник этого не делает."


def _line_context(probe, what: str) -> tuple:
    """(префикс адреса сервера, сегменты проекта, текущая линия) по адресу рабочей копии."""
    info = probe.info
    found = server.line_from_url(info.url, info.root) if info else None
    prefix = _PREFIX_RE.match(info.url.strip()) if info else None
    if not found or not prefix:
        raise OpsError("wrong_kind", 7, f"У этого проекта нет основной линии, веток и версий: {what} невозможно.")
    return prefix.group(0), found[0], found[1]


def _name(raw: Optional[str]) -> str:
    try:
        return server.validate_line_name(raw or "")
    except ValueError as exc:
        raise invalid_args(str(exc)) from exc


def prepare_branch(ctx: OpsContext, project: str, name: str, kind: str = "branch",
                   message: Optional[str] = None) -> Prepared:
    line_kind = BRANCH_KINDS.get((kind or "branch").strip().lower())
    if line_kind is None:
        raise invalid_args("Что создать: «branch» (ветка) или «version» (версия).")
    ref, probe = project_target(ctx, project, "branches", (KIND_PROJECT,), "Ветки бывают только у проектов с сервера.")
    name = _name(name)
    prefix, segments, current = _line_context(probe, "создание")
    new_line = server.Line(line_kind, name)
    src = server.build_url(prefix, [*segments, *current.segments()])
    dst = server.build_url(prefix, [*segments, *new_line.segments()])
    if ctx.client.url_exists(dst):
        raise OpsError("exists", 1, f"Такое название уже занято: «{clean_name(name)}».", "Выберите другое название.")
    unsaved = fresh_status(ctx, probe, False).tracked_changes
    final = mark_comment(ctx, (message or "").strip() or f"Создана {LINE_WORD_NOM[line_kind]} «{name}»")
    summary = (f"Создать {LINE_WORD[line_kind]} «{clean_name(name)}» в проекте «{probe.name}» из текущей линии "
               f"(сохранение № {probe.number}). Папка на компьютере не переключается.")
    warnings = [f"Несохранённые правки ({unsaved}) в {LINE_WORD[line_kind]} не войдут."] if unsaved else []
    return Prepared("branches", probe.path, summary, signature_of("branch", src, dst, probe.number, final),
                    {"kind": "version" if line_kind == "tag" else "branch", "name": name, "from_number": probe.number},
                    warnings=warnings, line="",
                    audit={"comment": final, "rev_before": probe.number, "files": [(new_line.label, "создана")]},
                    state={"probe": probe, "src": src, "dst": dst, "message": final, "line": new_line})


def execute_branch(ctx: OpsContext, prepared: Prepared, project: str, name: str, kind: str = "branch",
                   message: Optional[str] = None) -> Result:
    state = prepared.state
    ctx.client.copy_url(state["src"], state["dst"], state["message"], state["probe"].number)
    line = state["line"]
    return Result("branch_apply", data={"kind": "version" if line.kind == "tag" else "branch", "name": line.name,
                                        "from_number": state["probe"].number},
                  text=f"Создана {LINE_WORD_NOM[line.kind]} «{clean_name(line.name)}» в проекте "
                       f"«{state['probe'].name}».")


def prepare_switch(ctx: OpsContext, project: str, kind: str = "main", name: Optional[str] = None) -> Prepared:
    target_kind = SWITCH_KINDS.get((kind or "main").strip().lower())
    if target_kind is None:
        raise invalid_args("Куда переключить: «main» (основная), «branch» (ветка) или «version» (версия).")
    ref, probe = project_target(ctx, project, "branches", (KIND_PROJECT,), "Ветки бывают только у проектов с сервера.",
                                allow_tag=True)
    target = server.TRUNK_LINE if target_kind == "trunk" else server.Line(target_kind, _name(name))
    prefix, segments, current = _line_context(probe, "переключение")
    status = fresh_status(ctx, probe, False)
    if target == current and not status.switched:
        return Prepared("branches", probe.path, f"Проект «{probe.name}» уже на этой линии: {target.label}.",
                        signature_of("switch", probe.path, target.kind, target.name, "same"), {}, noop=True)
    url = server.build_url(prefix, [*segments, *target.segments()])
    if not ctx.client.url_exists(url):
        raise OpsError("not_found", 1, f"Такой линии нет на сервере: {clean_name(target.label)}.",
                       "Проверьте название или создайте её.")
    if status.conflicts or status.tracked_changes:
        raise OpsError("local_changes", 7, "В проекте есть несохранённые правки или конфликт: переключаться нельзя.",
                       "Сначала сохраните правки.", data={"changes": status.tracked_changes})
    if status.my_locks:
        files = [display_path(lock.path, probe.path) for lock in status.my_locks[:20]]
        raise OpsError("local_changes", 7, "Есть файлы, занятые вами: переключаться с ними нельзя.",
                       "Сначала освободите их.", data={"files": files}, untrusted_fields=["files[]"])
    summary = f"Переключить проект «{probe.name}» на линию: {clean_name(target.label)}."
    if target.kind == "tag":
        summary += " Версия открывается только для просмотра."
    return Prepared("branches", probe.path, summary, signature_of("switch", probe.path, probe.number, url),
                    {"target": target.label, "kind": target_kind}, line="",
                    audit={"rev_before": probe.number, "files": [(target.label, "переключение")]},
                    state={"probe": probe, "url": url, "target": target})


def execute_switch(ctx: OpsContext, prepared: Prepared, project: str, kind: str = "main",
                   name: Optional[str] = None) -> Result:
    state = prepared.state
    probe, path = state["probe"], state["probe"].path
    with deadline(ctx) as token:
        try:
            result = ctx.client.switch(path, state["url"], CheckoutProgress(), token)
        except SvnError as exc:
            if exc.kind is ErrorKind.OTHER and "E155004" in exc.codes:
                raise OpsError("wc_locked", 7, WC_LOCKED, WC_LOCKED_STEP) from exc
            raise
        rules = ctx.config.filter
        if rules.enabled and rules.skip_junk and result.added_paths:
            excluded = junk_audit.junk_among(result.added_paths, rules, path)
            if excluded:
                ctx.client.exclude_paths(path, excluded, token)
    ctx.audit.update(rev_after=result.revision)
    target = state["target"]
    data = {"target": target.label, "number": result.revision,
            "conflicts": [display_path(p, path) for p in result.conflicts][:50]}
    if result.conflicts:
        error = ErrorInfo("conflict", "После переключения есть конфликты.",
                          "Конфликты должен разобрать человек в программе SVN-lite.", False, 7,
                          "human_resolve_conflict")
        return Result.failure("switch_apply", error, data, untrusted_fields=["conflicts[]"])
    return Result("switch_apply", data=data, text=f"Проект «{probe.name}» переключён: {clean_name(target.label)}.")


register(WriteSpec("branch", prepare_branch, execute_branch))
register(WriteSpec("switch", prepare_switch, execute_switch))
