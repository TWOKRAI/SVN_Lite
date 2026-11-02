"""Дерево папок на сервере: построение URL и определение проектов (папок с trunk)."""
from __future__ import annotations

import re
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence
from urllib.parse import quote, unquote

from .svn_core import CancelToken, SvnClient, SvnError

TRUNK = "trunk"
BRANCHES = "branches"
TAGS = "tags"
MAX_LINE_NAME = 80
PROBE_THREADS = 6


def build_url(server: str, segments: Sequence[str]) -> str:
    """Каждый сегмент кодируется отдельно: '@', '%', пробелы и кириллица не ломают URL."""
    base = server if server.endswith("://") else server.rstrip("/")
    return base + "".join("/" + _quote_segment(s) for s in segments)


def _quote_segment(segment: str) -> str:
    if _DRIVE_SEGMENT.fullmatch(segment):  # file:///C:/... — двоеточие диска кодировать нельзя
        return segment
    return quote(segment, safe="")


_DRIVE_SEGMENT = re.compile(r"[A-Za-z]:")
_URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/]*)(/.*)?$")
ALLOWED_SCHEMES = {"svn", "svn+ssh", "http", "https", "file"}


def normalize_url(url: str) -> str:
    """Приводит введённую вручную ссылку к закодированному виду (кириллица и пробелы допустимы)."""
    match = _URL_RE.match(url.strip())
    if not match:
        return url
    segments = [unquote(x) for x in (match.group(3) or "").split("/") if x]
    return build_url(f"{match.group(1).lower()}://{match.group(2)}", segments)


@dataclass(frozen=True)
class Line:
    """Линия разработки внутри проекта: основная (trunk), ветка (branches/<имя>) или версия (tags/<имя>)."""
    kind: str         # "trunk" | "branch" | "tag" | "folder" (обычная папка сервера без trunk/branches/tags)
    name: str = ""    # "" для trunk

    @property
    def label(self) -> str:
        if self.kind == "branch":
            return f"Ветка «{self.name}»"
        if self.kind == "tag":
            return f"Версия «{self.name}»"
        if self.kind == "folder":
            return "Папка"
        return "Основная линия"

    @property
    def read_only(self) -> bool:
        """Версии (теги) только для чтения."""
        return self.kind == "tag"

    def segments(self) -> list[str]:
        """Путь линии относительно папки проекта (имена в «человеческом» виде)."""
        if self.kind == "branch":
            return [BRANCHES, self.name]
        if self.kind == "tag":
            return [TAGS, self.name]
        if self.kind == "folder":
            return []
        return [TRUNK]


TRUNK_LINE = Line("trunk")
FOLDER_LINE = Line("folder")


@dataclass(frozen=True)
class ProjectRef:
    prefix: str            # svn://host
    segments: tuple        # путь проекта без trunk/branches/tags, имена в «человеческом» виде
    line: Line = TRUNK_LINE

    @property
    def name(self) -> str:
        return self.segments[-1]

    @property
    def trunk_url(self) -> str:
        return build_url(self.prefix, [*self.segments, TRUNK])

    def line_url(self, line: Optional[Line] = None) -> str:
        """URL выбранной линии (по умолчанию — линии самой ссылки)."""
        return build_url(self.prefix, [*self.segments, *(line or self.line).segments()])

    @property
    def display(self) -> str:
        return self.prefix + "/" + "/".join(self.segments)


