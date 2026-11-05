"""Слой работы с svn.exe: запуск процесса, разбор XML, классификация ошибок.

Модуль не зависит от tkinter и может тестироваться отдельно.
Все методы блокирующие: вызывать их нужно из фоновых потоков.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import unquote
from typing import TYPE_CHECKING, Optional, Sequence, Union

if TYPE_CHECKING:
    from .config import FilterRules

log = logging.getLogger("svnhelper.svn")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LIST_TIMEOUT = 60  # секунд на листинг одной папки
CONNECT_TIMEOUT = 15      # секунд на листинг, по которому решаем «связь есть» (templates_root)
STREAM_IDLE_TIMEOUT = 300  # export/checkout: сколько секунд svn может молчать, прежде чем считаем связь оборванной


# --------------------------------------------------------------------------
# Ошибки
# --------------------------------------------------------------------------

class ErrorKind(Enum):
    AUTH = "auth"                    # неверный логин/пароль или вход не выполнен
    AUTHZ = "authz"                  # вход выполнен, но прав на путь нет
    NO_CONNECTION = "no_connection"  # сервер недоступен
    NOT_FOUND = "not_found"          # папки нет на сервере
    TIMEOUT = "timeout"              # сервер не отвечает
    CANCELLED = "cancelled"          # пользователь нажал «Отмена»
    LOCKED = "locked"                # файл уже занят другим пользователем
    OUT_OF_DATE = "out_of_date"      # на сервере есть более новая версия файла
    EXISTS = "exists"                # такая ветка или версия уже есть на сервере
    UNRELATED = "unrelated"          # линии не связаны общей историей: переключить копию нельзя
    BUSY = "busy"                    # файл открыт в другой программе (Компас, Word, Excel)
    LOCK_REQUIRED = "lock_required"  # файл с svn:needs-lock нужно сначала занять, потом сохранять
    READ_ONLY_LINE = "read_only_line"  # версия (тег) только для чтения: хук сервера отклонил сохранение
    LOCAL_HISTORY_MISSING = "local_history_missing"  # хранилище локальной истории (file://) пропало; связь с сервером ни при чём
    OTHER = "other"


_CODE_RE = re.compile(r"\b([EW]\d{6})\b")

# E160035: путь уже заблокирован другим, E195022/E195024: блокировка не наша или устарела
# E160037: путь занят на сервере, а у копии нет подходящего токена блокировки (при сохранении)
_LOCKED_CODES = {"E160035", "W160035", "E195022", "W195022", "E160037"}
# W160042/E160042: Lock failed, newer version exists; E155011/E160028: файл устарел при сохранении
_OUT_OF_DATE_CODES = {"W160042", "E160042", "E155011", "E160028"}
_NOT_FOUND_CODES = {"E160013", "E210005", "W160013"}
# E160020: путь уже существует (гонка при создании ветки/версии)
_EXISTS_CODES = {"E160020", "W160020"}
# E195012: switch между линиями без общего предка (проверено на svn 1.14)
_UNRELATED_CODES = {"E195012"}
# файл занят другой программой при записи (Windows): E720032 — sharing violation,
# E720005 — access denied, E720033 — lock violation (фактические коды см. тест BusyFile)
_BUSY_CODES = {"E720032", "E720005", "E720033"}
# E730060 = WSAETIMEDOUT, E120001 = APR_TIMEUP, E175012 = таймаут http
_TIMEOUT_CODES = {"E730060", "E120001", "E175012"}
# E730061 отказ в соединении, E730065 нет маршрута, E731001 имя не найдено,
# E210002 соединение закрыто сервером; E730050/53/54/64 — сеть упала, соединение
# прервано или сброшено (обрыв VPN), узел недоступен
_NO_CONNECTION_CODES = {"E170013", "E175002", "E730061", "E730065", "E730051",
                        "E731001", "E210002", "E730050", "E730053", "E730054", "E730064"}


def extract_codes(text: str) -> set[str]:
    return set(_CODE_RE.findall(text or ""))


def classify_error(text: str, had_credentials: bool = False) -> ErrorKind:
    """Определяет тип ошибки по кодам E######, а не по (локализуемому) тексту.

    Текст используется только как дополнительная подсказка для E170001,
    который svn выдаёт и при отсутствии входа, и при отказе в правах.
    """
    codes = extract_codes(text)
    if "E215004" in codes:
        return ErrorKind.AUTH
    if "E170001" in codes:
        if "Authorization failed" in text:
            return ErrorKind.AUTHZ
        if "Authentication" in text:
            return ErrorKind.AUTH
        return ErrorKind.AUTHZ if had_credentials else ErrorKind.AUTH
    # E180001 на file://: локальное хранилище переименовано или удалено. Это не обрыв связи с сервером,
    # хотя svn пишет рядом и E170013 «Unable to connect»; проверяем раньше NO_CONNECTION
    if "E180001" in codes and "file://" in text.lower():
        return ErrorKind.LOCAL_HISTORY_MISSING
    # E165001: хук pre-commit отклонил сохранение; наш хук пишет «read-only» / «только для чтения»
    if "E165001" in codes and ("read-only" in text or "только для чтения" in text):
        return ErrorKind.READ_ONLY_LINE
    if codes & _LOCKED_CODES:
        return ErrorKind.LOCKED
    # W160042 приходит вместе с E200009 («не удалось заблокировать»), поэтому ищем его до OTHER
    if codes & _OUT_OF_DATE_CODES:
        return ErrorKind.OUT_OF_DATE
    if codes & _BUSY_CODES:
        return ErrorKind.BUSY
    # «не найдено» раньше «нет связи»: у несуществующего репозитория
    # svn пишет и E170013, и E210005.
    if codes & _NOT_FOUND_CODES:
        return ErrorKind.NOT_FOUND
    if codes & _EXISTS_CODES:
        return ErrorKind.EXISTS
    if codes & _UNRELATED_CODES:
        return ErrorKind.UNRELATED
    if codes & _TIMEOUT_CODES:
        return ErrorKind.TIMEOUT
    if codes & _NO_CONNECTION_CODES or "Unable to connect" in text:
        return ErrorKind.NO_CONNECTION
    return ErrorKind.OTHER


class SvnError(Exception):
    def __init__(self, kind: ErrorKind, raw: str = "", returncode: Optional[int] = None):
        super().__init__(raw or kind.value)
        self.kind = kind
        self.raw = raw
        self.returncode = returncode
        self.codes = extract_codes(raw)

    def to_dict(self) -> dict:
        """Для CLI и MCP: тип, коды E###### и короткий текст по-русски. Сырой ответ svn (raw) не включается:
        в нём могут быть пароль и адреса."""
        from .messages import short_message   # отложенный импорт: messages сам импортирует svn_core
        return {"kind": self.kind.value, "codes": sorted(self.codes), "message": short_message(self.kind)}


# --------------------------------------------------------------------------
# Кодировки вывода
# --------------------------------------------------------------------------

def _windows_codepage(func_name: str, fallback: str) -> str:
    try:
        return "cp%d" % getattr(ctypes.windll.kernel32, func_name)()
    except Exception:  # не Windows
        return fallback


def decode_output(data: bytes) -> str:
    """utf-8 → ANSI-кодовая страница → OEM-кодовая страница."""
    ansi = _windows_codepage("GetACP", "cp1252")
    oem = _windows_codepage("GetOEMCP", "cp866")
    for enc in ("utf-8", ansi, oem):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _encode_for_stdin(text: str) -> bytes:
    """svn берёт пароль со stdin как есть, байтами UTF-8 (проверено на svn 1.14 + svnserve:
    cp1251 даёт «Password incorrect» для кириллицы, а пароль через --password svn сам переводит в UTF-8)."""
    return text.encode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Учётные данные, отмена, прогресс
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:  # пароль не должен попасть ни в лог, ни в трассировку
        return f"Credentials(username={self.username!r}, password=***)"


class CancelToken:
    """Позволяет прервать операцию: убивает привязанные процессы svn."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            procs = list(self._procs)
        for proc in procs:
            _kill(proc)

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if not self._event.is_set():
                self._procs.add(proc)
                return
        _kill(proc)

    def detach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass


