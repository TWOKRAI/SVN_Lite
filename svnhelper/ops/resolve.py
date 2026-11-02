"""Как по названию или пути найти папку проекта и какие папки помощнику открывать нельзя.

Принимаются: название папки проекта (без учёта регистра), название из project.json, название без даты в начале или
начало слова в названии (последние три шага нестрогие), либо полный путь (в том числе вида `/c/Users/...`, который
даёт Git Bash). Строгий режим (`strict=True`, для записи): только точное название папки или полный путь.

Отказ (код 6): `..`, сетевые пути (UNC, `\\\\?\\`), сетевой диск, ссылка (junction, symlink), папка вне папок для
проектов, имя с завершающей точкой или пробелом, потоком данных (`::$DATA`) и другими недопустимыми знаками. Те же
проверки проходят и сами папки для проектов: ссылка на сетевую папку из них отбрасывается. Одно имя в двух папках или
нестрогое имя, подходящее нескольким проектам: ошибка `ambiguous` со списком.
"""
from __future__ import annotations

import dataclasses
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterator, Optional

from .. import checks, importer, notifier, projectmeta
from . import texts
from .context import OpsContext
from .errors import OpsError, refused
from .result import EXIT_OTHER, MAX_ENTRIES, clean_name

_GIT_BASH_RE = re.compile(r"^/([A-Za-z])(?:/(.*))?$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:\\")
_PATH_CHARS = ("\\", "/", ":")
_BAD_NAME_CHARS = frozenset(':*?"<>|')
_DATE_PREFIX_RE = re.compile(r"^\d{6,8}[_ -]+")      # дата в начале названия папки: 261028_отбраковщик
MAX_FOLDERS = 200           # подпапок в списке проектов
MIN_PART = 3                # знаков в части названия, по которой ищут проект

# как проект найден (data.matched_by)
BY_EXACT = "exact"          # точное название папки
BY_PATH = "path"            # полный путь
BY_META = "meta"            # название из project.json
BY_NO_DATE = "no_date"      # название папки без даты в начале
BY_PART = "part"            # начало слова в названии папки


@dataclass(frozen=True)
class ProjectRef:
    name: str               # название папки (одна строка, без управляющих знаков и кавычек « »)
    path: str               # полный путь (normpath), настоящий: по нему работает файловая система
    matched_by: str = BY_PATH


def from_git_bash(ref: str) -> str:
    """`/c/Users/me/проект` -> `C:\\Users\\me\\проект`; остальное без изменений (`/mnt/c/...` не понимается)."""
    match = _GIT_BASH_RE.match(ref)
    if not match:
        return ref
    rest = (match.group(2) or "").replace("/", "\\")
    return f"{match.group(1).upper()}:\\{rest}"


def looks_like_path(ref: str) -> bool:
    return any(ch in ref for ch in _PATH_CHARS) or ref.startswith((".", "~"))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _fold(text: str) -> str:
    """Название для сравнения: NFC (я + й как одна буква) и без учёта регистра."""
    return unicodedata.normalize("NFC", text).casefold()


def _real(path: str) -> str:
    """Путь с раскрытыми ссылками, без `\\\\?\\`, normcase."""
    real = os.path.realpath(path)
    if real.startswith("\\\\?\\UNC\\"):
        real = "\\\\" + real[8:]
    elif real.startswith("\\\\?\\"):
        real = real[4:]
    return _norm(real)


def _lookup(path: str) -> Optional[tuple]:
    """(настоящее имя, ссылка ли это) последней части пути по списку родительской папки; None: в списке её нет
    (например, путь написан короткими именами 8.3)."""
    parent, name = os.path.split(os.path.normpath(path))
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name.casefold() == name.casefold():
                    return entry.name, importer.is_link(entry)
    except OSError:
        pass
    return None


def is_link_path(path: str) -> bool:
    """Последняя часть пути - ссылка на другое место (junction, symlink)."""
    found = _lookup(path)
    return bool(found and found[1])


def base_refusal(base: str) -> Optional[str]:
    """Почему папку для проектов использовать нельзя (сеть, в том числе через ссылку), или None."""
    if base.startswith(("\\\\", "//")) or _real(base).startswith("\\\\"):
        return texts.REFUSE_UNC
    if checks.drive_type(base) == checks.DRIVE_REMOTE:
        return texts.REFUSE_NETWORK_DRIVE
    return None


def _existing_bases(ctx: OpsContext) -> list:
    return [b for b in ctx.project_dirs if b and os.path.isdir(b)]


def folder_bases(ctx: OpsContext, warnings: Optional[list] = None) -> list:
    """Папки для проектов, с которыми можно работать. Сетевые и ссылки на сеть отбрасываются (причина в warnings)."""
    result = []
    for base in _existing_bases(ctx):
        reason = base_refusal(base)
        if reason is None:
            result.append(base)
        elif warnings is not None:
            warnings.append(texts.WARN_BASE_REFUSED.format(path=clean_name(base), reason=reason))
    return result


def iter_folders(bases: list) -> Iterator[tuple]:
    """(путь, название) подпапок в папках для проектов; скрытые и ссылки пропускаются. Без повторов по нормализованному
    пути; название настоящее, как на диске."""
    seen = set()
    for base in bases:
        try:
            with os.scandir(base) as entries:
                items = sorted(entries, key=lambda e: e.name.casefold())
        except OSError:
            continue
        for entry in items:
            path = os.path.join(base, entry.name)
            key = _norm(path)
            if key in seen or entry.name.startswith(".") or not _is_dir(entry) or importer.is_link(entry):
                continue
            seen.add(key)
            yield os.path.normpath(path), entry.name


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def make_ref(path: str, name: str, matched_by: str = BY_PATH) -> ProjectRef:
    return ProjectRef(clean_name(name), path, matched_by)


