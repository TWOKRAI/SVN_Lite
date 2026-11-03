"""Политика безопасности ИИ-помощника: что ему разрешено, в каких папках и сколько раз в час.

Файл `data_dir()/agent_policy.json` (формат в docs/PLAN_agent_access.md, Task AG.4). Подпись `sig` - это
`config.protect_text` от sha256 канонического JSON без `sig` (DPAPI, как у пароля). Писать политику может только
`write()`: её вызывают окно SVN-lite (AG.7) и администратор (`python -m svnhelper.agent_policy`). В консольной
программе и сервере MCP команды смены политики нет. Подпись защищает от случайной правки файла помощником, но не от
враждебного процесса того же пользователя (он может вызвать DPAPI сам).

Действует меньший из двух режимов: потолок администратора (`agent_max_mode` в svn_helper.json: off | read_only | write,
по умолчанию read_only) и режим из файла политики (read_only | write). Нет файла политики или неверная подпись:
read_only. Неверное значение в файле заменяется безопасным и пишется в журнал, как в `config.clean_notify`.

Модуль не импортирует `svnhelper.ops` на уровне модуля: слой ops сам подключает политику (цикла нет). Отказы
(`Policy.check`) бросают `ops.errors.OpsError` с кодом возврата 6; ошибка подтягивается в момент отказа.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .config import (AGENT_MODES, DEFAULT_AGENT_MAX_MODE, SettingsStore, build_config, data_dir, load_shared_cache,
                     protect_text, stricter_mode, unprotect_text)
from .folderlock import named_lock

log = logging.getLogger("svnhelper.agent_policy")

POLICY_NAME = "agent_policy.json"
FORMAT = 1
FILE_MODES = ("read_only", "write")
# разрешения: save_local и save_server - сохранить у себя и на сервер, lock - занять и освободить файл,
# get_new - получить новое, create_local и create_server - создать проект на компьютере и на сервере, publish - отправить
ACTIONS = ("save_local", "save_server", "lock", "get_new", "create_local", "create_server", "publish")
WRITE_WINDOW = 3600.0           # секунд, за которые считаются записи для лимита в час
COUNTERS_LOCK = "policy"
COUNTERS_TIMEOUT = 10.0

# состояние подписи
SIG_OK = "ok"
SIG_NO_FILE = "no_file"         # файла политики нет: помощник только смотрит, это обычное состояние
SIG_INVALID = "invalid"         # файл правили не через SVN-lite, он повреждён или не читается

DEFAULTS: dict = {
    "format": FORMAT, "mode": "read_only", "allow": [], "allowed_dirs": [],
    "max_files_per_save": 50, "max_deletes_per_save": 10, "max_writes_per_hour": 20, "min_comment_chars": 10,
    "plan_ttl_minutes": 10, "mark_comments": True, "max_history": 50, "max_text_chars": 500,
}
# числовые поля: (название для сообщения, наименьшее, наибольшее)
_INT_FIELDS = {
    "max_files_per_save": ("Файлов в одном сохранении", 1, 1000),
    "max_deletes_per_save": ("Удалений в одном сохранении", 0, 1000),
    "max_writes_per_hour": ("Записей в час", 0, 1000),
    "min_comment_chars": ("Наименьшая длина комментария", 0, 200),
    "plan_ttl_minutes": ("Срок плана, минут", 1, 120),
    "max_history": ("Записей в истории", 1, 50),
    "max_text_chars": ("Знаков в комментарии коллеги", 50, 2000),
}


def policy_path() -> Path:
    return data_dir() / POLICY_NAME


def counters_path() -> Path:
    return data_dir() / "agent" / "counters.json"


# --------------------------------------------------------------------------
# Проверка значений и подпись
# --------------------------------------------------------------------------

def clean_policy(raw: dict) -> tuple:
    """Проверяет значения политики. Возвращает (чистые значения без sig, сообщения по-русски о том, что заменено).
    Неверное значение заменяется безопасным (по умолчанию); ненужные ключи отбрасываются."""
    clean = {key: (list(value) if isinstance(value, list) else value) for key, value in DEFAULTS.items()}
    problems: list = []
    mode = raw.get("mode", DEFAULTS["mode"])
    if mode in FILE_MODES:
        clean["mode"] = mode
    else:
        problems.append(f"Режим помощника «{mode}» неизвестен: оставлен только просмотр (read_only).")
    allow = raw.get("allow", [])
    if isinstance(allow, list):
        clean["allow"] = [a for a in dict.fromkeys(allow) if a in ACTIONS]
        for bad in (a for a in allow if a not in ACTIONS):
            problems.append(f"Разрешение «{bad}» неизвестно и пропущено.")
    else:
        problems.append("Список разрешений задан неверно: помощнику ничего не разрешено.")
    dirs = raw.get("allowed_dirs", [])
    if isinstance(dirs, list):
        clean["allowed_dirs"] = list(dict.fromkeys(os.path.expandvars(d.strip()) for d in dirs
                                                   if isinstance(d, str) and d.strip()))
    else:
        problems.append("Список разрешённых папок задан неверно: добавочных папок нет.")
    for key, (label, low, high) in _INT_FIELDS.items():
        value = raw.get(key, DEFAULTS[key])
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            clean[key] = value
        else:
            problems.append(f"{label}: нужно целое число от {low} до {high}. Возвращено значение по умолчанию: "
                            f"{DEFAULTS[key]}.")
    mark = raw.get("mark_comments", DEFAULTS["mark_comments"])
    if isinstance(mark, bool):
        clean["mark_comments"] = mark
    else:
        problems.append("Метка в комментариях: нужно «да» или «нет». Возвращено значение по умолчанию.")
    return clean, problems


def _digest(values: dict) -> str:
    """sha256 канонического JSON (ключи по порядку, без пробелов, без `sig`): правка пробелов подпись не ломает."""
    body = {key: value for key, value in values.items() if key != "sig"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _signature_ok(data: dict) -> bool:
    sig = data.get("sig")
    return isinstance(sig, str) and bool(sig) and unprotect_text(sig) == _digest(data)


def write(values: Optional[dict] = None, path: Optional[Path] = None) -> dict:
    """Записывает политику с подписью (единственный способ её изменить). Значения поверх умолчаний, проверка как в
    `clean_policy`. Возвращает записанный словарь. Не подписать (DPAPI недоступен): OSError, файл не пишется."""
    clean, problems = clean_policy({**DEFAULTS, **(values or {})})
    for problem in problems:
        log.warning("Политика помощника: %s", problem)
    clean["sig"] = protect_text(_digest(clean))
    path = path or policy_path()
    payload = json.dumps(clean, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with named_lock(COUNTERS_LOCK, COUNTERS_TIMEOUT):
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    log.info("Политика помощника записана: режим %s, разрешений %d", clean["mode"], len(clean["allow"]))
    return clean


# --------------------------------------------------------------------------
# Политика
# --------------------------------------------------------------------------

def deny(reason: str, **fields) -> None:
    """Отказ политики (код возврата 6). Тексты в ops/texts.py (POLICY_DENIALS)."""
    from .ops import texts
    from .ops.errors import OpsError
    from .ops.result import EXIT_POLICY
    message, next_step = texts.POLICY_DENIALS[reason]
    raise OpsError("policy_denied", EXIT_POLICY, message.format(**fields), next_step, svn={"reason": reason})


def _real(path: str) -> str:
    real = os.path.realpath(path)
    if real.startswith("\\\\?\\UNC\\"):
        real = "\\\\" + real[8:]
    elif real.startswith("\\\\?\\"):
        real = real[4:]
    return os.path.normcase(os.path.normpath(real))


def _inside(path: str, bases: Iterable[str]) -> bool:
    """Папка path равна одной из bases или лежит внутри неё (по настоящему пути, без ссылок и сети)."""
    real = _real(path)
    if real.startswith("\\\\"):
        return False
    for base in bases:
        root = _real(base).rstrip("\\")
        if root and not root.startswith("\\\\") and (real.rstrip("\\") == root or real.startswith(root + "\\")):
            return True
    return False


@dataclass(frozen=True)
class Policy:
    """Действующая политика. `mode` - итоговый режим (off | read_only | write): меньший из потолка и файла."""
    file_mode: str = "read_only"            # режим из файла (после проверки подписи)
    ceiling: str = DEFAULT_AGENT_MAX_MODE   # потолок администратора (agent_max_mode)
    allow: tuple = ()
    allowed_dirs: tuple = ()
    max_files_per_save: int = DEFAULTS["max_files_per_save"]
    max_deletes_per_save: int = DEFAULTS["max_deletes_per_save"]
    max_writes_per_hour: int = DEFAULTS["max_writes_per_hour"]
    min_comment_chars: int = DEFAULTS["min_comment_chars"]
    plan_ttl_minutes: int = DEFAULTS["plan_ttl_minutes"]
    mark_comments: bool = DEFAULTS["mark_comments"]
    max_history: int = DEFAULTS["max_history"]
    max_text_chars: int = DEFAULTS["max_text_chars"]
    signature: str = SIG_NO_FILE
    problems: tuple = ()
    reloadable: bool = field(default=False, compare=False)     # прочитана из файла: контекст перечитывает её на каждую операцию

    @property
    def mode(self) -> str:
        return stricter_mode(self.file_mode, self.ceiling)

    @classmethod
    def load(cls, ceiling: str = DEFAULT_AGENT_MAX_MODE, path: Optional[Path] = None) -> "Policy":
        """Читает файл политики. Любой сбой (нет файла, неверная подпись, повреждение) даёт read_only, не исключение."""
        ceiling = ceiling if ceiling in AGENT_MODES else DEFAULT_AGENT_MAX_MODE
        path = path or policy_path()
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cls(ceiling=ceiling, signature=SIG_NO_FILE, reloadable=True)
        except OSError:
            log.exception("Файл политики помощника не прочитан: запись выключена")
            return cls(ceiling=ceiling, signature=SIG_INVALID, reloadable=True)
        try:
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("ожидался объект JSON")
        except ValueError:
            log.warning("Файл политики помощника повреждён: запись выключена")
            return cls(ceiling=ceiling, signature=SIG_INVALID, reloadable=True)
        if not _signature_ok(data):
            log.warning("Файл политики помощника изменён не через SVN-lite (подпись неверна): запись выключена")
            return cls(ceiling=ceiling, signature=SIG_INVALID, reloadable=True)
        clean, problems = clean_policy(data)
        for problem in problems:
            log.warning("Политика помощника: %s", problem)
        return cls(file_mode=clean["mode"], ceiling=ceiling, allow=tuple(clean["allow"]),
                   allowed_dirs=tuple(clean["allowed_dirs"]), signature=SIG_OK, problems=tuple(problems),
                   reloadable=True,
                   **{key: clean[key] for key in (*_INT_FIELDS, "mark_comments")})

    def reload(self) -> "Policy":
        """Свежее чтение файла с тем же потолком: сервер MCP живёт долго, а политику меняет окно."""
        return Policy.load(self.ceiling)

    def describe(self) -> dict:
        return {"mode": self.mode, "file_mode": self.file_mode, "ceiling": self.ceiling, "signature": self.signature,
                "allow": list(self.allow), "allowed_dirs": list(self.allowed_dirs),
                "limits": {key: getattr(self, key) for key in (*_INT_FIELDS, "mark_comments")},
                "problems": list(self.problems)}

    def check(self, action: str, project: object = None, base_dirs: Optional[Iterable[str]] = None, line: str = "",
              consume: bool = False) -> None:
        """Разрешено ли записывающее действие. Отказ: OpsError с кодом 6 (версия, `line="tag"`: код 5).

        action: одно из ACTIONS. project: путь к папке проекта (или объект с `.path`); None - папку не проверять.
        base_dirs: папки, внутри которых можно работать (обычно `ctx.project_dirs`); None - только `allowed_dirs`.
        consume=True: считать вызов записью (под общим замком, виден другому процессу). План ничего не меняет
        и не считается: consume=True только перед самой записью."""
        if action not in ACTIONS:
            deny("action", action=action)
        if self.mode == "off":
            deny("off")
        if self.mode != "write":
            deny("tampered" if self.signature == SIG_INVALID else "mode")
        if action not in self.allow:
            deny("allow", action=action)
        path = getattr(project, "path", project)
        if path:
            allowed = self.allowed_dirs if base_dirs is None else (*base_dirs, *self.allowed_dirs)
            if not _inside(str(path), allowed):
                deny("folder")
        if line == "tag":
            from .ops.errors import OpsError
            from .ops.result import EXIT_NO_RIGHTS
            raise OpsError("read_only_line", EXIT_NO_RIGHTS)
        self._count_write(consume)

    def _count_write(self, consume: bool) -> None:
        """Лимит записей в час на все процессы: метки времени в `agent/counters.json` под общим замком."""
        path = counters_path()
        with named_lock(COUNTERS_LOCK, COUNTERS_TIMEOUT):
            now = time.time()
            stamps = [t for t in _read_counters(path) if now - WRITE_WINDOW < t <= now + 60]
            if len(stamps) >= self.max_writes_per_hour:
                deny("limit", limit=self.max_writes_per_hour)
            if consume:
                _write_counters(path, stamps + [now])


def _read_counters(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [float(t) for t in data["writes"]]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("Счётчик записей помощника повреждён: он начат заново")
        return []


def _write_counters(path: Path, stamps: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"writes": stamps}), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Администратор: python -m svnhelper.agent_policy
# --------------------------------------------------------------------------

def _file_values() -> dict:
    """Значения для правки: из действующего подписанного файла, иначе умолчания."""
    policy = Policy.load()
    if policy.signature != SIG_OK:
        return dict(DEFAULTS)
    values = {key: getattr(policy, key) for key in (*_INT_FIELDS, "mark_comments")}
    return {**DEFAULTS, **values, "mode": policy.file_mode, "allow": list(policy.allow),
            "allowed_dirs": list(policy.allowed_dirs)}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m svnhelper.agent_policy",
                                     description="Политика ИИ-помощника (для администратора).")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("show", help="показать действующую политику")
    setter = sub.add_parser("set", help="изменить политику (остальное остаётся как есть) и подписать")
    setter.add_argument("--mode", choices=FILE_MODES, help="режим: только просмотр или запись")
    setter.add_argument("--allow", help="разрешения через запятую (" + ", ".join(ACTIONS) + "); пусто - никаких")
    setter.add_argument("--allowed-dir", action="append", dest="allowed_dirs",
                        help="разрешённая папка (можно повторять); повтор «-» очищает список")
    for key, (label, _low, _high) in _INT_FIELDS.items():
        setter.add_argument("--" + key.replace("_", "-"), type=int, dest=key, help=label)
    setter.add_argument("--mark-comments", choices=("yes", "no"), help="добавлять метку помощника в комментарии")
    sub.add_parser("reset", help="вернуть умолчания (только просмотр) и подписать заново")
    return parser


def _apply_args(args: argparse.Namespace) -> dict:
    values = _file_values()
    if args.mode:
        values["mode"] = args.mode
    if args.allow is not None:
        values["allow"] = [item.strip() for item in args.allow.split(",") if item.strip()]
    if args.allowed_dirs is not None:
        values["allowed_dirs"] = [d for d in args.allowed_dirs if d != "-"]
    for key in _INT_FIELDS:
        if getattr(args, key, None) is not None:
            values[key] = getattr(args, key)
    if args.mark_comments:
        values["mark_comments"] = args.mark_comments == "yes"
    return values


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command in (None, "show"):
        ceiling = build_config(SettingsStore().settings, load_shared_cache()).agent_max_mode
        print(json.dumps(Policy.load(ceiling).describe(), ensure_ascii=False, indent=2))
        return 0
    values = dict(DEFAULTS) if args.command == "reset" else _apply_args(args)
    try:
        written = write(values)
    except OSError as exc:
        print(f"Политику записать не удалось: {exc}", file=sys.stderr)
        return 1
    print(f"Политика записана: режим {written['mode']}, разрешения: {', '.join(written['allow']) or 'нет'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
