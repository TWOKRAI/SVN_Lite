"""Откат: отменить своё сохранение новым сохранением (Task AG.8, разрешение `rollback`).

История не стирается: отмена это обычное новое сохранение, обращающее правки сохранения № N. Чтобы не потерять чужую
работу, помощник отменяет только:

* сохранение, сделанное вами (автор совпадает с вашим именем на сервере);
* целиком внутри этого проекта и этой линии (не создание проекта, ветки или версии);
* если после него никто не менял те же файлы и папки, а в проекте нет несохранённых правок, нового на сервере и конфликтов;
* не больше `max_files_per_save` файлов.

Отменить отмену можно той же операцией (номер отмены): так делается «вернуть вперёд». Версии (теги) не меняются.
Если обратить правки не удалось (конфликт, неожиданные изменения), папка возвращается как была.
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import unquote

from .. import agent_policy
from ..messages import files_count
from ..svn_core import SvnError
from .context import OpsContext, deadline
from .errors import OpsError, invalid_args
from .projects import KIND_PROJECT, display_path
from .result import Result, clean_name, clean_text
from .writing import Prepared, WriteSpec, fresh_status, mark_comment, project_target, register, signature_of

log = logging.getLogger("svnhelper.ops")

MAX_SHOWN = 50
_ACTION_RU = {"A": "добавлен (будет удалён)", "M": "изменён", "D": "удалён (вернётся)", "R": "заменён"}


def _repo_path(url: str, root: str) -> str:
    """Путь линии внутри хранилища («/проект/trunk») по адресу копии и корню."""
    path = unquote(url[len(root):] if root and url.startswith(root) else url)
    return "/" + path.strip("/")


def _related(a: str, b: str) -> bool:
    """Один путь совпадает с другим или лежит внутри него."""
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def prepare_rollback(ctx: OpsContext, project: str, revision: int, message: Optional[str] = None) -> Prepared:
    if not isinstance(revision, int) or revision < 1:
        raise invalid_args("Номер сохранения должен быть целым числом, начиная с 1.")
    ref, probe = project_target(ctx, project, "rollback", (KIND_PROJECT,),
                                "Отменять сохранения можно только в проекте с сервера.")
    info = probe.info
    base = _repo_path(info.url, info.root)
    status = fresh_status(ctx, probe, True)
    if status.conflicts:
        raise OpsError("conflict", 7, data={"conflicts": status.conflicts})
    if max(len(status.incoming_files), status.incoming):
        raise OpsError("incoming_first", 7)
    if status.local_changes:
        raise OpsError("local_changes", 7, "В проекте есть несохранённые правки: отменять сохранение с ними нельзя.",
                       "Сначала сохраните правки.", data={"changes": len(status.local_changes)})
    entries = ctx.client.log_range(info.url, revision, revision)
    if not entries:
        raise OpsError("not_found", 1, f"Сохранения № {revision} нет.", "Проверьте номер в истории проекта.")
    entry = entries[0]
    me = (ctx.settings.username or ctx.config.username or "").strip().casefold()
    if not me or (entry.author or "").strip().casefold() != me:
        agent_policy.deny("rollback_not_mine")
    inside = [p for p in entry.paths if _related(p.path, base) and p.path.rstrip("/") != base.rstrip("/")
              and p.path.startswith(base.rstrip("/") + "/")]
    if not inside or len(inside) != len(entry.paths):
        raise OpsError("wrong_kind", 7, "Это сохранение затрагивает не только этот проект и эту линию (или создаёт "
                                        "проект, ветку, версию): автоматически его не отменить.",
                       "Если это нужно, сделайте это вручную в программе SVN-lite.")
    limit = getattr(ctx.policy, "max_files_per_save", 50)
    if len(inside) > limit:
        agent_policy.deny("rollback_too_big", limit=limit)
    later = ctx.client.log_range(info.url, revision + 1)
    touched = [(e.revision, e.author) for e in later if any(_related(lp.path, p.path) for lp in e.paths for p in inside)]
    if touched:
        authors = sorted({clean_name(a, 60) for _r, a in touched if a})
        raise OpsError("newer_changes", 7, data={"later_numbers": [r for r, _a in touched[:20]], "authors": authors},
                       untrusted_fields=["authors[]"])
    rows = [(display_path(os.path.join(probe.path, *p.path[len(base):].strip("/").split("/")), probe.path), p.action)
            for p in inside]
    needs_lock = {os.path.normcase(os.path.normpath(x)) for x in ctx.client.propget_needs_lock(probe.path)}
    locked = {os.path.normcase(os.path.normpath(lock.path if os.path.isabs(lock.path)
                                                else os.path.join(probe.path, lock.path))) for lock in status.my_locks}
    blocked = []
    for rel, action in rows:
        full = os.path.normcase(os.path.normpath(os.path.join(probe.path, *rel.split("/"))))
        if action in ("M", "D", "R") and full in needs_lock and full not in locked:
            blocked.append(rel)
    if blocked:
        raise OpsError("lock_needed", 7, data={"files": blocked[:MAX_SHOWN]}, untrusted_fields=["files[]"])
    original = clean_text(entry.message, 100).replace("\n", " ")
    final = mark_comment(ctx, (message or "").strip() or f"Отмена сохранения № {revision}: {original}".rstrip(": "))
    parts = ", ".join(f"{_ACTION_RU.get(a, a)}: {sum(1 for _p, x in rows if x == a)}"
                      for a in dict.fromkeys(x for _p, x in rows))
    summary = (f"Отменить своё сохранение № {revision} в проекте «{probe.name}» новым сохранением: "
               f"{files_count(len(rows))} ({parts}).")
    detail = {"revision": revision, "count": len(rows), "original_comment": clean_text(entry.message, 200),
              "comment": final, "files": [{"path": clean_name(p), "action": a} for p, a in rows[:MAX_SHOWN]]}
    return Prepared("rollback", probe.path, summary, signature_of("rollback", probe.path, status.head, revision, rows,
                                                                  final),
                    detail, untrusted=["files[].path", "original_comment"], line=probe.line,
                    audit={"files": rows, "comment": final, "rev_before": status.head or probe.number},
                    state={"probe": probe, "rows": rows, "message": final, "revision": revision})


def execute_rollback(ctx: OpsContext, prepared: Prepared, project: str, revision: int,
                     message: Optional[str] = None) -> Result:
    state = prepared.state
    probe = state["probe"]
    info, path = probe.info, probe.path
    expected = {os.path.normcase(os.path.normpath(os.path.join(path, *rel.split("/")))) for rel, _a in state["rows"]}
    merged = False
    changed: list = []
    try:
        with deadline(ctx) as token:
            ctx.client.update(path, cancel=token)           # после сохранения части файлов копия «смешанная»: слияние требует ровной
            ctx.client.reverse_merge(path, info.url, state["revision"], token)
            merged = True
            after = fresh_status(ctx, probe, False)
            changed = [c.path for c in after.local_changes]
            unexpected = [p for p in changed if not any(os.path.normcase(os.path.normpath(p)) == e
                                                        or os.path.normcase(os.path.normpath(p)).startswith(e + os.sep)
                                                        or e.startswith(os.path.normcase(os.path.normpath(p)) + os.sep)
                                                        for e in expected)]
            if after.conflicts or unexpected or not changed:
                raise OpsError("conflict", 7, "Отмена не получилась: правки нельзя обратить без конфликта.",
                               "Папка возвращена как была. Отмените сохранение вручную в программе SVN-lite.")
            committed = ctx.client.commit(path, changed, state["message"], token)
    except BaseException:
        if merged:
            _undo_merge(ctx, path, changed)
        raise
    ctx.audit.update(rev_after=committed.revision, undone_rev=state["revision"])
    data = {"number": committed.revision, "undone": state["revision"], "count": len(state["rows"]),
            "files": [{"path": clean_name(p), "action": a} for p, a in state["rows"][:MAX_SHOWN]]}
    text = (f"Сохранение № {state['revision']} отменено в проекте «{probe.name}»: "
            f"{files_count(len(state['rows']))}, новое сохранение № {committed.revision}.")
    return Result("rollback_apply", data=data, text=text, untrusted_fields=["files[].path"])


def _undo_merge(ctx: OpsContext, path: str, changed: list) -> None:
    """Убирает только то, что сделало наше обратное слияние: папка остаётся такой, как была до отмены."""
    try:
        targets = changed or [path]
        ctx.client.revert_paths(path, targets)
    except SvnError:
        log.exception("Не удалось вернуть папку после неудавшейся отмены сохранения")


register(WriteSpec("rollback", prepare_rollback, execute_rollback))