def _split_line(segments: list[str], start: int = 0) -> tuple[list[str], Line, bool]:
    """(путь проекта, линия, найден ли маркер). Маркер ищется СПРАВА, с индекса start.

    Правило: самый правый `trunk` определяет основную линию; если trunk нет - самый правый
    `branches/<имя>` или `tags/<имя>` (с именем после маркера) определяет ветку или версию.
    Всё слева от маркера - путь проекта, всё справа (подпапки) отбрасывается. Поэтому:
      g/tags/proj/trunk -> проект g/tags/proj (папка с именем tags внутри пути - не версия);
      p/trunk/tags/x    -> проект p, основная линия (tags внутри trunk - обычная папка);
      p/tags/v1/docs    -> проект p, версия v1.
    Неустранимая неоднозначность: ссылка без trunk вида X/tags/<имя> всегда читается как
    версия <имя> проекта X (синтаксически она не отличима от проекта в папке tags);
    проект в такой папке задавайте ссылкой с /trunk или выбором в дереве.
    «.../branches» или «.../tags» без имени - проект с основной линией.
    """
    n = len(segments)
    for i in range(n - 1, start - 1, -1):
        if segments[i] == TRUNK:
            return segments[:i], TRUNK_LINE, True
    for i in range(n - 2, start - 1, -1):
        if segments[i] in (BRANCHES, TAGS):
            kind = "branch" if segments[i] == BRANCHES else "tag"
            return segments[:i], Line(kind, segments[i + 1]), True
    if n - 1 >= start and segments[n - 1] in (BRANCHES, TAGS):
        return segments[:n - 1], TRUNK_LINE, True
    return segments, TRUNK_LINE, False


def example_url(server_url: str = "", repos: Sequence[str] = ()) -> str:
    """Пример ссылки на проект для подсказок: адрес сервера из настроек и его первая папка проектов.

    Сервер не настроен (адреса нет или он без схемы) - нейтральный пример, а не чужой адрес."""
    base = (server_url or "").strip().rstrip("/")
    if not _URL_RE.match(base):
        base = "svn://адрес_сервера"
    return f"{base}/{repos[0] if repos else 'папка'}/проекты/мой_проект"


def parse_project_url(text: str, default_server: str) -> ProjectRef:
    """Разбирает ссылку, которую вставил пользователь. Бросает ValueError с русским текстом.

    Понимает ссылку с /trunk, /branches/<имя>, /tags/<имя> (и подпапками после них)
    на конце и без, с пробелами и кириллицей как в «сыром», так и в закодированном
    (%D0...) виде, и путь без адреса сервера. Подпапка обрезается до линии;
    «.../branches» без имени означает проект с основной линией.
    Правило поиска линии (самый правый маркер) и его неоднозначности - в _split_line.
    """
    t = text.strip().strip('"').strip("<>").strip()
    example = example_url(default_server)
    if not t:
        raise ValueError("Вставьте ссылку на проект.")
    if "://" not in t:
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", t):
            raise ValueError("Ссылка выглядит неверно. Пример: " + example)
        t = default_server.rstrip("/") + "/" + t.lstrip("/\\").replace("\\", "/")
    match = _URL_RE.match(t)
    if not match:
        raise ValueError("Ссылка выглядит неверно. Пример: " + example)
    scheme, netloc, path = match.group(1).lower(), match.group(2), match.group(3) or ""
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("Ссылка должна начинаться с svn://")
    segments, line, _found = _split_line([unquote(seg) for seg in path.split("/") if seg])
    if not segments:
        raise ValueError("В ссылке нет пути к проекту. Пример: " + example)
    return ProjectRef(f"{scheme}://{netloc}", tuple(segments), line)


def line_from_url(url: str, root: str = "") -> Optional[tuple[tuple, Line]]:
    """По адресу рабочей копии (из svn info; закодированному или нет) → (путь проекта, линия).

    Путь проекта — сегменты после адреса сервера (то же, что ProjectRef.segments).
    root — адрес корня репозитория: если задан и url внутри него, trunk/branches/tags
    ищутся только ниже корня (иначе репозиторий с именем «tags» сбил бы разбор).
    None, если в адресе нет trunk/branches/tags. Маркер ищется справа (см. _split_line):
    для корня копии это «trunk» или «branches|tags/<имя>» в конце, для подпапки линии -
    ближайший справа.
    """
    match = _URL_RE.match(url.strip())
    if not match:
        return None
    segments = [unquote(x) for x in (match.group(3) or "").split("/") if x]
    start = 0
    root_match = _URL_RE.match(root.strip()) if root else None
    if root_match and root_match.group(2) == match.group(2):
        root_segments = [unquote(x) for x in (root_match.group(3) or "").split("/") if x]
        if segments[:len(root_segments)] == root_segments:
            start = len(root_segments)
    project, line, found = _split_line(segments, start)
    if not found or not project:
        return None
    return tuple(project), line