class CheckoutProgress:
    """Счётчик, который worker обновляет, а окно периодически читает."""

    def __init__(self) -> None:
        self.count = 0
        self.last_path = ""


# --------------------------------------------------------------------------
# Результаты
# --------------------------------------------------------------------------

@dataclass
class ListEntry:
    kind: str   # "file" | "dir"
    name: str
    size: int = 0
    author: str = ""                       # автор последней правки (<commit><author>)
    date: Optional[datetime] = None        # aware datetime UTC последней правки
    revision: Optional[int] = None         # версия последней правки
    lock: Optional["LockInfo"] = None      # блокировка файла (<lock>), если есть


@dataclass
class LineEntry:
    """Ветка или версия (тег) в списке: имя и последняя правка в ней."""
    name: str
    last_author: str = ""
    last_date: Optional[datetime] = None
    last_revision: Optional[int] = None


@dataclass
class AuthorStats:
    name: str
    commits: int
    last_date: Optional[datetime] = None


@dataclass
class LogPath:
    """Изменённый путь в записи журнала (svn log -v)."""
    action: str                  # A / M / D / R
    path: str                    # путь в репозитории, например /proj/branches/x
    copyfrom_path: str = ""
    copyfrom_rev: Optional[int] = None


@dataclass
class LogEntry:
    revision: int
    author: str = ""
    date: Optional[datetime] = None
    message: str = ""
    paths: list = field(default_factory=list)   # LogPath


@dataclass
class BranchHistory:
    """Кто и когда создал ветку (версию), откуда, и кто в ней работал."""
    created_by: str = ""
    created: Optional[datetime] = None
    created_revision: Optional[int] = None
    copied_from: str = ""                  # путь в репозитории, например "/projects/X/trunk"; пусто, если не копия
    copied_from_revision: Optional[int] = None
    comment: str = ""                      # сообщение коммита создания
    authors: list = field(default_factory=list)   # AuthorStats, по last_date по убыванию
    last_revision: Optional[int] = None
    last_date: Optional[datetime] = None
    complete: bool = True                  # False: журнал обрезан limit, создание могло не попасть

    @property
    def from_label(self) -> str:
        """«из основной линии (версия 345)» / «из ветки «x»» / «из версии «y»»; пусто, если неизвестно."""
        parts = [p for p in self.copied_from.split("/") if p]
        for i, part in enumerate(parts):
            if part == "trunk":
                rev = f" (версия {self.copied_from_revision})" if self.copied_from_revision else ""
                return f"из основной линии{rev}"
            if part in ("branches", "tags") and i + 1 < len(parts):
                return f"из ветки «{parts[i + 1]}»" if part == "branches" else f"из версии «{parts[i + 1]}»"
        return ""


@dataclass
class Estimate:
    files: int
    dirs: int
    size: int
    items: list = field(default_factory=list, repr=False)  # все записи svn list -R (ListEntry)

    @property
    def entries(self) -> int:
        return self.files + self.dirs


@dataclass
class CheckoutResult:
    revision: Optional[int]
    entries: int


@dataclass
class WcInfo:
    url: str
    revision: int
    root: str = ""
    uuid: str = ""        # опознавательный номер хранилища: по нему находят переехавшее хранилище истории


@dataclass
class LockInfo:
    """Блокировка файла (Get lock): кто занял, когда и с каким комментарием."""
    path: str                              # локальный путь, как его напечатал svn status
    owner: str
    created: Optional[datetime] = None     # aware datetime в UTC
    comment: str = ""

    @property
    def age_days(self) -> Optional[int]:
        """Сколько полных дней файл занят (None, если дата неизвестна)."""
        if self.created is None:
            return None
        return max(0, (datetime.now(timezone.utc) - self.created).days)


def parse_svn_date(text: Optional[str]) -> Optional[datetime]:
    """'2026-10-19T08:00:00.000000Z' → aware datetime UTC; мусор → None."""
    if not text:
        return None
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _lock_from_xml(lock: ET.Element, path: str) -> LockInfo:
    return LockInfo(path=path, owner=lock.findtext("owner") or "?",
                    created=parse_svn_date(lock.findtext("created")),
                    comment=lock.findtext("comment") or "")


@dataclass
class LocalChange:
    """Своя правка в рабочей копии: путь (как напечатал svn status) и вид."""
    path: str
    kind: str                      # modified | added | deleted | missing | unversioned | conflicted
    locked_by_me: bool = False     # файл занят вами (есть токен блокировки)
    needs_lock: bool = False       # на файле svn:needs-lock: менять его можно только занятым


@dataclass
class CommitResult:
    revision: Optional[int]        # новая версия; None, если сохранять было нечего
    committed: list = field(default_factory=list)   # пути, которые svn отправил (как напечатал svn commit)


@dataclass
class WcStatus:
    """Состояние рабочей копии (svn status [-u])."""
    head: Optional[int] = None                       # последняя версия на сервере (только с -u)
    modified: int = 0                                # свои правки: изменено, добавлено, удалено, потеряно
    unversioned: int = 0                             # новые файлы, которых нет в SVN
    conflicts: int = 0
    incoming: int = 0                                # на сервере есть более новое (только с -u)
    changed_files: list = field(default_factory=list)
    locked_by_me: list = field(default_factory=list)
    locked_by_others: list = field(default_factory=list)   # (путь, владелец)
    my_locks: list = field(default_factory=list)           # LockInfo: файлы, занятые вами
    other_locks: list = field(default_factory=list)        # LockInfo: файлы, занятые другими
    min_revision: Optional[int] = None                     # только status(verbose=True): наименьшая версия записей
    max_revision: Optional[int] = None                     # только status(verbose=True): наибольшая (как svnversion)
    switched: int = 0                                      # записей, переключённых на другую линию (switched="true")
    local_changes: list[LocalChange] = field(default_factory=list)   # свои правки по файлам
    incoming_files: list[str] = field(default_factory=list)          # пути, изменённые на сервере (только с -u)

    @property
    def tracked_changes(self) -> int:
        """Несохранённые правки в файлах под контролем SVN, по списку `local_changes`: без новых файлов и конфликтов.
        Счётчик `modified` для этого не годится: в нём и папки incomplete/obstructed (прерванное «Получить новое»),
        которые сохранять нечего (ревью v4, находка 2)."""
        return sum(1 for c in self.local_changes if c.kind not in ("unversioned", "conflicted"))

    @property
    def mixed(self) -> bool:
        """В копии есть записи разных версий (после Commit части файлов): для копирования берите max_revision."""
        return (self.min_revision is not None and self.max_revision is not None
                and self.min_revision != self.max_revision)


