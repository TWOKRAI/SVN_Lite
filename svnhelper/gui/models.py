"""Константы, узлы дерева, задачи и чистые функции окна: без tkinter-виджетов и без svn."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import unquote
from typing import Optional

from .. import audit, checks, messages, server
from ..progress import EtaTracker
from ..svn_core import CancelToken, CheckoutProgress, ErrorKind, LockInfo, SvnClient, SvnError, WcStatus


log = logging.getLogger("svnhelper.gui")

MAX_LOGIN_ATTEMPTS = 3
REMEMBER_HINT = ("Хранится зашифрованным, расшифровать его может только ваша учётная запись Windows. "
                 "Если снять, программа не будет запоминать пароль.")
POLL_MS = 100
PROGRESS_MS = 150
TREE_HINT = "Проекты отмечены значком ✔. Выберите проект или любую папку, и ссылка подставится сама."
REFRESH_SEC = 5            # как часто вкладка «Мои проекты» (в том числе «Ветки» в колонке 3) обновляется сама
AUTO_CHECK_SEC = 300      # не чаще раза в 5 минут проверять один и тот же проект сам
LOCK_WARN_DAYS = 3        # напоминать об освобождении файлов, занятых дольше этого
CLOSE_WAIT_SEC = 15       # сколько ждать фоновые задачи при закрытии окна
CLOSE_POLL_MS = 100
LONG_TASK_SEC = 30        # после такой задачи мигаем в панели задач, если окно не в фокусе
SEARCH_DELAY_MS = 250     # пауза после ввода в поиске, прежде чем искать
SEARCH_LIMIT = 200        # сколько результатов поиска показываем
CLIPBOARD_MAX_LEN = 500   # длиннее этого в буфере ссылкой считать не будем
LINES_DELAY_MS = 400      # пауза после ввода ссылки, прежде чем загружать ветки и версии проекта
TRUNK_CHOICE = "Основная"
LINES_PREFETCH = 8        # сколько строк таблицы линий читать, пока неизвестно, какие из них видны
FAV_ADD_TEXT = "☆ В избранное"
FAV_REMOVE_TEXT = "★ Убрать из избранного"


def shorten_path(path: str, limit: int = 60) -> str:
    """Длинный путь для подписи: начало отбрасывается, остаётся «…конец пути»."""
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


@dataclass(frozen=True)
class JobStamp:
    """Состояние входа на момент старта задачи."""
    cred_gen: int
    login_epoch: int


@dataclass
class Node:
    segments: list[str]
    state: str = "new"  # new | loading | loaded | error
    is_project: bool = False


@dataclass
class MineItem:
    """Строка вкладки «Мои проекты»."""
    path: str
    name: str
    state: str                       # svn | plain | broken
    url: str = ""
    revision: Optional[int] = None
    status: Optional[WcStatus] = None
    error: str = ""
    checked: bool = False            # статус с сервера уже получен
    checking: bool = False           # проверка этого проекта идёт сейчас (или ждёт очереди)
    last_check: Optional[float] = None   # time.monotonic() последней проверки (для автопроверки)
    raw_url: str = ""                    # адрес копии как его напечатал svn (закодированный)
    line: Optional[server.Line] = None   # основная линия, ветка или версия; None, если по адресу не понять
    line_url: str = ""                   # закодированный адрес линии (источник для копирования и переключения)
    project_prefix: str = ""             # svn://host
    project_segments: tuple = ()         # путь проекта без линии
    local: bool = False                  # проект с историей на этом компьютере (file://), а не на сервере (Task T.6)
    history_missing: bool = False        # у такого проекта пропала история (папку переименовали, перенесли или удалили)
    history_found: str = ""              # пропавшая история найдена на диске в другом месте: её можно восстановить
    archive: str = ""                    # прежние сохранения на этом компьютере после отправки проекта на сервер

    @property
    def project_url(self) -> str:
        """Адрес проекта без линии (папка, где лежат trunk/branches/tags)."""
        return server.build_url(self.project_prefix, self.project_segments) if self.project_prefix else ""

    @property
    def project_key(self) -> str:
        return lines_key(self.project_prefix, self.project_segments)

    def line_url_of(self, line: server.Line) -> str:
        return server.build_url(self.project_prefix, [*self.project_segments, *line.segments()])

    @property
    def display_name(self) -> str:
        """Название проекта для таблицы: без суффикса «__ветка», если папка названа программой по умолчанию."""
        if not self.project_segments or self.line is None:
            return self.name
        project = self.project_segments[-1]
        names = {project, checks.safe_local_name(project)}
        if self.line.kind != "trunk":
            names |= {f"{project}__{self.line.name}", checks.safe_local_name(f"{project}__{self.line.name}")}
        return project if self.name.casefold() in {n.casefold() for n in names} else self.name

    def take_probe(self, fresh: "MineItem") -> None:
        """Перенимает результат повторного определения папки (адрес, линия)."""
        self.state, self.url, self.revision, self.error = fresh.state, fresh.url, fresh.revision, fresh.error
        self.raw_url, self.line, self.line_url = fresh.raw_url, fresh.line, fresh.line_url
        self.project_prefix, self.project_segments = fresh.project_prefix, fresh.project_segments
        self.local, self.history_missing, self.archive = fresh.local, fresh.history_missing, fresh.archive
        self.history_found = fresh.history_found


def lines_key(prefix: str, segments) -> str:
    """Ключ проекта для кэша веток и версий (то же, что ProjectRef.display)."""
    return prefix + "/" + "/".join(segments)


def same_url(a: str, b: str) -> bool:
    """Один и тот же адрес на сервере (закодированный и нет, со слэшем на конце и без)."""
    return unquote(a).rstrip("/") == unquote(b).rstrip("/")


def line_choice_label(kind: str, entry) -> str:
    """Строка выбора: «Ветка «x» — 18.10, Петров», «🔒 Версия «y» — 12.10»."""
    label = messages.line_label(server.Line(kind, entry.name))
    if kind == "tag":
        label = "🔒 " + label
    who = entry.last_author if kind == "branch" else ""
    when = f"{entry.last_date.astimezone():%d.%m}" if entry.last_date else ""
    tail = ", ".join(p for p in (when, who) if p)
    return f"{label} — {tail}" if tail else label


def line_cell(line: Optional[server.Line]) -> str:
    """Колонка «Ветка» в «Мои проекты»: «Основная», имя ветки, «🔒 Версия 2026-10-01»."""
    if line is None:
        return ""
    if line.kind == "trunk":
        return "Основная"
    return line.name if line.kind == "branch" else f"🔒 Версия {line.name}"


@dataclass
class LockResult:
    """Итог занятия/освобождения одного файла."""
    path: str
    error: Optional[SvnError] = None
    holder: Optional[LockInfo] = None    # кто держит файл (если занять не удалось из-за чужой блокировки)


def lock_holder_text(info: Optional[LockInfo]) -> str:
    """«Файл занят: Петров, с 12.10 (комментарий: …)». Без данных о владельце — общий текст."""
    if info is None:
        return messages.short_message(ErrorKind.LOCKED)
    text = f"Файл занят: {info.owner}"
    if info.created is not None:
        text += f", с {info.created.astimezone():%d.%m}"
    if info.comment.strip():
        text += f" (комментарий: {info.comment.strip()})"
    return text


def search_projects(query: str, loaded: list[list[str]], folders: dict,
                    limit: int = SEARCH_LIMIT) -> tuple[list[list[str]], bool]:
    """Поиск проектов по названию (без учёта регистра) среди открытых узлов и кэша дерева.

    query уже в нижнем регистре (casefold). Возвращает (пути проектов, были ли обрезаны по limit).
    Выполняется в фоновом потоке: кэш может быть большим.
    """
    found: dict[tuple, list[str]] = {}

    def consider(segments: list[str]) -> None:
        if segments and query in segments[-1].casefold():
            found.setdefault(tuple(segments), segments)

    for segments in loaded:
        consider(list(segments))
    for key, entry in folders.items():
        if not isinstance(entry, dict) or not isinstance(key, str):
            continue
        parent = [part for part in key.split("/") if part]
        if entry.get("is_project"):
            consider(parent)
        dirs = entry.get("dirs")
        for item in dirs if isinstance(dirs, list) else []:
            if isinstance(item, list) and len(item) == 2 and item[1] and isinstance(item[0], str):
                consider([*parent, item[0]])
    ordered = sorted(found.values(), key=lambda seg: (not seg[-1].casefold().startswith(query),
                                                      seg[-1].casefold(), "/".join(seg).casefold()))
    return ordered[:limit], len(ordered) > limit


@dataclass
class UpdateJob:
    item: MineItem
    url: str = ""                                  # не пусто: это переключение на линию с таким адресом, а не Update
    target: Optional[server.Line] = None           # куда переключаем
    unlock_paths: list = field(default_factory=list)   # свои занятые файлы: перед переключением освобождаются
    created_label: str = ""                        # «Ветка «x» создана.»: переключение сразу после создания
    stage: str = ""                                # "unlock" - идёт освобождение файлов (для текста ошибки)
    cancel: CancelToken = field(default_factory=CancelToken)
    progress: CheckoutProgress = field(default_factory=CheckoutProgress)
    client: Optional[SvnClient] = None             # свой клиент: пересборка настроек его не подменяет
    started: float = field(default_factory=time.monotonic)


@dataclass
class DownloadJob:
    url: str
    name: str
    dest: str
    client: Optional[SvnClient] = None             # свой клиент: пересборка настроек его не подменяет
    display_url: str = ""                          # ссылка в читаемом виде (для списка недавних)
    line: Optional[server.Line] = None             # что скачиваем: основная линия, ветка или версия
    base: str = ""                                 # выбранная папка для проектов (dest лежит внутри)
    dest_fresh: bool = False                       # папки не было или она была пуста до старта: она наша
    cancel: CancelToken = field(default_factory=CancelToken)
    progress: CheckoutProgress = field(default_factory=CheckoutProgress)
    total: Optional[int] = None
    phase: str = "estimate"  # estimate | checkout | exclude
    audit: Optional[audit.AuditResult] = None
    audit_skipped: bool = False                    # проверка на мусор не выполнена (не удалось подсчитать размер)
    started: float = field(default_factory=time.monotonic)
    eta: EtaTracker = field(default_factory=EtaTracker)