def folder_from_segments(prefix: str, segments: Sequence[str]) -> ProjectRef:
    """Обычная папка сервера (не проект): скачивается как есть, без trunk."""
    if not segments:
        raise ValueError("Не указан путь к папке.")
    return ProjectRef(prefix, tuple(segments), FOLDER_LINE)


def project_from_segments(prefix: str, segments: Sequence[str]) -> ProjectRef:
    """Проект, выбранный в дереве: сегменты папки-проекта как есть, основная линия.

    Разбор строки не нужен и опасен: проект может лежать в папке с именем tags/branches.
    """
    if not segments:
        raise ValueError("Не указан путь к проекту.")
    return ProjectRef(prefix, tuple(segments), TRUNK_LINE)


_BAD_NAME_CHARS = re.compile(r'[/\\:*?"<>|]')
_RESERVED_NAMES = {TRUNK, BRANCHES, TAGS}


def validate_line_name(name: str) -> str:
    """Проверяет имя новой ветки или версии. Возвращает имя без пробелов по краям.

    Бросает ValueError с понятным русским текстом.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название.")
    if len(name) > MAX_LINE_NAME:
        raise ValueError(f"Название слишком длинное (не больше {MAX_LINE_NAME} символов).")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("В названии не должно быть управляющих символов (например, табуляции).")
    bad = _BAD_NAME_CHARS.search(name)
    if bad:
        raise ValueError(f"В названии нельзя использовать символ «{bad.group()}». "
                         'Запрещены: / \\ : * ? " < > |')
    if name in (".", "..") or name.startswith(".") or name.endswith("."):
        raise ValueError("Название не должно начинаться или заканчиваться точкой.")
    if name.casefold() in _RESERVED_NAMES:
        raise ValueError(f"Название «{name}» зарезервировано, выберите другое.")
    return name


def suggest_tag_name(today: date, existing: list[str]) -> str:
    """«2026-10-19»; если занято (без учёта регистра) — «2026-10-19_2», «_3» и т.д."""
    taken = {e.casefold() for e in existing}
    base = today.isoformat()
    candidate, n = base, 1
    while candidate.casefold() in taken:
        n += 1
        candidate = f"{base}_{n}"
    return candidate


@dataclass(frozen=True)
class FolderEntry:
    name: str
    is_project: bool


@dataclass
class FolderListing:
    entries: list[FolderEntry] = field(default_factory=list)
    is_project: bool = False  # сама папка оказалась проектом (trunk лежит в ней)


def load_folder(client: SvnClient, server: str, segments: Sequence[str],
                pool: Executor, cancel: Optional[CancelToken] = None) -> FolderListing:
    """Листинг папки + параллельная проверка, какие из подпапок являются проектами.

    Ошибка листинга самой папки пробрасывается. Если не удалось прочитать
    подпапку, она показывается как обычная папка.
    """
    names = client.list_dirs(build_url(server, segments), cancel)
    if TRUNK in names:
        return FolderListing(is_project=True)

    def probe(name: str) -> bool:
        try:
            return TRUNK in client.list_dirs(build_url(server, [*segments, name]), cancel)
        except SvnError:
            return False

    futures = [(name, pool.submit(probe, name)) for name in names]
    entries = [FolderEntry(name, fut.result()) for name, fut in futures]
    entries.sort(key=lambda e: e.name.casefold())
    return FolderListing(entries=entries)