@dataclass
class UpdateResult:
    revision: Optional[int]
    added: int = 0
    updated: int = 0
    deleted: int = 0
    conflicts: list = field(default_factory=list)
    added_paths: list = field(default_factory=list)       # пути из строк «A», как напечатал svn


_LOCAL_CHANGE_ITEMS = {"modified", "added", "deleted", "replaced", "missing", "incomplete", "obstructed"}


def _local_change_kind(item: str, props: str, ws) -> Optional[str]:
    """Вид своей правки по записи wc-status; None, если это не правка (или вид не поддержан)."""
    if item == "conflicted" or props == "conflicted" or (ws is not None and ws.get("tree-conflicted") == "true"):
        return "conflicted"
    if item in ("modified", "replaced") or props == "modified":
        return "modified"
    if item in ("added", "deleted", "missing", "unversioned"):
        return item
    return None


def parse_status_xml(data: bytes) -> WcStatus:
    """`svn status --xml [-v] [-u]` → WcStatus. Версии записей (min/max_revision) есть только при -v."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SvnError(ErrorKind.OTHER, f"Не удалось разобрать XML от svn: {exc}") from exc
    st = WcStatus()
    against = root.find(".//against")
    if against is not None and (against.get("revision") or "").isdigit():
        st.head = int(against.get("revision"))
    for entry in root.iter("entry"):
        path = entry.get("path", "")
        ws = entry.find("wc-status")
        rs = entry.find("repos-status")
        item = ws.get("item", "") if ws is not None else ""
        props = ws.get("props", "") if ws is not None else ""
        if ws is not None and ws.get("switched") == "true":
            st.switched += 1
        rev = _int_or_none(ws.get("revision")) if ws is not None else None
        if rev is not None and item not in ("unversioned", "ignored", "external", "excluded"):
            st.min_revision = rev if st.min_revision is None else min(st.min_revision, rev)
            st.max_revision = rev if st.max_revision is None else max(st.max_revision, rev)
        if item == "conflicted" or props == "conflicted" or (ws is not None and ws.get("tree-conflicted") == "true"):
            st.conflicts += 1
            st.changed_files.append(path)
        elif item in _LOCAL_CHANGE_ITEMS or props == "modified":
            st.modified += 1
            st.changed_files.append(path)
        elif item == "unversioned":
            st.unversioned += 1
        if rs is not None and (rs.get("item", "none") != "none" or rs.get("props", "none") != "none"):
            st.incoming += 1
            st.incoming_files.append(path)
        change_kind = _local_change_kind(item, props, ws)
        if change_kind:
            st.local_changes.append(LocalChange(path, change_kind,
                                                locked_by_me=ws is not None and ws.find("lock") is not None))
        if ws is not None and ws.find("lock") is not None:
            st.locked_by_me.append(path)
            st.my_locks.append(_lock_from_xml(ws.find("lock"), path))
        elif rs is not None and rs.find("lock") is not None:
            st.locked_by_others.append((path, rs.findtext("lock/owner") or "?"))
            st.other_locks.append(_lock_from_xml(rs.find("lock"), path))
    return st


def parse_list_xml(data: bytes) -> list[ListEntry]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SvnError(ErrorKind.OTHER, f"Не удалось разобрать XML от svn: {exc}") from exc
    result = []
    for entry in root.iter("entry"):
        name = entry.findtext("name") or ""
        size_text = entry.findtext("size") or "0"
        commit = entry.find("commit")
        revision = commit.get("revision") if commit is not None else None
        lock = entry.find("lock")
        result.append(ListEntry(
            kind=entry.get("kind", ""),
            name=name,
            size=int(size_text) if size_text.isdigit() else 0,
            author=(commit.findtext("author") or "") if commit is not None else "",
            date=parse_svn_date(commit.findtext("date")) if commit is not None else None,
            revision=int(revision) if revision and revision.isdigit() else None,
            lock=_lock_from_xml(lock, name) if lock is not None else None,
        ))
    return result


def parse_locks_xml(data: bytes) -> list[LockInfo]:
    """Блокировки из `svn list -R --xml`; path — относительный путь файла."""
    return [e.lock for e in parse_list_xml(data) if e.lock is not None]


def _int_or_none(text: Optional[str]) -> Optional[int]:
    return int(text) if text and text.isdigit() else None


def parse_log_xml(data: bytes) -> list[LogEntry]:
    """`svn log --xml [-v]` → записи в том порядке, как их выдал svn (новые первыми)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SvnError(ErrorKind.OTHER, f"Не удалось разобрать XML от svn: {exc}") from exc
    result = []
    for item in root.iter("logentry"):
        paths = [LogPath(action=p.get("action", ""), path=(p.text or ""),
                         copyfrom_path=p.get("copyfrom-path", ""),
                         copyfrom_rev=_int_or_none(p.get("copyfrom-rev")))
                 for p in item.findall("paths/path")]
        result.append(LogEntry(revision=_int_or_none(item.get("revision")) or 0,
                               author=item.findtext("author") or "",
                               date=parse_svn_date(item.findtext("date")),
                               message=item.findtext("msg") or "",
                               paths=paths))
    return result


_MIN_DATE = datetime.min.replace(tzinfo=timezone.utc)


def build_branch_history(entries: list[LogEntry], complete: bool = True,
                         created: Optional[LogEntry] = None) -> BranchHistory:
    """Сводка по журналу линии (svn log --stop-on-copy): самая старая запись — создание.

    created — запись создания с путями (`log -v`), полученная отдельным запросом; без неё
    создание берётся из самой старой записи entries (тогда в ней должны быть пути).

    Создание копированием определяется по добавленному пути с copyfrom; из нескольких
    (например, и «tags», и «tags/x») берётся самый короткий — это и есть линия.
    """
    hist = BranchHistory(complete=complete)
    if not entries:
        return hist
    newest, oldest = entries[0], created or entries[-1]
    hist.last_revision, hist.last_date = newest.revision, newest.date
    hist.created_by, hist.created = oldest.author, oldest.date
    hist.created_revision, hist.comment = oldest.revision, oldest.message
    copies = [p for p in oldest.paths if p.action == "A" and p.copyfrom_path]
    if copies:
        top = min(copies, key=lambda p: len(p.path))
        hist.copied_from, hist.copied_from_revision = top.copyfrom_path, top.copyfrom_rev
    stats: dict[str, AuthorStats] = {}
    for entry in entries:  # новые первыми: первая встреченная дата автора и есть последняя
        name = entry.author or "?"
        if name not in stats:
            stats[name] = AuthorStats(name, 0, entry.date)
        stats[name].commits += 1
    hist.authors = sorted(stats.values(), key=lambda a: a.last_date or _MIN_DATE, reverse=True)
    return hist


