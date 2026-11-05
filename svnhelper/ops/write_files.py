"""Запись над файлами проекта: получить новое, сохранить (на сервер или у себя), занять и освободить файл (Task AG.5).

Каждая операция описана парой `prepare` (проверки, план) и `execute` (запись); каркас в `writing.py`. Ничего из
опасного не отдаётся: конфликты не разбираются, папка не чинится (`cleanup`), чужие занятые файлы не освобождаются.
Автоповторов после «на сервере новее» нет: помощник получает код 7 и сообщает человеку.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .. import audit as junk_audit
from ..messages import files_count
from ..svn_core import CheckoutProgress, ErrorKind, SvnError
from . import texts
from .context import OpsContext, deadline
from .errors import OpsError
from .projects import KIND_LOCAL_HISTORY, KIND_PROJECT, display_path
from .result import EXIT_NO_RIGHTS, ErrorInfo, Result, clean_name
from .writing import (Prepared, WriteSpec, check_comment, fresh_status, mark_comment, pick_files, project_target,
                      register, signature_of)

log = logging.getLogger("svnhelper.ops")

MAX_SHOWN = 50                      # файлов в ответе плана
LOCK_COMMENT = "Правка через SVN-lite (по просьбе ИИ-помощника)"
KIND_RU = texts.CHANGE_KIND_RU
_DELETE_KINDS = ("deleted", "missing")
_NO_LOCK_KINDS = ("added", "unversioned")


def _shown(paths: list) -> list:
    return [clean_name(p) for p in paths[:MAX_SHOWN]]


# --------------------------------------------------------------------------
# get_new
# --------------------------------------------------------------------------

def prepare_get_new(ctx: OpsContext, project: str) -> Prepared:
    ref, probe = project_target(ctx, project, "get_new", (KIND_PROJECT,),
                                "Получить новое можно только в проекте с сервера.")
    status = fresh_status(ctx, probe, True)
    if status.conflicts:
        raise OpsError("conflict", 7, data={"conflicts": status.conflicts})
    files = [display_path(p, probe.path) for p in status.incoming_files]
    count = max(len(files), status.incoming)
    audit = {"rev_before": probe.number}
    if not count:
        return Prepared("get_new", probe.path, f"В проекте «{probe.name}» нового на сервере нет.",
                        signature_of("get_new", probe.path, status.head), {"incoming": 0, "files": []}, noop=True,
                        line=probe.line, audit=audit)
    own = len(status.local_changes)
    summary = f"Получить новое в проекте «{probe.name}»: изменено на сервере {files_count(count)}."
    if own:
        summary += f" Ваши несохранённые правки ({files_count(own)}) останутся."
    return Prepared("get_new", probe.path, summary, signature_of("get_new", probe.path, status.head, sorted(files)),
                    {"incoming": count, "files": _shown(files), "local_changes": own, "head": status.head},
                    untrusted=["files[]"], line=probe.line, audit=audit, state=probe)


def execute_get_new(ctx: OpsContext, prepared: Prepared, project: str) -> Result:
    probe = prepared.state
    path = probe.path
    with deadline(ctx) as token:
        try:
            result = ctx.client.update(path, CheckoutProgress(), token)
        except SvnError as exc:
            if exc.kind is ErrorKind.OTHER and "E155004" in exc.codes:    # папка заблокирована прошлым сбоем
                raise OpsError("wc_locked", 7, "Папка проекта осталась в недоделанном состоянии после прошлого сбоя.",
                               "Откройте SVN-lite: программа починит папку. Помощник этого не делает.") from exc
            raise
        excluded: list = []
        rules = ctx.config.filter
        if rules.enabled and rules.skip_junk and result.added_paths:
            excluded = junk_audit.junk_among(result.added_paths, rules, path)
            if excluded:
                ctx.client.exclude_paths(path, excluded, token)
    ctx.audit.update(rev_after=result.revision)
    conflicts = [display_path(p, path) for p in result.conflicts]
    data = {"number": result.revision, "added": result.added, "updated": result.updated, "deleted": result.deleted,
            "junk_skipped": len(excluded), "conflicts": _shown(conflicts)}
    text = (f"Получено новое в проекте «{probe.name}»: {files_count(result.added + result.updated + result.deleted)}"
            f", сохранение № {result.revision}.")
    if conflicts:
        message = f"После получения нового есть конфликты: {files_count(len(conflicts))}."
        error = ErrorInfo("conflict", message, "Конфликты должен разобрать человек в программе SVN-lite.", False, 7,
                          "human_resolve_conflict")
        return Result.failure("get_new_apply", error, data, untrusted_fields=["conflicts[]"])
    return Result("get_new_apply", data=data, text=text, untrusted_fields=["conflicts[]"])


# --------------------------------------------------------------------------
# save
# --------------------------------------------------------------------------

def prepare_save(ctx: OpsContext, project: str, message: str, files: Optional[list] = None,
                 allow_deletes: bool = False, local: bool = False) -> Prepared:
    action = "save_local" if local else "save_server"
    wrong = ("У этого проекта нет истории на компьютере: он хранится на сервере."
             if local else "Этот проект хранится только у вас: сохраните его у себя или отправьте на сервер.")
    ref, probe = project_target(ctx, project, action, (KIND_LOCAL_HISTORY,) if local else (KIND_PROJECT,), wrong)
    text = check_comment(ctx, message)
    status = fresh_status(ctx, probe, server=not local)
    if status.conflicts:
        raise OpsError("conflict", 7, data={"conflicts": status.conflicts})
    incoming = max(len(status.incoming_files), status.incoming) if not local else 0
    if incoming:
        raise OpsError("incoming_first", 7, data={"incoming": incoming})
    audit = {"rev_before": status.head or probe.number}
    candidates = [c for c in status.local_changes if c.kind != "conflicted"]
    chosen = pick_files(candidates, probe.path, files, lambda c: c.path)
    if not chosen:
        return Prepared(action, probe.path, f"В проекте «{probe.name}» сохранять нечего.",
                        signature_of(action, probe.path, "nothing"), {"files": [], "count": 0}, noop=True,
                        line=probe.line, audit=audit)
    need_lock = [c for c in chosen if c.needs_lock and not c.locked_by_me and c.kind not in _NO_LOCK_KINDS]
    if need_lock:
        names = [display_path(c.path, probe.path) for c in need_lock]
        raise OpsError("lock_needed", 7, data={"files": _shown(names)}, untrusted_fields=["files[]"])
    from ..agent_policy import deny
    max_files = getattr(ctx.policy, "max_files_per_save", 50)
    if len(chosen) > max_files:
        deny("too_many_files", limit=max_files, count=len(chosen))
    deletes = sum(1 for c in chosen if c.kind in _DELETE_KINDS)
    max_deletes = getattr(ctx.policy, "max_deletes_per_save", 10)
    if deletes > max_deletes and not allow_deletes:
        deny("too_many_deletes", limit=max_deletes, count=deletes)
    rows = [(display_path(c.path, probe.path), c.kind) for c in chosen]
    kinds = {kind: sum(1 for _p, k in rows if k == kind) for kind in KIND_RU}
    parts = ", ".join(f"{KIND_RU[k]}: {n}" for k, n in kinds.items() if n)
    final = mark_comment(ctx, text)
    where = "у себя на компьютере" if local else "на сервер"
    summary = (f"Сохранить {where} в проекте «{probe.name}»: {files_count(len(rows))} ({parts}). "
               f"Комментарий: «{clean_name(final, 200)}».")
    audit.update(files=rows, comment=final)
    detail = {"count": len(rows), "kinds": {k: n for k, n in kinds.items() if n}, "comment": final,
              "files": [{"path": clean_name(p), "kind": k} for p, k in rows[:MAX_SHOWN]]}
    return Prepared(action, probe.path, summary, signature_of(action, probe.path, status.head, rows, final), detail,
                    untrusted=["files[].path"], line=probe.line, audit=audit,
                    state={"probe": probe, "paths": [c.path for c in chosen], "message": final, "rows": rows,
                           "local": local})


def execute_save(ctx: OpsContext, prepared: Prepared, project: str, message: str, files: Optional[list] = None,
                 allow_deletes: bool = False, local: bool = False) -> Result:
    state = prepared.state
    probe = state["probe"]
    with deadline(ctx) as token:
        result = ctx.client.commit(probe.path, state["paths"], state["message"], token)
        if state["local"] and result.revision is not None:
            ctx.client.update(probe.path, cancel=token)         # корень копии догоняет только что сохранённое
    if result.revision is None:
        return Result("save_apply", data={"number": None, "count": 0}, text="Сохранять было нечего.")
    ctx.audit.update(rev_after=result.revision)
    where = "у себя на компьютере" if state["local"] else "на сервер"
    text = (f"Сохранено {where} в проекте «{probe.name}»: {files_count(len(state['paths']))}, "
            f"сохранение № {result.revision}.")
    data = {"number": result.revision, "count": len(state["paths"]), "comment": state["message"],
            "files": [{"path": clean_name(p), "kind": k} for p, k in state["rows"][:MAX_SHOWN]]}
    return Result("save_apply", data=data, text=text, untrusted_fields=["files[].path"])


# --------------------------------------------------------------------------
# lock / unlock
# --------------------------------------------------------------------------

def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside_files(root: str, wanted: list) -> list:
    """Файлы проекта по относительным путям: внутри папки, существуют, не служебные. Иначе OpsError."""
    found, missing = [], []
    for raw in wanted:
        rel = str(raw).replace("\\", "/").strip().strip("/")
        full = os.path.join(root, *rel.split("/")) if rel else ""
        parts = rel.split("/")
        if not rel or ".." in parts or ".svn" in parts or os.path.isabs(str(raw)) or not os.path.isfile(full):
            missing.append(str(raw))
        elif full not in found:
            found.append(full)
    if missing:
        raise OpsError("not_found_here", 1, f"{texts.ERRORS['not_found_here'][0]} {', '.join(missing[:5])}",
                       texts.ERRORS["not_found_here"][1], data={"missing": missing[:20]})
    return found


def _lock_paths(locks: list, root: str) -> dict:
    return {_norm(p.path if os.path.isabs(p.path) else os.path.join(root, p.path)): p for p in locks}


def _prepare_lock(ctx: OpsContext, project: str, files: list, release: bool) -> Prepared:
    name = "unlock" if release else "lock"
    ref, probe = project_target(ctx, project, "lock", (KIND_PROJECT,), "Занимать файлы можно только в проекте с сервера.")
    chosen = _inside_files(probe.path, files)
    status = fresh_status(ctx, probe, True)
    mine = _lock_paths(status.my_locks, probe.path)
    others = _lock_paths(status.other_locks, probe.path)
    rels = {f: display_path(f, probe.path) for f in chosen}
    warnings: list = []
    todo: list = []
    for full in chosen:
        key = _norm(full)
        if release:
            if key in mine:
                todo.append(full)
            elif key in others:
                raise OpsError("lock_foreign", EXIT_NO_RIGHTS, "Этот файл занят другим человеком: его освободить нельзя.",
                               data={"files": [rels[full]]}, untrusted_fields=["files[]"])
            else:
                warnings.append(f"Файл «{clean_name(rels[full])}» вами не занят, пропущен.")
        else:
            if key in others:
                owner = others[key].owner
                raise OpsError("lock_foreign", EXIT_NO_RIGHTS,
                               f"Файл «{clean_name(rels[full])}» уже занят: {clean_name(owner, 60)}.",
                               data={"files": [rels[full]], "owner": clean_name(owner, 60)},
                               untrusted_fields=["files[]", "owner"])
            if key in mine:
                warnings.append(f"Файл «{clean_name(rels[full])}» уже занят вами.")
            else:
                todo.append(full)
    shown = [rels[f] for f in todo]
    audit = {"files": [(p, "освобождён" if release else "занят") for p in shown]}
    if not todo:
        return Prepared("lock", probe.path, "Менять нечего.", signature_of(name, probe.path, "nothing"),
                        {"files": []}, warnings=warnings, noop=True, line=probe.line, audit=audit)
    verb = "Освободить" if release else "Занять"
    summary = f"{verb} файлы в проекте «{probe.name}»: {files_count(len(todo))}."
    return Prepared("lock", probe.path, summary, signature_of(name, probe.path, sorted(shown)),
                    {"files": _shown(shown), "count": len(todo)}, warnings=warnings, untrusted=["files[]"],
                    line=probe.line, audit=audit, state={"probe": probe, "paths": todo, "shown": shown})


def prepare_lock(ctx: OpsContext, project: str, files: list) -> Prepared:
    return _prepare_lock(ctx, project, files, False)


def prepare_unlock(ctx: OpsContext, project: str, files: list) -> Prepared:
    return _prepare_lock(ctx, project, files, True)


def _execute_lock(ctx: OpsContext, prepared: Prepared, release: bool) -> Result:
    state = prepared.state
    done: list = []
    verb = "освобождены" if release else "заняты"
    try:
        for full, shown in zip(state["paths"], state["shown"]):
            if release:
                ctx.client.unlock(full)
            else:
                ctx.client.lock(full, LOCK_COMMENT)
            done.append(shown)
    finally:
        ctx.audit["files"] = [(p, "освобождён" if release else "занят") for p in done]
    tool = "unlock_apply" if release else "lock_apply"
    text = f"Файлы {verb} в проекте «{state['probe'].name}»: {files_count(len(done))}."
    return Result(tool, data={"files": _shown(done), "count": len(done)}, text=text, untrusted_fields=["files[]"])


def execute_lock(ctx: OpsContext, prepared: Prepared, project: str, files: list) -> Result:
    return _execute_lock(ctx, prepared, False)


def execute_unlock(ctx: OpsContext, prepared: Prepared, project: str, files: list) -> Result:
    return _execute_lock(ctx, prepared, True)


register(WriteSpec("get_new", prepare_get_new, execute_get_new))
register(WriteSpec("save", prepare_save, execute_save))
register(WriteSpec("lock", prepare_lock, execute_lock))
register(WriteSpec("unlock", prepare_unlock, execute_unlock))
