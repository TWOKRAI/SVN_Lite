"""План записи: токен, срок, отпечаток параметров и состояние (Task AG.5).

План лежит файлом `data_dir()/agent/plans/<токен>.json`. Токен - 128 случайных бит (32 шестнадцатеричных знака), одноразовый:
применить по нему можно один раз, повтор возвращает прежний результат (`already_applied`) и не пишет второй раз. Применение
проверяет срок, отпечаток параметров (токен от другого списка файлов или другого комментария не подходит) и подпись
того, что будет сделано (если состояние проекта изменилось с момента плана, нужен новый план).

Токен не заменяет подтверждение человека: настоящее подтверждение даёт клиент (Claude спрашивает на вызове `*_apply`).
Состояния меняются под межпроцессным замком `plans`, поэтому два процесса не применят один план дважды.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..config import data_dir
from ..folderlock import named_lock

log = logging.getLogger("svnhelper.ops")

TOKEN_RE = re.compile(r"[0-9a-f]{32}")
LOCK_NAME = "plans"
LOCK_TIMEOUT = 5.0
KEEP_SECONDS = 24 * 3600            # просроченные и применённые планы хранятся сутки (для повторного ответа и разбора)

PLANNED = "planned"
APPLYING = "applying"
APPLIED = "applied"
FAILED = "failed"


def plans_dir() -> Path:
    return data_dir() / "agent" / "plans"


def fingerprint(op: str, path: str, params: dict) -> str:
    """Отпечаток вызова: операция, папка и параметры (без токена). Порядок ключей и пробелы не важны."""
    body = {"op": op, "path": os.path.normcase(os.path.normpath(path)) if path else "",
            "params": {k: v for k, v in sorted(params.items()) if k != "token" and v is not None}}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Plan:
    id: str
    op: str
    path: str
    fingerprint: str
    signature: str                  # что будет сделано (от prepare): список файлов, номера
    summary: str
    created: float
    expires: float
    state: str = PLANNED
    result: Optional[dict] = None   # Result.to_dict() применённого плана
    failure: str = ""               # вид ошибки, если применение не удалось
    client: str = ""

    @property
    def expired(self) -> bool:
        return time.time() > self.expires


def _file(token: str) -> Path:
    return plans_dir() / f"{token}.json"


def valid_token(token: object) -> bool:
    return isinstance(token, str) and bool(TOKEN_RE.fullmatch(token))


def _read(token: str) -> Optional[Plan]:
    try:
        data = json.loads(_file(token).read_text(encoding="utf-8"))
        return Plan(**{k: data[k] for k in Plan.__dataclass_fields__ if k in data})
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write(plan: Plan) -> None:
    path = _file(plan.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(plan), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def prune(now: Optional[float] = None) -> int:
    """Удаляет планы, которым больше суток после срока. Вызывается при составлении нового плана."""
    now = now or time.time()
    removed = 0
    try:
        names = os.listdir(plans_dir())
    except OSError:
        return 0
    for name in names:
        file = plans_dir() / name
        try:
            if now - file.stat().st_mtime > KEEP_SECONDS:
                file.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def create(op: str, path: str, params: dict, signature: str, summary: str, ttl_minutes: int,
           client: str = "") -> Plan:
    now = time.time()
    plan = Plan(id=secrets.token_hex(16), op=op, path=path, fingerprint=fingerprint(op, path, params),
                signature=signature, summary=summary, created=now, expires=now + max(1, ttl_minutes) * 60,
                client=client)
    with named_lock(LOCK_NAME, LOCK_TIMEOUT):
        _write(plan)
    prune(now)
    return plan


def load(token: object) -> Optional[Plan]:
    """План по токену; неверный токен (не 32 шестнадцатеричных знака) или чужой файл: None. Имя файла из токена не
    строится, пока токен не проверен: обойти папку планов нельзя."""
    return _read(token) if valid_token(token) else None


def claim(token: str) -> Optional[Plan]:
    """planned -> applying под замком. None: плана нет или он уже взят/применён/просрочен (смотрите `load`)."""
    with named_lock(LOCK_NAME, LOCK_TIMEOUT):
        plan = _read(token)
        if plan is None or plan.state != PLANNED or plan.expired:
            return None
        plan.state = APPLYING
        _write(plan)
        return plan


def finish(plan: Plan, result: Optional[dict] = None, failure: str = "") -> None:
    """applying -> applied (с результатом) или failed (вид ошибки)."""
    with named_lock(LOCK_NAME, LOCK_TIMEOUT):
        current = _read(plan.id) or plan
        current.state = FAILED if failure else APPLIED
        current.result, current.failure = result, failure
        _write(current)