# правки существующих (versioned) файлов: для них имеет смысл проверять svn:needs-lock
_NEEDS_LOCK_KINDS = ("modified", "deleted", "missing", "conflicted")


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_under(path: str, roots) -> bool:
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def _targets_args(folder: str, name: str, paths: Sequence[str]) -> list[str]:
    """Аргументы svn для списка путей: `--targets <файл>` (длинные списки, кириллица) или сами пути.

    svn читает файл --targets в кодировке системы (ANSI-кодовая страница Windows), а не в UTF-8:
    проверено на svn 1.14, файл в UTF-8 даёт E155007 с искажённым путём. Если какой-то путь в этой
    кодировке записать нельзя, пути передаются аргументами (командная строка Windows - Unicode).
    '@' в конце каждого пути защищает от разбора имени как peg-ревизии.
    """
    entries = [p + "@" for p in paths]
    try:
        data = "".join(e + "\n" for e in entries).encode(_windows_codepage("GetACP", "utf-8"))
    except (UnicodeEncodeError, LookupError):
        return entries
    target_file = os.path.join(folder, name)
    with open(target_file, "wb") as fh:
        fh.write(data)
    return ["--targets", target_file]


_ADDED_RE = re.compile(r"^A\s+(?:\(bin\)\s+)?(\S.*)$")
# строки checkout/export: «A    путь»; при checkout --force для файлов, уже лежавших в папке, svn пишет
# «E    путь» (existed) — их тоже считаем, иначе прогресс останется нулевым
_CHECKOUT_RE = re.compile(r"^[AE]\s+(?:\(bin\)\s+)?(\S.*)$")
# строки update — 4 колонки и пробел: текст, свойства, сломанная блокировка (B), конфликт дерева (C).
# «U    путь», «A    путь», «UU   путь», « U   путь» (только свойства), «   C путь» (конфликт дерева)
_ACTION_RE = re.compile(r"^(?! {4})([ADUCGERB ]{4}) (\S.*)$")
# строки import: «Adding         путь» (слово может быть переведено, за ним не меньше двух пробелов).
# Ошибки («svn: …») и текст отказа хука сервера сюда не попадают и остаются в выводе для отчёта
_IMPORT_RE = re.compile(r"^(?!svn: )\S+ {2,}(?:\(bin\)\s+)?(\S.*)$")


# --------------------------------------------------------------------------
# Клиент
# --------------------------------------------------------------------------

