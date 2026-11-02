"""Создание проекта в хранилище из папки на диске: просмотр папки, загрузка, откат. Без tkinter.

Порядок (проще и атомарнее плана из PLAN_create_project.md):
1. `svn mkdir --parents` trunk, branches, tags одной ревизией (R1);
2. `svn import <папка> <проект>/trunk` одной ревизией, мусор отсеивается шаблонами имён;
3. при сбое или отмене: `svn delete <проект>`, только если после R1 в проекте нет других ревизий и R1 наша.
Исходная папка только читается: import не создаёт в ней .svn и ничего не меняет.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import stat
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from . import server
from .config import FilterRules
from .svn_core import CancelToken, CheckoutProgress, ErrorKind, LogEntry, SvnClient, SvnError

log = logging.getLogger("svnhelper.importer")

GB = 1024 ** 3
LARGE_IMPORT_BYTES = 5 * GB       # больше этого: предупреждение «очень большой объём»
LARGE_IMPORT_FILES = 50_000
# служебные папки других программ контроля версий: в хранилище их не кладём никогда
ALWAYS_SKIP = (".svn", "_svn", ".git", ".hg")
LAYOUT = (server.TRUNK, server.BRANCHES, server.TAGS)


def _any_case(text: str) -> str:
    """«thumbs.db» → «[tT][hH]…»: шаблоны global-ignores в svn чувствительны к регистру."""
    out = []
    for ch in text:
        if ch.lower() != ch.upper():
            out.append(f"[{ch.lower()}{ch.upper()}]")
        elif ch in "[]*?":
            out.append(f"[{ch}]")
        else:
            out.append(ch)
    return "".join(out)


def name_patterns(rules: FilterRules) -> list[str]:
    """Шаблоны имён мусора (без учёта регистра, как в fnmatch.casefold).

    junk_globs (шаблоны по относительному пути) svn import не поддерживает: они не учитываются.
    Пробел заменяется на «?»: global-ignores разделяет шаблоны пробелами.
    """
    patterns = list(ALWAYS_SKIP)
    if rules.enabled:
        patterns += ["*" + ext for ext in rules.junk_extensions]
        patterns += list(rules.junk_names)
        patterns += [prefix + "*" for prefix in rules.junk_prefixes]
        patterns += list(rules.junk_dirs)
    return [p for p in dict.fromkeys(_name_pattern(p) for p in patterns) if p]


def _name_pattern(name: str) -> str:
    return name.strip().casefold().replace(" ", "?")


def svn_ignores(patterns: Sequence[str]) -> list[str]:
    """Те же шаблоны в виде для svn.

    Латиница: регистр через [xX]. Сравнение в svn побайтовое (UTF-8), поэтому для кириллицы классы [аА]
    не работают: такой шаблон даётся как есть, строчными, Заглавной и ПРОПИСНЫМИ (смешанный регистр
    вроде «аРхИв» не отсеется, это упрощение).
    """
    result: list[str] = []
    for pattern in patterns:
        if pattern.isascii():
            result.append("".join(ch if ch in "*?" else _any_case(ch) for ch in pattern))
        else:
            result += [pattern, pattern.lower(), pattern.capitalize(), pattern.upper()]
    return list(dict.fromkeys(result))


def is_junk(name: str, patterns: Sequence[str]) -> bool:
    low = name.casefold()
    return any(fnmatch.fnmatchcase(low, p) for p in patterns)


def is_link(entry: os.DirEntry) -> bool:
    """Ссылка на другую папку (symlink или junction): svn import её не копирует как обычную папку."""
    try:
        if entry.is_symlink():
            return True
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


_is_junk = is_junk      # прежние имена: остаются для совместимости
_is_link = is_link


@dataclass
class ScanResult:
    """Что будет загружено из папки и о чём предупредить."""
    files: int = 0
    dirs: int = 0
    size: int = 0
    junk_files: int = 0
    junk_size: int = 0
    vcs_dirs: list = field(default_factory=list)      # найденные .svn/.git (относительные пути)
    links: list = field(default_factory=list)         # ссылки на другие папки (junction): пропускаются
    open_files: list = field(default_factory=list)    # ~$файл: документ сейчас открыт в Word/Excel/Компас
    errors: list = field(default_factory=list)        # папки, которые не удалось прочитать
    is_working_copy: bool = False                     # в самой папке есть .svn

    @property
    def entries(self) -> int:
        return self.files + self.dirs

    def warnings(self) -> list[str]:
        """Предупреждения простыми словами (без терминов SVN)."""
        from .messages import format_number, format_size
        out = []
        if self.is_working_copy:
            out.append("Эта папка уже связана с сервером или историей проекта (в ней есть служебная папка .svn). "
                       "Возможно, проект уже есть на сервере.")
        elif self.vcs_dirs:
            out.append(f"Внутри есть служебные папки других программ ({', '.join(self.vcs_dirs[:3])}). "
                       "Они не будут загружены.")
        if self.links:
            out.append(f"Ссылки на другие папки не будут загружены: {', '.join(self.links[:3])}"
                       + (f" и ещё {len(self.links) - 3}" if len(self.links) > 3 else "") + ".")
        if self.open_files:
            out.append(f"Похоже, сейчас открыты документы ({len(self.open_files)}), например "
                       f"«{self.open_files[0]}». Закройте их и сохраните, иначе загрузится старая копия.")
        if self.size >= LARGE_IMPORT_BYTES or self.files >= LARGE_IMPORT_FILES:
            out.append(f"Очень большой объём: {format_size(self.size)}, {format_number(self.files)} файлов. "
                       "Загрузка может занять много времени.")
        if self.errors:
            out.append(f"Не удалось прочитать папок: {len(self.errors)} (например, {self.errors[0]}). "
                       "Их содержимое может не попасть на сервер.")
        return out


def scan_source(src: str, patterns: Sequence[str], cancel: Optional[CancelToken] = None,
                progress: Optional[CheckoutProgress] = None) -> tuple[ScanResult, list[str]]:
    """Обходит папку только на чтение. Возвращает (сводка, дополнительные шаблоны имён для ссылок).

    Мусор считается по тем же шаблонам, что потом отсеет svn import. Шаблон ссылки действует по имени
    во всей папке: одноимённая обычная папка тоже не загрузится (упрощение прототипа).
    """
    result = ScanResult()
    result.is_working_copy = os.path.isdir(os.path.join(src, ".svn"))
    extra: list[str] = []
    stack = [("", src)]
    while stack:
        if cancel and cancel.cancelled:
            raise SvnError(ErrorKind.CANCELLED)
        rel_dir, full_dir = stack.pop()
        try:
            entries = list(os.scandir(full_dir))
        except OSError as exc:
            log.warning("Не удалось прочитать папку %s: %s", full_dir, exc)
            result.errors.append(rel_dir or os.path.basename(src))
            continue
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            low = entry.name.casefold()
            if progress:
                progress.count += 1
                progress.last_path = rel
            if low in ALWAYS_SKIP:
                if rel_dir or low != ".svn":
                    result.vcs_dirs.append(rel)
                continue
            if _is_link(entry):
                # svn import заходит внутрь junction и загрузил бы чужую папку: исключаем по имени
                result.links.append(rel)
                extra.append(entry.name.strip().replace(" ", "?"))   # имя как есть: для кириллицы важен регистр
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if low.startswith("~$"):
                result.open_files.append(entry.name[2:])
            if _is_junk(entry.name, patterns):
                if not is_dir:
                    result.junk_files += 1
                    result.junk_size += _size(entry)
                continue
            if is_dir:
                result.dirs += 1
                stack.append((rel, entry.path))
            else:
                result.files += 1
                result.size += _size(entry)
    return result, extra


def _size(entry: os.DirEntry) -> int:
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


# --------------------------------------------------------------------------
# Создание проекта
# --------------------------------------------------------------------------

class CreateError(Exception):
    """Сбой создания. stage: check | mkdir | import. rolled_back: True — сервер возвращён как было,
    False — откатить не удалось (rollback_error), None — откатывать было нечего."""

    def __init__(self, error: SvnError, stage: str, rolled_back: Optional[bool] = None, rollback_error: str = ""):
        super().__init__(str(error))
        self.error = error
        self.stage = stage
        self.rolled_back = rolled_back
        self.rollback_error = rollback_error

    @property
    def kind(self) -> ErrorKind:
        return self.error.kind


@dataclass
class CreateResult:
    revision: Optional[int]        # ревизия, в которой загружены файлы
    entries: int                   # сколько файлов и папок загружено
    recovered: bool = False        # отмена пришла, когда загрузка уже завершилась на сервере


def create_project(client: SvnClient, project_url: str, src: str, message: str, ignores: Sequence[str],
                   progress: Optional[CheckoutProgress] = None,
                   cancel: Optional[CancelToken] = None,
                   on_layout: Optional[Callable[[LogEntry], None]] = None) -> CreateResult:
    """Создаёт <project_url>/{trunk,branches,tags} и загружает src в trunk. При сбое загрузки откатывает.

    Бросает CreateError. project_url закодирован (server.build_url). on_layout(запись журнала сервера) вызывается
    сразу после создания трёх папок: вызывающий пишет номер правки в свой журнал (`jobs.layout_recorder`); его сбой
    только записывается в журнал программы и создание не прерывает.
    """
    progress = progress or CheckoutProgress()
    try:
        if client.url_exists(project_url):
            raise SvnError(ErrorKind.EXISTS, "svn: E160020: Path already exists: " + project_url)
        if cancel and cancel.cancelled:
            raise SvnError(ErrorKind.CANCELLED)
    except SvnError as exc:
        raise CreateError(exc, "check") from exc
    try:
        client.mkdir_urls([f"{project_url}/{name}" for name in LAYOUT], message)
        first = client.last_log_entry(project_url)[0]
    except (SvnError, IndexError) as exc:
        error = exc if isinstance(exc, SvnError) else SvnError(ErrorKind.OTHER, "пустой журнал после создания")
        raise CreateError(error, "mkdir") from exc
    log.info("Проект %s: папки созданы, ревизия %s (%s)", project_url, first.revision, first.author)
    if on_layout is not None:
        try:
            on_layout(first)
        except Exception:
            log.exception("Номер правки папок проекта %s не записан в журнал задачи", project_url)
    try:
        entries = client.import_dir(src, f"{project_url}/{server.TRUNK}", message, ignores, progress, cancel)
        newest = client.last_log_entry(project_url)
        return CreateResult(revision=newest[0].revision if newest else None, entries=entries)
    except SvnError as exc:
        # отмена могла прийти, когда сервер уже принял загрузку: тогда проект готов, откатывать нельзя
        try:
            newest = client.last_log_entry(project_url)
        except SvnError:
            log.exception("Не удалось прочитать журнал проекта после сбоя загрузки")
            newest = []
        if newest and newest[0].revision != first.revision:
            if newest[0].author == first.author and exc.kind is ErrorKind.CANCELLED:
                log.warning("Загрузка отменена, но уже завершилась на сервере (ревизия %s)", newest[0].revision)
                return CreateResult(revision=newest[0].revision, entries=progress.count, recovered=True)
            raise CreateError(exc, "import", rolled_back=False,
                              rollback_error="после создания в проекте появились другие изменения") from exc
        rolled_back, reason = rollback(client, project_url, first.revision, first.author)
        raise CreateError(exc, "import", rolled_back=rolled_back, rollback_error=reason) from exc


def rollback(client: SvnClient, project_url: str, revision: int, author: str) -> tuple[bool, str]:
    """Удаляет только что созданный проект, если в нём ничего не появилось после нашей ревизии.

    (удалено ли, причина отказа). Отмена сюда не передаётся: откат доводится до конца.
    """
    try:
        newest = client.last_log_entry(project_url)
        if not newest or newest[0].revision != revision or newest[0].author != author:
            return False, "после создания в проекте появились другие изменения"
        client.remove_url(project_url, "SVN-lite: откат, проект не был создан до конца")
        log.info("Откат: проект %s удалён", project_url)
        return True, ""
    except SvnError as exc:
        log.error("Откат создания проекта %s не удался: %s", project_url, exc.raw)
        return False, exc.raw.strip().splitlines()[-1] if exc.raw.strip() else exc.kind.value
