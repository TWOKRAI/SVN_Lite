"""Журнал действий помощника: что он смотрел и что менял (Task AG.4; не путать с svnhelper/audit.py: тот ищет мусорные файлы).

Одна строка JSON на действие в `data_dir()/agent/audit_ГГГГ-ММ.jsonl`, дописывается под межпроцессным замком
`named_lock("audit")` (консольная программа и сервер MCP пишут в один файл). Чтение тоже записывается кратко: сотрудник
видит, что смотрел помощник. Пароль и сырой вывод svn не пишутся: из ответа берётся только вид ошибки, текст
комментария проходит `messages.redact`. Поля `rev_before`, `rev_after`, `files`, `op_id`, `plan_id` нужны журналу с
откатом (AG.8): без них отмену не построить.

Запись не должна ломать операцию: `record` никогда не бросает исключений, а возвращает, удалось ли записать.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from ..config import data_dir
from ..folderlock import named_lock
from ..messages import redact
from .result import MAX_COMMENT, clean_name, clean_text

log = logging.getLogger("svnhelper.ops")

AUDIT_VERSION = 1
MAX_FILES = 50              # файлов в одной строке журнала
COMMENT_LIMIT = 200         # знаков комментария в журнале
LOCK_TIMEOUT = 5.0          # секунд ждать общий замок журнала
KEEP_MONTHS = 12            # месяцев хранения журнала действий (технический журнал agentlog короче, 3 месяца: он нужен для разбора сбоев, а этот для ответственности)
_NAME_RE = re.compile(r"^audit_(\d{4})-(\d{2})\.jsonl$")


def untrusted(text: object, limit: int = MAX_COMMENT) -> str:
    """Чужой текст (комментарий коллеги или помощника): без управляющих знаков, не длиннее limit, обрезка помечена «…».

    Это та же чистка, что уже применяется во всех операциях remote.py (`result.clean_text`): здесь она лишь доступна
    под именем из плана. Обёртка для текстового ответа («данные коллег: …») лежит в `texts.colleague`."""
    return clean_text(text, limit)


def audit_dir() -> Path:
    return data_dir() / "agent"


def audit_path(now: Optional[datetime] = None) -> Path:
    return audit_dir() / f"audit_{(now or datetime.now()):%Y-%m}.jsonl"


def prune_old(now: Optional[datetime] = None, folder: Optional[Path] = None) -> int:
    """Удаляет файлы журнала старше KEEP_MONTHS месяцев. Вызывается при первой записи месяца. Возвращает число
    удалённых; ошибки (файл занят) пишет в журнал программы и не пробрасывает."""
    now = now or datetime.now()
    folder = folder or audit_dir()
    limit = now.year * 12 + now.month - KEEP_MONTHS
    removed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        match = _NAME_RE.match(name)
        if match and int(match.group(1)) * 12 + int(match.group(2)) < limit:
            try:
                os.remove(folder / name)
                removed += 1
            except OSError:
                log.warning("Старый журнал действий помощника не удалён: %s", name)
    return removed


def _files(files: Iterable) -> tuple:
    """([{path, action}] не больше MAX_FILES, сколько файлов всего). Элемент: строка или пара (путь, вид) или словарь."""
    rows = []
    total = 0
    for item in files or ():
        total += 1
        if len(rows) >= MAX_FILES:
            continue
        if isinstance(item, dict):
            path, action = item.get("path", ""), item.get("action", "")
        elif isinstance(item, (tuple, list)):
            path, action = item[0], (item[1] if len(item) > 1 else "")
        else:
            path, action = item, ""
        rows.append({"path": clean_name(path), "action": clean_name(action, 20)})
    return rows, total


def _number(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def build_entry(ctx, operation: str, outcome: str = "ok", *, read_only: bool = True, project: object = "",
                files: Iterable = (), plan_id: str = "", exit_code: int = 0, rev_before: object = None,
                rev_after: object = None, op_id: str = "", comment: object = "", undone_rev: object = None,
                now: Optional[datetime] = None) -> dict:
    """Строка журнала как словарь. Набор ключей постоянный (его читает окно журнала, AG.8). `undone_rev` пишет только
    удавшаяся отмена сохранения: номер сохранения, которое она обратила (в старых строках поля нет)."""
    rows, total = _files(files)
    text = str(comment or "").strip()
    user = getattr(getattr(ctx, "settings", None), "username", "") or getattr(getattr(ctx, "config", None),
                                                                              "username", "")
    return {
        "v": AUDIT_VERSION,
        "ts": (now or datetime.now()).astimezone().isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "client": clean_name(getattr(ctx, "client_name", ""), 20),
        "client_info": clean_name(getattr(ctx, "client_info", ""), 100),
        "user": clean_name(user, 100),
        "operation": clean_name(operation, 40),
        "read_only": bool(read_only),
        "project": redact(clean_name(project)),
        "files": rows,
        "files_total": total,
        "plan_id": clean_name(plan_id, 64),
        "outcome": clean_name(outcome, 60),
        "exit_code": exit_code if isinstance(exit_code, int) else 1,
        "rev_before": _number(rev_before),
        "rev_after": _number(rev_after),
        "op_id": clean_name(op_id, 64),
        "undone_rev": _number(undone_rev),
        "comment": redact(untrusted(text, COMMENT_LIMIT)),
        "comment_truncated": len(text) > COMMENT_LIMIT,
    }


def record(ctx, operation: str, outcome: str = "ok", **fields) -> bool:
    """Дописывает строку в журнал. Ошибка записи пишется в журнал программы и не пробрасывается: журнал не должен
    ронять операцию. Возвращает True, если строка записана. Поля: см. build_entry."""
    try:
        now = fields.pop("now", None) or datetime.now()
        entry = build_entry(ctx, operation, outcome, now=now, **fields)
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        path = audit_path(now)
        path.parent.mkdir(parents=True, exist_ok=True)
        first_of_month = not path.exists()
        with named_lock("audit", LOCK_TIMEOUT):
            with open(path, "ab") as handle:
                handle.write(line)
        if first_of_month:
            prune_old(now, path.parent)
        return True
    except Exception:                                   # журнал важен, но операции он не ломает
        log.exception("Не удалось записать журнал действий помощника")
        return False


def list_months(folder: Optional[Path] = None) -> list:
    """Месяцы, за которые есть файл журнала: [(год, месяц)], свежие первыми."""
    try:
        names = os.listdir(folder or audit_dir())
    except OSError:
        return []
    found = [(int(m.group(1)), int(m.group(2))) for m in map(_NAME_RE.match, names) if m]
    return sorted(found, reverse=True)


def read_entries(now: Optional[datetime] = None) -> list:
    """Строки журнала месяца (словари). Повреждённые строки пропускаются."""
    try:
        text = audit_path(now).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