class SvnClient:
    """Обёртка над svn.exe. `svn_cmd` — путь к svn.exe или список с префиксом команды."""

    def __init__(self, svn_cmd: Union[str, Sequence[str]]):
        self._base = [svn_cmd] if isinstance(svn_cmd, str) else list(svn_cmd)
        self.svn_path = self._base[0] if len(self._base) == 1 else ""
        self._lock = threading.Lock()
        self._version_lock = threading.Lock()
        self._credentials: Optional[Credentials] = None
        self._cred_generation = 0
        self._version: Optional[tuple[int, int]] = None
        self._store_auth = True

    # -- учётные данные -----------------------------------------------------

    @property
    def credentials(self) -> Optional[Credentials]:
        with self._lock:
            return self._credentials

    @property
    def cred_generation(self) -> int:
        """Меняется при каждой смене учётных данных (нужно, чтобы не открывать лишних окон входа)."""
        with self._lock:
            return self._cred_generation

    def set_credentials(self, username: str, password: str) -> None:
        with self._lock:
            self._credentials = Credentials(username, password)
            self._cred_generation += 1

    @property
    def store_auth(self) -> bool:
        """False → svn не запоминает пароль в своём кэше (--no-auth-cache); читать кэш он продолжает."""
        with self._lock:
            return self._store_auth

    @store_auth.setter
    def store_auth(self, value: bool) -> None:
        with self._lock:
            self._store_auth = bool(value)

    # -- версия svn ---------------------------------------------------------

    def version(self) -> tuple[int, int]:
        with self._version_lock:
            if self._version is None:
                self._version = self._detect_version()
            return self._version

    def _detect_version(self) -> tuple[int, int]:
        try:
            proc = subprocess.run(
                self._base + ["--version", "--quiet"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                creationflags=CREATE_NO_WINDOW)
            match = re.search(r"(\d+)\.(\d+)", decode_output(proc.stdout))
            if match:
                return int(match.group(1)), int(match.group(2))
            log.warning("Не удалось определить версию svn по ответу: %r", decode_output(proc.stdout)[:200])
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("Не удалось определить версию svn: %s", exc)
        return (1, 10)  # неизвестна → считаем современной: пароль через stdin, а не в списке процессов

    # -- запуск ------------------------------------------------------------

    def _build_command(self, args: Sequence[str]):
        """Возвращает (команда, данные для stdin, команда для лога без пароля, были ли учётные данные)."""
        cmd = self._base + list(args) + ["--non-interactive"]
        shown = list(cmd)
        stdin_data = None
        if not self.store_auth:
            cmd.append("--no-auth-cache")
            shown.append("--no-auth-cache")
        creds = self.credentials
        if creds:
            cmd += ["--username", creds.username]
            shown += ["--username", creds.username]
            if self.version() >= (1, 10):
                cmd.append("--password-from-stdin")
                shown.append("--password-from-stdin")
                # без перевода строки: svn для Windows читает stdin до конца и оставляет одиночный
                # '\n' в пароле («Password incorrect»), отрезает только '\r\n'
                stdin_data = _encode_for_stdin(creds.password)
            else:
                cmd += ["--password", creds.password]
                shown += ["--password", "***"]
        return cmd, stdin_data, shown, creds is not None

    def _popen(self, cmd, stdin_data, merge_stderr: bool, cwd: Optional[str] = None) -> subprocess.Popen:
        try:
            return subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                cwd=cwd,
            )
        except OSError as exc:
            raise SvnError(ErrorKind.OTHER, f"Не удалось запустить svn: {exc}") from exc

    def run(self, args: Sequence[str], cancel: Optional[CancelToken] = None,
            timeout: Optional[float] = None, cwd: Optional[str] = None) -> bytes:
        """Запускает svn (в папке cwd, если задана), возвращает stdout (байты). При ошибке бросает SvnError."""
        cmd, stdin_data, shown, with_creds = self._build_command(args)
        log.info("svn: %s", _join_for_log(shown))
        proc = self._popen(cmd, stdin_data, merge_stderr=False, cwd=cwd)
        if cancel:
            cancel.attach(proc)
        deadline = time.monotonic() + timeout if timeout else None
        first = True
        try:
            while True:
                try:
                    out, err = proc.communicate(input=stdin_data if first else None, timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    if cancel and cancel.cancelled:
                        _kill(proc)
                        proc.communicate()
                        raise SvnError(ErrorKind.CANCELLED)
                    if deadline and time.monotonic() > deadline:
                        _kill(proc)
                        proc.communicate()
                        log.warning("svn не ответил за %s с: %s", timeout, _join_for_log(shown))
                        raise SvnError(ErrorKind.TIMEOUT, "Превышено время ожидания svn")
        finally:
            if cancel:
                cancel.detach(proc)
        if cancel and cancel.cancelled:
            raise SvnError(ErrorKind.CANCELLED)
        if proc.returncode != 0:
            text = decode_output(err)
            kind = classify_error(text, with_creds)
            log.warning("svn завершился с кодом %s (%s):\n%s", proc.returncode, kind.value, text)
            raise SvnError(kind, text, proc.returncode)
        return out

    # -- операции ------------------------------------------------------------

    def list_entries(self, url: str, recursive: bool = False,
                     cancel: Optional[CancelToken] = None,
                     timeout: Optional[float] = LIST_TIMEOUT) -> list[ListEntry]:
        args = ["list", "--xml"] + (["-R"] if recursive else []) + [url]
        return parse_list_xml(self.run(args, cancel, timeout))

    def list_dirs(self, url: str, cancel: Optional[CancelToken] = None) -> list[str]:
        """Имена подпапок (файлы не нужны)."""
        return [e.name for e in self.list_entries(url, cancel=cancel) if e.kind == "dir"]

    def estimate(self, url: str, cancel: Optional[CancelToken] = None) -> Estimate:
        """Считает файлы, папки и размер. Без общего таймаута: большой проект может считаться долго."""
        entries = self.list_entries(url, recursive=True, cancel=cancel, timeout=None)
        files = [e for e in entries if e.kind == "file"]
        return Estimate(files=len(files),
                        dirs=sum(1 for e in entries if e.kind == "dir"),
                        size=sum(e.size for e in files),
                        items=entries)

    def info(self, path: str) -> WcInfo:
        """Адрес и версия рабочей копии (локальная операция, сеть не нужна).

        '@' в конце пути защищает от разбора имени как peg-ревизии.
        Бросает SvnError (код E155007), если папка не является рабочей копией.
        """
        out = self.run(["info", "--xml", path + "@"], timeout=LIST_TIMEOUT)
        try:
            entry = ET.fromstring(out).find("entry")
            return WcInfo(url=entry.findtext("url") or "", revision=int(entry.get("revision")),
                          root=entry.findtext("repository/root") or "",
                          uuid=entry.findtext("repository/uuid") or "")
        except (ET.ParseError, AttributeError, TypeError, ValueError) as exc:
            raise SvnError(ErrorKind.OTHER, f"Не удалось разобрать ответ svn info: {exc}") from exc

    def relocate(self, path: str, new_url: str) -> None:
        """Перенацеливает рабочую копию на тот же проект по другому адресу (хранилище переехало или переименовано).

        Путь копии не передаётся аргументом: svn разбирает «@» в середине пути как номер версии (E155007),
        а у наших папок «@» в имени бывает; поэтому svn запускается в самой папке (`svn relocate URL`).
        new_url - закодированный адрес (Path.as_uri()). Сеть не нужна: меняется только запись о копии.
        """
        self.run(["relocate", new_url], timeout=LIST_TIMEOUT, cwd=path)

    def remote_lock(self, path: str) -> Optional[LockInfo]:
        """Чья блокировка стоит на файле на сервере (None, если файл свободен). Нужна сеть.

        Сначала адрес файла из рабочей копии, затем `svn info` по адресу: только там есть <lock>.
        """
        out = self.run(["info", "--xml", path + "@"], timeout=LIST_TIMEOUT)
        try:
            url = ET.fromstring(out).findtext("entry/url") or ""
        except ET.ParseError as exc:
            raise SvnError(ErrorKind.OTHER, f"Не удалось разобрать ответ svn info: {exc}") from exc
        if not url:
            return None
        out = self.run(["info", "--xml", url + "@"], timeout=LIST_TIMEOUT)
        try:
            lock = ET.fromstring(out).find("entry/lock")
        except ET.ParseError as exc:
            raise SvnError(ErrorKind.OTHER, f"Не удалось разобрать ответ svn info: {exc}") from exc
        return _lock_from_xml(lock, path) if lock is not None else None

    def info_revision(self, path: str) -> Optional[int]:
        try:
            return self.info(path).revision
        except SvnError:
            return None

    def status(self, path: str, check_server: bool = False,
               cancel: Optional[CancelToken] = None, verbose: bool = False,
               rules: Optional["FilterRules"] = None) -> WcStatus:
        """Свои правки; с check_server=True ещё и новое на сервере и блокировки (нужна сеть).

        verbose=True (`-v`) добавляет min_revision/max_revision/mixed: корень копии после Commit
        остаётся на старой версии, поэтому для копирования нужна max_revision (как у svnversion).

        local_changes: новые файлы-мусор (по rules, по умолчанию стандартные правила фильтра) в списке
        не показываются; счётчик unversioned остаётся как есть. rules.enabled=False - ничего не скрывать.
        """
        st = self._read_status(path, check_server, cancel, verbose)
        self._drop_junk(st, path, rules)
        return st

    def _read_status(self, path: str, check_server: bool, cancel: Optional[CancelToken],
                     verbose: bool) -> WcStatus:
        args = (["status", "--xml"] + (["-u"] if check_server else []) + (["-v"] if verbose else [])
                + [path + "@"])
        st = parse_status_xml(self.run(args, cancel, timeout=600))
        self._mark_needs_lock(st, path, cancel)
        return st

    def _mark_needs_lock(self, st: WcStatus, path: str, cancel: Optional[CancelToken]) -> None:
        """Отмечает needs_lock у правок существующих файлов (свойство читается локально: `svn propget -R`)."""
        if not any(c.kind in _NEEDS_LOCK_KINDS for c in st.local_changes):
            return
        marked = {_norm_path(p) for p in self.propget_needs_lock(path, cancel)}
        for change in st.local_changes:
            change.needs_lock = change.kind in _NEEDS_LOCK_KINDS and _norm_path(change.path) in marked

    def propget_needs_lock(self, path: str, cancel: Optional[CancelToken] = None) -> list[str]:
        """Файлы под путём рабочей копии с svn:needs-lock (`svn propget -R`, локально). Пути как напечатал svn."""
        out = self.run(["propget", "svn:needs-lock", "-R", "--xml", path + "@"], cancel, timeout=600)
        try:
            return [t.get("path", "") for t in ET.fromstring(out).iter("target")]
        except ET.ParseError as exc:
            raise SvnError(ErrorKind.OTHER, f"Не удалось разобрать XML от svn: {exc}") from exc

    @staticmethod
    def _drop_junk(st: WcStatus, path: str, rules: Optional["FilterRules"]) -> None:
        from .audit import junk_among   # отложенный импорт: audit сам импортирует svn_core
        from .config import FilterRules
        rules = rules or FilterRules()
        if not rules.enabled:
            return
        unversioned = [os.path.abspath(c.path) for c in st.local_changes if c.kind == "unversioned"]
        if not unversioned:
            return
        root = os.path.abspath(path)
        junk = {_norm_path(os.path.join(root, *rel.split("/"))) for rel in junk_among(unversioned, rules, root)}
        st.local_changes = [c for c in st.local_changes
                            if not (c.kind == "unversioned" and _is_under(_norm_path(c.path), junk))]

    def lock(self, path: str, comment: str = "Правка через SVN-lite") -> None:
        """Занимает файл (Get lock): остальные не смогут его менять, а сам файл становится доступным для записи."""
        self.run(["lock", "-m", comment, path + "@"], timeout=60)

    def unlock(self, path: str) -> None:
        """Освобождает файл (Release lock). Чужую блокировку без --force снять нельзя, и мы её не ломаем."""
        self.run(["unlock", path + "@"], timeout=60)

    def cleanup(self, path: str) -> None:
        self.run(["cleanup", path + "@"], timeout=300)  # без '@' путь с '@' даёт E200009

    RESOLVE_CHOICES = ("mine-full", "theirs-full")

    def resolve(self, path: str, accept: str) -> None:
        """Решает конфликт файла: accept="mine-full" - оставить свою версию, "theirs-full" - взять версию с сервера.

        Папка решается целиком (рекурсивно): все её конфликтные файлы получают один и тот же выбор.
        """
        if accept not in self.RESOLVE_CHOICES:
            raise ValueError(f"Неизвестный выбор при конфликте: {accept}")
        self.run(["resolve", "--accept", accept, "-R", path + "@"], timeout=120)

    def exclude_paths(self, dest: str, rel_paths: Sequence[str],
                      cancel: Optional[CancelToken] = None) -> None:
        """Убирает файлы и папки из рабочей копии (`update --set-depth exclude`).

        Исключённое не считается «потерянным» и не возвращается при следующем Update.
        Пути передаются аргументами партиями, чтобы не упереться в лимит командной строки.
        """
        batch: list[str] = []
        size = 0
        for rel in list(rel_paths) + [None]:
            if rel is not None:
                target = os.path.join(dest, *rel.split("/")) + "@"  # '@' защищает от peg-ревизии
                batch.append(target)
                size += len(target) + 1
                if size < 6000:
                    continue
            if batch:
                self.run(["update", "--set-depth", "exclude", *batch], cancel, timeout=300)
                batch, size = [], 0

    def _stream(self, args: Sequence[str], pattern, progress: CheckoutProgress,
                cancel: Optional[CancelToken], what: str, cwd: Optional[str] = None,
                timeout: Optional[float] = None) -> tuple[str, list]:
        """Запускает svn и читает вывод построчно. Возвращает (прочие строки, [(действие, путь)]).

        timeout - сколько секунд svn может ничего не печатать; после этого процесс убивается и
        бросается SvnError(TIMEOUT). Отмена (cancel) убивает процесс сразу -> SvnError(CANCELLED).
        """
        cmd, stdin_data, shown, with_creds = self._build_command(args)
        log.info("svn: %s%s", _join_for_log(shown), f" (в папке {cwd})" if cwd else "")
        proc = self._popen(cmd, stdin_data, merge_stderr=True, cwd=cwd)
        if cancel:
            cancel.attach(proc)
        other_lines: list[str] = []
        matches: list[tuple[str, str]] = []
        finished = threading.Event()
        timed_out = threading.Event()
        last_output = [time.monotonic()]
        if timeout:
            def watchdog() -> None:
                while not finished.wait(0.25):
                    if time.monotonic() - last_output[0] > timeout:
                        timed_out.set()
                        _kill(proc)
                        return
            threading.Thread(target=watchdog, daemon=True).start()
        try:
            if stdin_data:
                try:
                    proc.stdin.write(stdin_data)
                    proc.stdin.close()
                except OSError:
                    pass  # svn уже завершился; причину покажет его вывод
            for raw in iter(proc.stdout.readline, b""):
                last_output[0] = time.monotonic()
                line = decode_output(raw).rstrip("\r\n")
                match = pattern.match(line)
                if match:
                    action = match.group(1).strip() if pattern is _ACTION_RE else "A"
                    path = match.group(pattern.groups)
                    matches.append((action, path))
                    progress.last_path = path
                    progress.count += 1
                elif line.strip():
                    other_lines.append(line)
            proc.wait()
        finally:
            finished.set()
            if cancel:
                cancel.detach(proc)
            if proc.stdout:
                proc.stdout.close()
        output = "\n".join(other_lines)
        if cancel and cancel.cancelled:
            log.info("%s отменён после %d записей", what, progress.count)
            raise SvnError(ErrorKind.CANCELLED)
        if timed_out.is_set():
            log.warning("%s: svn молчит больше %s с, прервано после %d записей", what, timeout, progress.count)
            raise SvnError(ErrorKind.TIMEOUT, "Превышено время ожидания svn")
        if proc.returncode != 0:
            kind = classify_error(output, with_creds)
            log.error("%s завершился с кодом %s (%s), записей: %d\n%s",
                      what, proc.returncode, kind.value, progress.count, output)
            raise SvnError(kind, output, proc.returncode)
        log.info("%s завершён, записей: %d\n%s", what, progress.count, output)
        return output, matches

    def checkout(self, url: str, dest: str, progress: Optional[CheckoutProgress] = None,
                 cancel: Optional[CancelToken] = None, force: bool = False,
                 timeout: Optional[float] = STREAM_IDLE_TIMEOUT) -> CheckoutResult:
        """Скачивает рабочую копию. force=True (`--force`) привязывает уже существующую папку на месте:
        лежавшие в ней файлы svn оставляет как есть (строки «E», они входят в прогресс), а расходящиеся
        с сервером показывает как изменённые.

        '@' в конце url нужен, чтобы '@' внутри имени не приняли за peg-ревизию. Папка назначения передаётся
        как «.» из рабочего каталога dest: путь вида «a @1» svn искажает в «a@1» (отрезает пробел перед '@'),
        а конечный '@' в имени папки оставляет буквально (проверено на svn 1.14).
        """
        progress = progress or CheckoutProgress()
        created = not os.path.isdir(dest)
        os.makedirs(dest, exist_ok=True)
        args = ["checkout", "--depth", "infinity", "-r", "HEAD"] + (["--force"] if force else []) + [url + "@", "."]
        try:
            output, _ = self._stream(args, _CHECKOUT_RE, progress, cancel, "checkout", cwd=dest, timeout=timeout)
        except SvnError:
            if created:
                try:
                    os.rmdir(dest)   # svn до записи ничего не успел: пустую папку, созданную нами, убираем
                except OSError:
                    pass
            raise
        revision = None
        try:
            revision = self.info(dest).revision
        except SvnError:
            match = re.search(r"(\d+)\.?\s*$", output)
            revision = int(match.group(1)) if match else None
        return CheckoutResult(revision=revision, entries=progress.count)

    def export(self, url: str, dest: str, progress: Optional[CheckoutProgress] = None,
               cancel: Optional[CancelToken] = None, revision: Optional[int] = None,
               timeout: Optional[float] = STREAM_IDLE_TIMEOUT) -> CheckoutResult:
        """Копия папки с сервера без служебных данных svn (`svn export`). dest не должен существовать.

        --ignore-keywords: svn:keywords не раскрываются, содержимое файлов остаётся побайтово как на сервере;
        --ignore-externals: внешние ссылки не подтягиваются. url передаётся закодированным (build_url, Path.as_uri):
        '@' в конце нужен, чтобы '@' внутри имени не приняли за peg-ревизию. dest тоже заканчивается на '@'
        (export разбирает peg и в локальном пути; конечный '@' svn отбрасывает).
        """
        progress = progress or CheckoutProgress()
        rev = str(revision) if revision else ""
        args = ["export", "--ignore-keywords", "--ignore-externals", url + "@" + rev, dest + "@"]
        output, _ = self._stream(args, _CHECKOUT_RE, progress, cancel, "export", timeout=timeout)
        match = re.search(r"(\d+)\.?\s*$", output)
        return CheckoutResult(revision=int(match.group(1)) if match else None, entries=progress.count)

    def cat(self, url: str, revision: Optional[int] = None, cancel: Optional[CancelToken] = None,
            timeout: Optional[float] = LIST_TIMEOUT) -> bytes:
        """Содержимое одного файла с сервера (`svn cat`), байтами. url закодирован, '@' в конце добавляется здесь."""
        return self.run(["cat", url + "@" + (str(revision) if revision else "")], cancel, timeout)

    def has_connection(self, url: str, cancel: Optional[CancelToken] = None,
                       timeout: float = CONNECT_TIMEOUT) -> bool:
        """Есть ли связь с сервером: успешный листинг url за timeout секунд.

        Нет связи или нет ответа -> False; прочие ошибки (вход, права, нет папки) пробрасываются:
        связь при них есть, а причину надо показать пользователю.
        """
        try:
            self.list_entries(url + "@", cancel=cancel, timeout=timeout)
            return True
        except SvnError as exc:
            if exc.kind in (ErrorKind.NO_CONNECTION, ErrorKind.TIMEOUT):
                return False
            raise

    def update(self, path: str, progress: Optional[CheckoutProgress] = None,
               cancel: Optional[CancelToken] = None) -> UpdateResult:
        """Получает свежие изменения с сервера. Конфликты не разрешает: оставляет их пользователю."""
        progress = progress or CheckoutProgress()
        output, matches = self._stream(["update", "--accept", "postpone", path + "@"],
                                       _ACTION_RE, progress, cancel, "update")
        return self._update_result(path, matches)

    def _update_result(self, path: str, matches) -> UpdateResult:
        result = UpdateResult(revision=self.info_revision(path))
        for action, item_path in matches:
            if "C" in action:
                result.conflicts.append(item_path)
            elif action.startswith("A"):
                result.added += 1
                result.added_paths.append(item_path)
            elif action.startswith("D"):
                result.deleted += 1
            else:
                result.updated += 1
        return result

    # -- сохранение на сервер и сведения о новом -----------------------------------

    def commit(self, path: str, files: Sequence[str], message: str,
               cancel: Optional[CancelToken] = None) -> CommitResult:
        """Сохраняет выбранные правки на сервер (`svn commit`). files - пути из LocalChange.path.

        Новые файлы (unversioned) добавляются (`svn add --parents`), потерянные (missing) удаляются
        (`svn rm --keep-local`). Комментарий и список файлов передаются через временные файлы в UTF-8
        (кириллица, длинные списки; комментарий в UTF-8, список путей в кодировке системы). Конфликты и новое на сервере не разрешаются:
        OUT_OF_DATE, READ_ONLY_LINE и прочие ошибки приходят как SvnError.

        Файл с svn:needs-lock, который вы не заняли, -> SvnError(LOCK_REQUIRED) ДО любых действий;
        в raw пути таких файлов, по одному в строке (сам сервер этого не требует, поэтому проверка здесь).
        """
        st = self._read_status(path, False, cancel, False)
        known = {_norm_path(c.path): c for c in st.local_changes}
        chosen = [(f, known.get(_norm_path(f))) for f in files]
        need_lock = [f for f, c in chosen if c and c.needs_lock and not c.locked_by_me]
        if need_lock:
            raise SvnError(ErrorKind.LOCK_REQUIRED, "\n".join(need_lock))
        to_add = [f for f, c in chosen if c and c.kind == "unversioned"]
        to_remove = [f for f, c in chosen if c and c.kind == "missing"]
        targets = list(files)
        # новые родительские папки тоже должны попасть в commit, иначе svn его отклонит
        for f, c in chosen:
            if c and c.kind in ("unversioned", "added"):
                targets += self._new_parents(f, path, known)
        with tempfile.TemporaryDirectory(prefix="svnhelper_") as tmp:
            if to_add:
                self.run(["add", "--parents", *_targets_args(tmp, "add.txt", to_add)], cancel, timeout=600)
            if to_remove:
                self.run(["rm", "--keep-local", *_targets_args(tmp, "rm.txt", to_remove)], cancel, timeout=600)
            message_file = os.path.join(tmp, "message.txt")
            with open(message_file, "w", encoding="utf-8", newline="") as fh:
                fh.write(message)
            output, matches = self._stream(
                ["commit", "--encoding", "UTF-8", "--file", message_file,
                 *_targets_args(tmp, "commit.txt", list(dict.fromkeys(targets)))],
                _IMPORT_RE, CheckoutProgress(), cancel, "commit")
        match = re.search(r"(\d+)\.\s*$", output)
        return CommitResult(revision=int(match.group(1)) if match else None,
                            committed=[p for _action, p in matches])

    @staticmethod
    def _new_parents(file: str, wc_root: str, known: dict) -> list[str]:
        """Папки между файлом и корнем копии, которые тоже новые (unversioned или уже added)."""
        root = _norm_path(wc_root)
        found: list[str] = []
        current = os.path.dirname(file)
        while current and _norm_path(current).startswith(root + os.sep):
            change = known.get(_norm_path(current))
            if change and change.kind in ("unversioned", "added"):
                found.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return found

    def incoming_log(self, path: str, cancel: Optional[CancelToken] = None) -> list[LogEntry]:
        """Что появилось на сервере после вашей копии: `svn log -r <BASE+1>:HEAD -v --xml`.

        BASE - наибольшая версия записей копии (как у svnversion), а не версия корня: после
        сохранения части файлов корень остаётся старым, и свои же правки показались бы «новыми».
        Записи в порядке svn для диапазона вверх: старые первыми. Нового нет -> [].
        """
        st = self._read_status(path, False, cancel, True)
        base = st.max_revision if st.max_revision is not None else self.info(path).revision
        args = ["log", "--xml", "-v", "-r", f"{base + 1}:HEAD", path + "@"]
        try:
            return parse_log_xml(self.run(args, cancel, timeout=300))
        except SvnError as exc:
            if "E160006" in exc.codes:   # BASE+1 больше HEAD: на сервере ничего нового
                return []
            raise

    def recent_log(self, path: str, limit: int = 50, cancel: Optional[CancelToken] = None) -> list[LogEntry]:
        """Последние `limit` сохранений рабочей копии (`svn log -v -l N`), новые первыми: кто, когда, комментарий, файлы."""
        args = ["log", "--xml", "-v", "-l", str(limit), path + "@"]
        return parse_log_xml(self.run(args, cancel, timeout=300))

    def diff_summary(self, url_a: str, url_b: str,
                     cancel: Optional[CancelToken] = None) -> list[tuple[str, str]]:
        """Чем отличаются две линии: `svn diff --summarize --xml` -> [(путь, вид)].

        Вид: added | deleted | modified (item у svn). Путь относительно url_a (или url_b для новых
        файлов); если svn вернул адрес вне них, путь остаётся адресом как есть.
        """
        out = self.run(["diff", "--summarize", "--xml", url_a + "@", url_b + "@"], cancel, timeout=300)
        try:
            root = ET.fromstring(out)
        except ET.ParseError as exc:
            raise SvnError(ErrorKind.OTHER, f"Не удалось разобрать XML от svn: {exc}") from exc
        prefixes = [unquote(u).rstrip("/") + "/" for u in (url_a, url_b)]
        result = []
        for item in root.iter("path"):
            text = unquote((item.text or "").strip())   # svn печатает адреса в %-кодировке
            for prefix in prefixes:
                if text.startswith(prefix):
                    text = text[len(prefix):]
                    break
            result.append((text, item.get("item", "")))
        return result

    # -- ветки и версии (теги) -----------------------------------------------

    def list_lines(self, project_url: str, cancel: Optional[CancelToken] = None) -> dict:
        """Ветки и версии проекта: {"branches": [LineEntry], "tags": [LineEntry], "has_trunk": bool}.

        project_url — папка проекта (где лежат trunk/branches/tags). Нет папки branches
        или tags — это пустой список, не ошибка. Новые линии первыми.
        """
        base = project_url.rstrip("/")
        top = {e.name for e in self.list_entries(base + "@", cancel=cancel) if e.kind == "dir"}
        result = {"branches": [], "tags": [], "has_trunk": "trunk" in top}
        for key in ("branches", "tags"):
            if key not in top:
                continue
            try:
                entries = self.list_entries(f"{base}/{key}@", cancel=cancel)
            except SvnError as exc:
                if exc.kind == ErrorKind.NOT_FOUND:
                    continue
                raise
            lines = [LineEntry(e.name, e.author, e.date, e.revision) for e in entries if e.kind == "dir"]
            lines.sort(key=lambda l: l.last_date or _MIN_DATE, reverse=True)
            result[key] = lines
        return result

    def branch_history(self, url: str, cancel: Optional[CancelToken] = None,
                       limit: Optional[int] = None) -> BranchHistory:
        """Сводка по линии: создатель и откуда, авторы правок, последняя правка.

        Два лёгких запроса вместо `log -v` по всей истории:
        1) `log -q --stop-on-copy [-l N]` - авторы и даты (без путей);
        2) `log -v -r 0:HEAD -l 1 --stop-on-copy` - ровно одна запись создания линии
           (прямой диапазон со stop-on-copy возвращает именно её; проверено на svn 1.14).
        limit обрезает только список авторов (complete=False); создатель определяется всегда.
        """
        quiet = ["log", "--xml", "-q", "--stop-on-copy"] + (["-l", str(limit)] if limit else []) + [url + "@"]
        entries = parse_log_xml(self.run(quiet, cancel, timeout=300))
        first = ["log", "--xml", "-v", "--stop-on-copy", "-r", "0:HEAD", "-l", "1", url + "@"]
        created = parse_log_xml(self.run(first, cancel, timeout=300))
        return build_branch_history(entries, complete=not (limit and len(entries) >= limit),
                                    created=created[0] if created else None)

    def locks_under(self, url: str, cancel: Optional[CancelToken] = None) -> list[LockInfo]:
        """Занятые файлы под адресом на сервере (<lock> приходит в `svn list -R --xml`)."""
        return parse_locks_xml(self.run(["list", "-R", "--xml", url + "@"], cancel, timeout=300))

    def url_exists(self, url: str) -> bool:
        """Есть ли такой путь на сервере (E170000/E160013 — нет; остальные ошибки пробрасываются)."""
        try:
            self.run(["info", "--xml", url + "@"], timeout=LIST_TIMEOUT)
            return True
        except SvnError as exc:
            if exc.codes & {"E170000", "W170000", "E160013", "W160013"}:
                return False
            raise

    def copy_url(self, src_url: str, dst_url: str, message: str,
                 revision: Optional[int] = None) -> None:
        """Серверное копирование (создание ветки или версии): `svn copy src@rev dst -m msg`.

        Недостающие промежуточные папки (например, tags/) создаются сами (--parents).
        Если dst_url уже есть, svn молча положил бы копию ВНУТРЬ него, поэтому наличие
        проверяется заранее и бросается SvnError(EXISTS). Между проверкой url_exists и самим
        copy есть гонка: если путь создадут в этот промежуток, svn положит копию внутрь него.
        Кириллица и переводы строк
        в message доходят через -m без искажений (проверено на svn 1.14, Windows).
        """
        if self.url_exists(dst_url):
            raise SvnError(ErrorKind.EXISTS, "svn: E160020: Path already exists: " + dst_url)
        src = src_url + "@" + (str(revision) if revision else "")
        self.run(["copy", "--parents", "-m", message, src, dst_url], timeout=300)

    # -- создание проекта из папки -------------------------------------------------

    def mkdir_urls(self, urls: Sequence[str], message: str) -> None:
        """Создаёт папки на сервере одной ревизией (`svn mkdir --parents`).

        URL передаются закодированными (build_url): '@' внутри имени приходит как %40,
        поэтому '@' в конце не нужен.
        """
        self.run(["mkdir", "--parents", "-m", message, *urls], timeout=300)

    def last_log_entry(self, url: str, limit: int = 1) -> list[LogEntry]:
        """Последние записи журнала по адресу (новые первыми), без путей."""
        return parse_log_xml(self.run(["log", "--xml", "-q", "-l", str(limit), url + "@"], timeout=LIST_TIMEOUT))

    def remove_url(self, url: str, message: str) -> None:
        """Удаляет папку на сервере. '@' в конце обязателен: svn раскодирует %40 и иначе примет его за ревизию."""
        self.run(["delete", "-m", message, url + "@"], timeout=300)

    def import_dir(self, src: str, url: str, message: str, ignores: Sequence[str],
                   progress: Optional[CheckoutProgress] = None,
                   cancel: Optional[CancelToken] = None) -> int:
        """Загружает папку src на сервер одной ревизией (`svn import`). Возвращает число загруженных записей.

        Исходная папка не меняется (import не создаёт в ней .svn). Мусор отсеивается шаблонами имён
        ignores через --config-option (заменяют стандартный global-ignores svn).
        '@' к адресу НЕ добавляется: import не разбирает peg-ревизию и создал бы папку «trunk@».
        Папка передаётся как «.» из рабочего каталога src: путь «a @ b» svn import искажает в «a@ b»,
        а '@' в конце не помогает (проверено на svn 1.14).
        """
        progress = progress or CheckoutProgress()
        option = "config:miscellany:global-ignores=" + " ".join(ignores)
        self._stream(["import", "--config-option", option, "-m", message, ".", url],
                     _IMPORT_RE, progress, cancel, "import", cwd=src)
        return progress.count

    def switch(self, path: str, url: str, progress: Optional[CheckoutProgress] = None,
               cancel: Optional[CancelToken] = None) -> UpdateResult:
        """Переключает рабочую копию на другую линию. Конфликты не разрешает.

        Синтаксис проверен на svn 1.14: `switch URL@ PATH` - '@' нужен у URL, а путь
        рабочей копии передаётся БЕЗ '@' (svn берёт его буквально; `PATH@` даёт E155007).
        Несвязанные линии -> ErrorKind.UNRELATED (E195012); --ignore-ancestry не используется.
        """
        progress = progress or CheckoutProgress()
        _output, matches = self._stream(["switch", "--accept", "postpone", url + "@", path],
                                        _ACTION_RE, progress, cancel, "switch")
        return self._update_result(path, matches)


def _join_for_log(args: Sequence[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in args)
