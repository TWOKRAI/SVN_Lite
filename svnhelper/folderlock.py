"""Межпроцессный замок папки проекта: с папкой работает один процесс (окно, svnlite.exe, MCP), остальные видят её
занятой. Без tkinter.

Файл замка `data_dir()/locks/<ключ>.lock`, ключ - первые 16 знаков sha1 от `normcase(realpath(папка))`: путь с
другим регистром, косыми чертами, концевой чертой или через junction даёт тот же ключ. Замок - блокировка
`msvcrt.locking` первого байта файла (байт 0 после `seek(0)`); Windows снимает её сама, когда процесс завершается
или его убивают, поэтому «брошенных» замков не бывает. Сведения о владельце (pid, время старта процесса, кто и
когда занял) лежат JSON со смещения 64 - вне блокируемого байта, чтобы другой процесс мог их прочитать
(`holder_info`).

Блокировки двух дескрипторов одного процесса тоже конфликтуют, поэтому у процесса есть реестр своих замков:
`held_by_other` не принимает собственный замок за чужой, а второй замок на ту же папку в этом же процессе не
берётся (`try_lock` -> None, `hold` -> FolderBusy). Замок не реентерабельный: на один путь в процессе берут его
одним способом (`publish.mark_busy` или `hold`), не двумя.

Проверка `held_by_other` пробует занять байт на мгновение. Если в этот миг другой процесс берёт замок, он
повторяет попытку (`ACQUIRE_ATTEMPTS`), поэтому проверки окна не мешают агенту.

Отпущенный замок удаляет свой файл (только Windows: файл, открытый другим процессом, удалить нельзя, и это
безопасно). Остатки (проверка держала файл, процесс убит) старше 7 дней удаляются при первом обращении процесса к
каталогу замков (`cleanup_stale`).

Каталог замков недоступен: в обычном режиме (окно) работа идёт без межпроцессного замка, в журнал пишется
предупреждение; в строгом режиме (`set_strict(True)`: консольная программа и MCP перед изменяющими операциями)
бросается LockUnavailable, и операция отказывает.

`named_lock(name)` - блокирующий замок общих данных (настройки, счётчики, аудит) по имени, с ожиданием.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from . import jobs, projectmeta

try:
    import msvcrt
except ImportError:          # не Windows: замок через fcntl (без удаления файла при отпускании)
    msvcrt = None
    import fcntl

log = logging.getLogger("svnhelper.folderlock")

LOCKS_DIR_NAME = "locks"
LOCK_SUFFIX = ".lock"
KEY_LENGTH = 16
INFO_OFFSET = 64                # сведения о владельце: за блокируемым байтом 0
INFO_MAX_BYTES = 4096
STALE_DAYS = 7
ACQUIRE_ATTEMPTS = 3            # попыток занять байт: чужая проверка держит его лишь мгновение
ACQUIRE_DELAY = 0.02            # секунд между попытками (растёт: 0.02, 0.04)
OPEN_ATTEMPTS = 4               # открыть файл, который антивирус или удаление держат миг
NAMED_POLL = 0.05               # секунд между попытками named_lock
DEFAULT_TIMEOUT = 10.0
NAMED_PREFIX = "named-"
_NAME_RE = re.compile(r"[a-z0-9_]{1,40}")
_O_BINARY = getattr(os, "O_BINARY", 0)

PathLike = Union[str, os.PathLike]

BUSY_TEXT = "Папку проекта сейчас обрабатывает другая программа. Подождите и повторите."
SAME_PROCESS_TEXT = "С этой папкой уже идёт другая работа. Дождитесь её окончания."
UNAVAILABLE_TEXT = "Не удалось проверить, свободна ли папка: служебная папка программы недоступна."
TIMEOUT_TEXT = "Общие данные программы сейчас заняты другой программой. Повторите через несколько секунд."


class FolderLockError(Exception):
    """Замок папки не взят."""


class FolderBusy(FolderLockError):
    """Папку держит другой процесс (holder: сведения о нём, если прочитались) или другая работа этого процесса."""

    def __init__(self, path: str, holder: Optional[dict] = None, same_process: bool = False):
        super().__init__(SAME_PROCESS_TEXT if same_process else BUSY_TEXT)
        self.path = path
        self.holder = holder
        self.same_process = same_process


class LockUnavailable(FolderLockError):
    """Каталог или файл замка недоступен (строгий режим)."""

    def __init__(self, path: str, error: Optional[BaseException] = None):
        super().__init__(UNAVAILABLE_TEXT)
        self.path = path
        self.error = error


class LockTimeout(FolderLockError):
    """named_lock не дождался своей очереди."""

    def __init__(self, name: str, timeout: float):
        super().__init__(TIMEOUT_TEXT)
        self.name = name
        self.timeout = timeout


# --------------------------------------------------------------------------
# Режим процесса
# --------------------------------------------------------------------------

_strict = False


def set_strict(value: bool) -> None:
    """Строгий режим процесса: без каталога замков работа не идёт (консольная программа, MCP)."""
    global _strict
    _strict = bool(value)


def is_strict() -> bool:
    return _strict


# --------------------------------------------------------------------------
# Ключ и файлы
# --------------------------------------------------------------------------

def canonical_path(path: PathLike) -> str:
    """Один вид пути для одной папки: realpath (junction, ссылки, короткие имена), без `\\\\?\\`, normcase."""
    path = os.fspath(path)
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        real = os.path.abspath(path)
    if real.startswith("\\\\?\\UNC\\"):
        real = "\\\\" + real[8:]
    elif real.startswith("\\\\?\\"):
        real = real[4:]
    return os.path.normcase(os.path.normpath(real))


def lock_key(path: PathLike) -> str:
    return hashlib.sha1(canonical_path(path).encode("utf-8")).hexdigest()[:KEY_LENGTH]


def locks_dir() -> Path:
    from .config import data_dir     # отложенно: учитывает SVNLITE_DATA_DIR на момент вызова
    return data_dir() / LOCKS_DIR_NAME


def lock_file(path: PathLike) -> Path:
    """Файл замка папки (может не существовать)."""
    return locks_dir() / f"{lock_key(path)}{LOCK_SUFFIX}"


_cleaned: set[str] = set()          # каталоги замков, где этот процесс уже убрал старые файлы
_cleaned_lock = threading.Lock()


def _ensure_dir() -> Path:
    """Каталог замков (создаётся); при первом обращении процесса старые файлы удаляются. Бросает OSError."""
    folder = locks_dir()
    folder.mkdir(parents=True, exist_ok=True)
    marker = os.path.normcase(str(folder))
    with _cleaned_lock:
        first = marker not in _cleaned
        _cleaned.add(marker)
    if first:
        cleanup_stale(folder=folder)
    return folder


def cleanup_stale(max_age_days: float = STALE_DAYS, folder: Optional[PathLike] = None) -> int:
    """Удаляет файлы замков старше max_age_days. Занятый файл удалить нельзя, он остаётся. Возвращает число
    удалённых. Сбои записываются в журнал программы."""
    folder = Path(folder) if folder is not None else locks_dir()
    if not folder.is_dir():
        return 0
    border = time.time() - max_age_days * 86400
    removed = 0
    for file in folder.glob(f"*{LOCK_SUFFIX}"):
        try:
            if file.stat().st_mtime >= border:
                continue
            if msvcrt is None and _probe_busy(file):
                continue                    # вне Windows удаление не мешает держателю: проверяем сами
            os.remove(file)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.debug("Старый файл замка %s не удалён: %s", file, exc)
    if removed:
        log.info("Удалено старых файлов замков: %d (%s)", removed, folder)
    return removed


# --------------------------------------------------------------------------
# Блокировка байта 0
# --------------------------------------------------------------------------

def _os_lock(fd: int) -> bool:
    """Пробует занять байт 0 без ожидания. True: занят нами."""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if msvcrt is not None:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
            log.warning("Замок не взят из-за неожиданной ошибки: %s", exc)
        return False
    return True


def _os_unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if msvcrt is not None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.lockf(fd, fcntl.LOCK_UN, 1, 0)


def _open(file: Path, create: bool) -> int:
    """Дескриптор файла замка. Файл на миг занят (удаляется, его читает антивирус) -> ещё попытки."""
    flags = os.O_RDWR | _O_BINARY | (os.O_CREAT if create else 0)
    for attempt in range(1, OPEN_ATTEMPTS + 1):
        try:
            return os.open(file, flags, 0o666)
        except PermissionError:
            if attempt == OPEN_ATTEMPTS:
                raise
            time.sleep(ACQUIRE_DELAY * attempt)
    raise AssertionError("недостижимо")


def _probe_busy(file: Path) -> bool:
    """Держит ли кто-то файл замка: пробная блокировка на мгновение. Файла нет или он не открывается -> False."""
    try:
        fd = os.open(file, os.O_RDWR | _O_BINARY)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.debug("Файл замка %s не открыт для проверки: %s", file, exc)
        return False
    try:
        if _os_lock(fd):
            _os_unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


def _write_info(fd: int, info: dict) -> None:
    data = json.dumps(info, ensure_ascii=False).encode("utf-8")[:INFO_MAX_BYTES]
    os.lseek(fd, INFO_OFFSET, os.SEEK_SET)
    os.write(fd, data)
    os.ftruncate(fd, INFO_OFFSET + len(data))


def _read_info(file: Path) -> dict:
    """Сведения о владельце из чужого файла замка; не читаются (ещё не записаны, файл исчез) -> {}."""
    try:
        fd = os.open(file, os.O_RDONLY | _O_BINARY)
    except OSError:
        return {}
    try:
        os.lseek(fd, INFO_OFFSET, os.SEEK_SET)
        data = os.read(fd, INFO_MAX_BYTES)
    except OSError:
        return {}
    finally:
        os.close(fd)
    try:
        info = json.loads(data.decode("utf-8"))
    except ValueError:
        return {}
    return info if isinstance(info, dict) else {}


def _program() -> str:
    return os.path.basename(sys.executable if getattr(sys, "frozen", False) else (sys.argv[0] or sys.executable))


# --------------------------------------------------------------------------
# Замки этого процесса
# --------------------------------------------------------------------------

class FolderLock:
    """Взятый замок. locked=False: каталог замков недоступен, замок только внутри процесса (обычный режим)."""

    def __init__(self, key: str, target: str, fd: Optional[int], file: Optional[Path], info: dict):
        self.key = key
        self.target = target            # папка (или имя named_lock)
        self.file = file
        self.info = info
        self._fd = fd
        self._released = False

    @property
    def locked(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """Отпускает замок (повторный вызов ничего не делает); свой файл замка удаляется, если он никем не открыт."""
        with _held_lock:
            if self._released:
                return
            self._released = True
        try:
            if self._fd is not None:
                try:
                    _os_unlock(self._fd)
                except OSError as exc:
                    log.warning("Замок %s не снят явно (снимется при закрытии): %s", self.target, exc)
                os.close(self._fd)
                self._fd = None
                if msvcrt is not None and self.file is not None:
                    try:
                        os.remove(self.file)       # открыт другим процессом -> не удалится, это нормально
                    except OSError:
                        pass
        finally:
            with _held_lock:
                if _held.get(self.key) is self:
                    del _held[self.key]

    def __enter__(self) -> "FolderLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# ключ -> взятый замок; None: замок сейчас берётся (место занято, чтобы два потока не взяли его вместе)
_held: dict[str, Optional[FolderLock]] = {}
_held_lock = threading.Lock()


def _acquire(key: str, target: str, owner: str, strict: Optional[bool]) -> Optional[FolderLock]:
    """Замок по ключу. None: держит другой процесс или уже держит этот. LockUnavailable: строгий режим без каталога."""
    with _held_lock:
        if key in _held:
            return None
        _held[key] = None
    lock = None
    try:
        lock = _acquire_file(key, target, owner, _strict if strict is None else strict)
        return lock
    finally:
        with _held_lock:
            if lock is None:
                _held.pop(key, None)
            else:
                _held[key] = lock


def _acquire_file(key: str, target: str, owner: str, strict: bool) -> Optional[FolderLock]:
    info = dict(jobs.owner(), owner=owner, program=_program(), path=target, since=projectmeta.now_iso())
    file = None
    try:
        file = _ensure_dir() / f"{key}{LOCK_SUFFIX}"
        fd = _open(file, create=True)
    except OSError as exc:
        if strict:
            raise LockUnavailable(target, exc) from exc
        log.warning("Каталог замков недоступен (%s): «%s» занята только внутри этой программы", exc, target)
        return FolderLock(key, target, None, None, info)
    for attempt in range(1, ACQUIRE_ATTEMPTS + 1):
        if _os_lock(fd):
            break
        if attempt == ACQUIRE_ATTEMPTS:
            os.close(fd)
            return None
        time.sleep(ACQUIRE_DELAY * attempt)
    try:
        _write_info(fd, info)
    except OSError as exc:              # замок взят, просто другие не узнают владельца
        log.warning("Сведения о владельце замка %s не записаны: %s", file, exc)
    return FolderLock(key, target, fd, file, info)


def held_here(path: PathLike) -> bool:
    """Держит ли эту папку сам этот процесс."""
    key = lock_key(path)
    with _held_lock:
        return key in _held


# --------------------------------------------------------------------------
# Публичные операции
# --------------------------------------------------------------------------

def try_lock(path: PathLike, owner: str = "", strict: Optional[bool] = None) -> Optional[FolderLock]:
    """Занимает папку без ожидания. None: её держит другой процесс или уже этот. Отпускать: release() или with.

    strict=None: режим процесса (set_strict). LockUnavailable: строгий режим и каталог замков недоступен."""
    return _acquire(lock_key(path), os.fspath(path), owner, strict)


@contextmanager
def hold(path: PathLike, owner: str = "", strict: Optional[bool] = None) -> Iterator[FolderLock]:
    """`with hold(папка):` - папка занята до выхода из блока. FolderBusy: её держит другой процесс или этот же."""
    same_process = held_here(path)
    lock = try_lock(path, owner, strict)
    if lock is None:
        same_process = same_process or held_here(path)
        holder = holder_info(path)
        raise FolderBusy(os.fspath(path), holder, same_process)
    with lock:
        yield lock


def _no_folder_locks() -> bool:
    """В каталоге замков нет ни одного файла замка папки (обычное дело: отпущенный замок удаляет свой файл).
    Тогда путь не разбирается: realpath папки на отключённом сетевом диске мог бы надолго задержать окно."""
    try:
        with os.scandir(locks_dir()) as entries:
            return not any(e.name.endswith(LOCK_SUFFIX) and not e.name.startswith(NAMED_PREFIX) for e in entries)
    except OSError:
        return True


def held_by_other(path: PathLike) -> bool:
    """Держит ли папку другой процесс. Собственный замок (hold, mark_busy) чужим не считается. Файлы не создаёт."""
    if _no_folder_locks():
        return False
    key = lock_key(path)
    with _held_lock:
        if key in _held:
            return False
    return _probe_busy(locks_dir() / f"{key}{LOCK_SUFFIX}")


def holder_info(path: PathLike) -> Optional[dict]:
    """Кто держит папку: {pid, started, owner, program, path, since}. None: никто. {}: держат, но сведения не
    прочитались (владелец только что занял папку и ещё не записал их)."""
    key = lock_key(path)
    with _held_lock:
        if key in _held:
            lock = _held[key]
            return dict(lock.info) if lock is not None else {}
    file = locks_dir() / f"{key}{LOCK_SUFFIX}"
    if not _probe_busy(file):
        return None
    return _read_info(file)


@contextmanager
def named_lock(name: str, timeout: float = DEFAULT_TIMEOUT, strict: Optional[bool] = None) -> Iterator[FolderLock]:
    """Блокирующий замок общих данных по имени (`settings`, `audit`...): ждёт до timeout секунд, затем LockTimeout.

    Работает и между потоками одного процесса. Не реентерабельный: повторный вход в том же потоке ждёт до timeout."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"недопустимое имя замка: {name!r}")
    key = NAMED_PREFIX + name
    deadline = time.monotonic() + timeout
    while True:
        lock = _acquire(key, name, name, strict)
        if lock is not None:
            break
        if time.monotonic() >= deadline:
            raise LockTimeout(name, timeout)
        time.sleep(NAMED_POLL)
    with lock:
        yield lock