def known_projects(ctx: OpsContext, warnings: Optional[list] = None) -> list:
    """Проекты в папках для проектов: скачанные (есть .svn, `notifier.discover_projects`) и папки с project.json."""
    bases = folder_bases(ctx, warnings)
    found = {_norm(path): make_ref(path, name) for path, name in notifier.discover_projects(bases)
             if not is_link_path(path)}
    for path, name in iter_folders(bases):
        if _norm(path) not in found and projectmeta.meta_path(path).is_file():
            found[_norm(path)] = make_ref(path, name)
    return sorted(found.values(), key=lambda p: (p.name.casefold(), p.path))


def resolve_project(ctx: OpsContext, ref: str, strict: bool = False) -> ProjectRef:
    """Папка проекта по названию или пути. Бросает OpsError: project_not_found, ambiguous, path_refused.

    strict=True (запись): только точное название папки или полный путь."""
    text = (ref or "").strip().strip('"')
    if not text:
        raise OpsError("invalid_args", 2, texts.EMPTY_REF)
    if looks_like_path(text):
        return _check_path(ctx, text)
    return _by_name(ctx, text, strict)


def match_warnings(ref: ProjectRef) -> list:
    """Предупреждения о том, как найден проект: выбранный по части названия нужно подтвердить."""
    return [texts.WARN_MATCHED_BY_PART.format(name=ref.name)] if ref.matched_by == BY_PART else []


def _by_name(ctx: OpsContext, name: str, strict: bool) -> ProjectRef:
    projects = known_projects(ctx)
    wanted = _fold(name)
    matches = [(p, BY_EXACT) for p in projects if _fold(p.name) == wanted]
    if not matches and not strict:
        matches = [(p, why) for p in projects for why in [_loose_match(p, wanted)] if why]
    if not matches:
        raise OpsError("project_not_found", EXIT_OTHER, next_step="" if ctx.project_dirs else texts.NO_PROJECT_DIRS)
    if len(matches) > 1:
        candidates = [{"name": p.name, "path": clean_name(p.path)} for p, _why in matches[:MAX_ENTRIES]]
        raise OpsError("ambiguous", EXIT_OTHER, data={"candidates": candidates},
                       untrusted_fields=["candidates[].name", "candidates[].path"])
    project, why = matches[0]
    return dataclasses.replace(_check_path(ctx, project.path), matched_by=why)


def _loose_match(project: ProjectRef, wanted: str) -> str:
    """Чем нестрого подходит проект (BY_META, BY_NO_DATE, BY_PART) или пустая строка."""
    meta = projectmeta.read(project.path)
    if meta and _fold(meta.name) == wanted:
        return BY_META
    title = _fold(project.name)
    if _fold(_DATE_PREFIX_RE.sub("", project.name)) == wanted:
        return BY_NO_DATE
    if len(wanted) >= MIN_PART and re.search(r"(?<![^\W_])" + re.escape(wanted), title):
        return BY_PART
    return ""


def _bad_component(part: str) -> bool:
    """Часть пути, которую Windows читает не так, как написано: завершающая точка или пробел, поток данных
    (`::$INDEX_ALLOCATION`), знаки подстановки."""
    return bool(part) and (part.endswith((".", " ")) or any(ch in _BAD_NAME_CHARS or ord(ch) < 32 for ch in part))


def _check_path(ctx: OpsContext, ref: str) -> ProjectRef:
    path = from_git_bash(ref).replace("/", "\\")
    if path.startswith("\\\\"):
        raise refused(texts.REFUSE_UNC, ref)
    parts = path.split("\\")
    if ".." in parts:
        raise refused(texts.REFUSE_PARENT, ref)
    if not _DRIVE_RE.match(path):
        raise refused(texts.REFUSE_RELATIVE, ref)
    if any(_bad_component(part) for part in parts[1:]):
        raise refused(texts.REFUSE_BAD_NAME, ref)
    path = os.path.normpath(path)
    if checks.drive_type(path) == checks.DRIVE_REMOTE:
        raise refused(texts.REFUSE_NETWORK_DRIVE, ref)
    if not os.path.isdir(path):
        raise refused(texts.REFUSE_NOT_DIR, ref)
    found = _lookup(path)
    if found is None:
        raise refused(texts.REFUSE_BAD_NAME, ref)
    real_name, is_link = found
    if is_link:
        raise refused(texts.REFUSE_LINK, ref)
    base = _base_of(ctx, path)
    if base is None:
        raise refused(texts.REFUSE_OUTSIDE if _existing_bases(ctx) else texts.NO_PROJECT_DIRS, ref)
    reason = base_refusal(base)
    if reason:
        raise refused(reason, ref)
    return make_ref(os.path.join(base, real_name), real_name)      # название и путь настоящие, как на диске


def _base_of(ctx: OpsContext, path: str) -> Optional[str]:
    """Папка для проектов, в которой папка лежит прямо; ссылка внутри пути (junction, subst) наружу не выводит."""
    real = _real(path)
    parent, name = os.path.split(real)
    parent = parent.rstrip("\\")
    for base in _existing_bases(ctx):
        if _real(base).rstrip("\\") == parent and name == _norm(os.path.basename(path)):
            return base
    return None
