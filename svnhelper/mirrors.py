"""Путь на диске (Z:\\… или \\\\сервер\\…) → папка в хранилище. Без tkinter.

Соответствия берутся из настроек «mirrors»: [{"path": "Z:\\Проекты_SVN", "repo": "Проекты_SVN"}].
"repo" — путь от адреса сервера (как в «repos») или полная ссылка svn://….
Регистр букв, вид слешей, слеш на конце и буква сетевого диска (Z: = \\\\fs01\\share) не важны.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence
from urllib.parse import unquote

from . import server
from .svn_core import CancelToken, ErrorKind, SvnClient, SvnError

_LOCAL_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[A-Za-z]:$|\\\\|//)")
_URL_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://[^/]*)(/.*)?$")

# что ответил сервер про папку
PROJECT = "project"      # проект есть (папка с trunk), можно скачивать
MISSING = "missing"      # проекта нет: можно создать из папки
FOLDER = "folder"        # на сервере есть папка с таким путём, но это не проект
ROOT = "root"            # указан сам корень соответствия, а не папка проекта
NO_REPO = "no_repo"      # нет самой папки хранилища на сервере (ошибка настроек)


def looks_like_local_path(text: str) -> bool:
    """Похоже на путь на диске: «Z:\\…», «C:/…» или «\\\\сервер\\…»."""
    return bool(_LOCAL_RE.match(text.strip().strip('"').strip()))


def _split(path: str) -> list[str]:
    """«\\\\Fs01\\Share\\A\\» → ["\\\\fs01", "share", "a"]; «Z:/A» → ["z:", "a"]. Без учёта регистра."""
    path = path.strip().strip('"').strip().replace("/", "\\")
    unc = path.startswith("\\\\")
    parts = [p for p in path.split("\\") if p and p != "."]
    if unc and parts:
        parts[0] = "\\\\" + parts[0]
    return [p.casefold() for p in parts]


def _original_parts(path: str) -> list[str]:
    """Те же части, но в исходном написании (для имён папок на сервере)."""
    path = path.strip().strip('"').strip().replace("/", "\\")
    return [p for p in path.split("\\") if p and p != "."]


def _variants(path: str, resolve_unc: Optional[Callable[[str], Optional[str]]]) -> list[list[str]]:
    """Путь как есть и, если он на сетевом диске, тот же путь через \\\\сервер\\папка."""
    parts = _split(path)
    found = [parts]
    if parts and re.fullmatch(r"[a-z]:", parts[0]) and resolve_unc:
        unc = resolve_unc(parts[0].upper())
        if unc:
            found.append(_split(unc) + parts[1:])
    return found


@dataclass(frozen=True)
class MirrorMatch:
    """Путь попал в соответствие: адрес сервера, папка хранилища и путь внутри неё (имена как ввёл человек)."""
    prefix: str                 # svn://host
    repo_segments: tuple        # папка хранилища от адреса сервера
    rest: tuple                 # путь внутри неё
    mirror_path: str            # корень соответствия, как он записан в настройках

    @property
    def segments(self) -> tuple:
        return self.repo_segments + self.rest


def _repo_base(repo: str, default_server: str) -> tuple[str, tuple]:
    """(адрес сервера, сегменты) для "repo": путь от сервера или полная ссылка."""
    match = _URL_PREFIX_RE.match(repo.strip())
    if match and "://" in repo:
        return match.group(1), tuple(unquote(s) for s in (match.group(2) or "").split("/") if s)
    return default_server.rstrip("/"), tuple(s for s in repo.replace("\\", "/").split("/") if s)


def map_path(text: str, mirrors: Sequence[dict], default_server: str,
             resolve_unc: Optional[Callable[[str], Optional[str]]] = None) -> Optional[MirrorMatch]:
    """Ищет соответствие для пути. Из нескольких подходящих берётся самое длинное. None — путь вне хранилища.

    resolve_unc(«Z:») → «\\\\fs01\\share» или None: нужен, чтобы Z: у одного и \\\\fs01\\share у другого совпадали.
    """
    original = _original_parts(text)
    best: Optional[tuple[int, MirrorMatch]] = None
    for mirror in mirrors:
        for wanted in _variants(text, resolve_unc):
            for root in _variants(mirror["path"], resolve_unc):
                if not root or len(wanted) < len(root) or wanted[:len(root)] != root:
                    continue
                # «хвост» берём в исходном написании: на сервере регистр букв имеет значение
                rest = tuple(original[len(original) - (len(wanted) - len(root)):]) if len(wanted) > len(root) else ()
                prefix, repo_segments = _repo_base(mirror["repo"], default_server)
                match = MirrorMatch(prefix, repo_segments, rest, mirror["path"])
                if best is None or len(root) > best[0]:
                    best = (len(root), match)
    return best[1] if best else None


@dataclass
class Target:
    """Что за папка на сервере соответствует пути на диске."""
    kind: str                                   # PROJECT | MISSING | FOLDER | ROOT | NO_REPO
    match: MirrorMatch
    project: Optional[server.ProjectRef] = None  # для PROJECT: найденный проект (может быть выше по пути)
    create_segments: tuple = ()                  # для MISSING: где создавать проект
    subfolder: tuple = field(default_factory=tuple)   # для PROJECT: подпапка проекта, которую ввёл человек


def find_target(client: SvnClient, match: MirrorMatch, cancel: Optional[CancelToken] = None) -> Target:
    """Ищет проект от введённой папки вверх до корня соответствия (подпапка проекта ведёт к самому проекту).

    Запросов к серверу не больше, чем уровней пути. Ошибки, кроме «папки нет», пробрасываются (SvnError).
    """
    if not match.rest:
        return Target(ROOT, match)
    deepest_exists = False
    for depth in range(len(match.rest), -1, -1):
        segments = list(match.repo_segments + match.rest[:depth])
        try:
            # '@' в конце: svn раскодирует %40 в имени и иначе примет хвост за ревизию
            names = client.list_dirs(server.build_url(match.prefix, segments) + "@", cancel)
        except SvnError as exc:
            if exc.kind is not ErrorKind.NOT_FOUND:
                raise
            if depth == 0:
                return Target(NO_REPO, match)
            continue
        if depth == 0:
            break
        if server.TRUNK in names:
            project = server.project_from_segments(match.prefix, segments)
            return Target(PROJECT, match, project=project, subfolder=match.rest[depth:])
        if depth == len(match.rest):
            deepest_exists = True
    if deepest_exists:
        return Target(FOLDER, match)
    return Target(MISSING, match, create_segments=match.segments)


def disk_path(prefix: str, segments: Sequence[str], mirrors: Sequence[dict], default_server: str) -> str:
    """Папка проекта на диске (Z:\\…) по его адресу в хранилище: обратное к map_path. Пусто, если соответствия нет."""
    best = ""
    best_len = -1
    wanted_prefix = prefix.rstrip("/").casefold()
    for mirror in mirrors:
        mprefix, repo_segments = _repo_base(mirror["repo"], default_server)
        n = len(repo_segments)
        if mprefix.rstrip("/").casefold() != wanted_prefix or list(segments[:n]) != list(repo_segments):
            continue
        if n > best_len:
            rest = [s for s in segments[n:]]
            best = os.path.join(mirror["path"].rstrip("\\/") + "\\", *rest) if rest else mirror["path"]
            best_len = n
    return best


def unc_resolver(drive: str) -> Optional[str]:
    """Сетевой адрес диска (только Windows)."""
    from . import winutil
    return winutil.drive_to_unc(drive)


def is_dir(path: str) -> bool:
    """Есть ли такая папка на диске (сетевой путь может отвечать долго: вызывать в фоне)."""
    try:
        return os.path.isdir(path.strip().strip('"'))
    except OSError:
        return False
