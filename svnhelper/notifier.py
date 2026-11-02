"""Фоновая проверка и решения «что и когда показать» (Task E.2). Без tkinter.

Фоновый поток только читает состояние через svn (check_projects) и возвращает снимок. Решает, показывать ли
уведомление, класс Notifier: он ничего не рисует и не ходит в сеть, поэтому проверяется тестами без окна.
Показывает уведомления главный поток (svnhelper/gui/notify_ui.py и popup.py).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .config import FilterRules, is_newer_version, parse_clock, Settings
from . import server
from .svn_core import ErrorKind, SvnClient, SvnError

log = logging.getLogger("svnhelper.notifier")

KIND_INCOMING = "incoming"
KIND_UNSAVED = "unsaved"
KIND_CONFLICT = "conflict"
KIND_APP_UPDATE = "app_update"
KIND_INFO = "info"

TAB_INCOMING = "incoming"      # ключи вкладок колонки «Состояние проекта»
TAB_CHANGES = "changes"

GROUP_FROM = 4                 # столько и больше уведомлений одного вида склеиваются в одно
POSTPONE_HOURS = 1             # на сколько откладывает кнопка «Позже»
SNOOZE_HOURS = 2               # пауза «не беспокоить» из меню значка
MAX_AUTHORS = 3


# --------------------------------------------------------------------------
# Проверка проектов (фоновый поток, только svn)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectInfo:
    """Что узнали о проекте при последней проверке."""
    path: str
    name: str
    incoming: int = 0              # сколько файлов изменено на сервере
    authors: str = ""              # кто их менял: «Иванов» или «Иванов, Петров»
    unsaved: int = 0               # сколько несохранённых правок
    conflicts: int = 0
    read_only: bool = False        # версия (тег): в неё ничего не сохраняют
    error: str = ""                # не пусто: проект не проверили (причина для журнала)


@dataclass
class CheckResult:
    projects: list[ProjectInfo] = field(default_factory=list)
    offline: bool = False          # сервер недоступен: проверку прервали
    auth_failed: bool = False      # сервер просит пароль; в фоне окно входа не показываем


def norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def discover_projects(dirs: list[str]) -> list[tuple[str, str]]:
    """(путь, имя) скачанных проектов: подпапки с .svn в папках для проектов. Без повторов."""
    found: dict[str, tuple[str, str]] = {}
    for base in dirs:
        try:
            names = sorted(os.listdir(base), key=str.casefold)
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if not name.startswith(".") and os.path.isdir(os.path.join(path, ".svn")):
                found.setdefault(norm(path), (os.path.normpath(path), name))
    return list(found.values())


def _authors_text(client: SvnClient, path: str) -> str:
    """Кто менял проект на сервере. Не вышло узнать - пустая строка (уведомление скажет без имени)."""
    try:
        names = [e.author for e in client.incoming_log(path) if e.author]
    except SvnError as exc:
        log.warning("Не удалось узнать, кто менял проект %s на сервере (%s)", path, exc.kind.value)
        return ""
    unique = list(dict.fromkeys(names))
    return ", ".join(unique[:MAX_AUTHORS]) + (" и др." if len(unique) > MAX_AUTHORS else "")


def check_project(client: SvnClient, path: str, name: str, rules: Optional[FilterRules] = None) -> ProjectInfo:
    """Один проект: `svn status -u` (+ `svn log` для имён авторов, если есть новое). Бросает SvnError."""
    status = client.status(path, check_server=True, rules=rules)
    incoming = max(len(status.incoming_files), status.incoming)
    read_only = False
    try:
        info = client.info(path)
        found = server.line_from_url(info.url, info.root)
        read_only = bool(found and found[1].read_only)
    except SvnError as exc:
        log.warning("Не удалось определить ветку проекта %s (%s)", path, exc.kind.value)
    return ProjectInfo(path, name, incoming=incoming, authors=_authors_text(client, path) if incoming else "",
                       unsaved=len(status.local_changes), conflicts=status.conflicts, read_only=read_only)


def check_projects(client: SvnClient, projects: list[tuple[str, str]], rules: Optional[FilterRules] = None,
                   skip: Callable[[str], bool] = lambda _path: False) -> CheckResult:
    """Проверяет проекты по очереди. Обрыв связи или запрос пароля останавливают всю проверку."""
    result = CheckResult()
    for path, name in projects:
        if skip(path):
            continue
        try:
            result.projects.append(check_project(client, path, name, rules))
        except SvnError as exc:
            if exc.kind is ErrorKind.AUTH:
                result.auth_failed = True
                break
            if exc.kind is ErrorKind.NO_CONNECTION:
                result.offline = True
                break
            log.warning("Фоновая проверка: %s не проверен (%s)", path, exc.kind.value)
            result.projects.append(ProjectInfo(path, name, error=exc.kind.value))
        except Exception:
            log.exception("Фоновая проверка: ошибка в проекте %s", path)
            result.projects.append(ProjectInfo(path, name, error="other"))
    return result


def check_app_update(latest_version: str, current_version: str) -> str:
    """Версия новее текущей, если администратор объявил её (latest_version из общего svn_helper.json,
    который программа перечитывает раз в час), иначе пустая строка."""
    return latest_version.strip() if is_newer_version(latest_version, current_version) else ""


# --------------------------------------------------------------------------
# Решения: что показывать
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Notice:
    key: str                       # по ключу сравниваем «это уже показывали»
    kind: str
    title: str
    text: str
    path: str = ""                 # проект, который откроет кнопка «Открыть»
    tab: str = ""                  # вкладка проекта
    signature: str = ""            # изменилась - уведомление новое (например, число новых файлов)
    important: bool = False        # конфликт и несохранённые правки: «только важное» их не прячет
    members: tuple = ()            # склеенное уведомление: ((ключ, подпись), ...)

    @property
    def parts(self) -> tuple:
        return self.members or ((self.key, self.signature),)


def in_quiet_hours(now: datetime, enabled: bool, start: str, end: str, weekends: bool) -> bool:
    """Попадает ли now в «Не беспокоить». Промежуток может переходить через полночь (18:00-09:00)."""
    if not enabled:
        return False
    if weekends and now.weekday() >= 5:
        return True
    a, b = parse_clock(start), parse_clock(end)
    if not a or not b or a == b:
        return False
    minutes = now.hour * 60 + now.minute
    lo, hi = a[0] * 60 + a[1], b[0] * 60 + b[1]
    return lo <= minutes < hi if lo < hi else (minutes >= lo or minutes < hi)


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    return forms[1] if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else forms[2]


class Notifier:
    """Помнит, что уже показано, и решает, что показать сейчас. Время берёт у now() (в тестах подменяется)."""

    def __init__(self, get_settings: Callable[[], Settings], now: Callable[[], datetime] = datetime.now):
        self._settings = get_settings
        self._now = now
        self._infos: list[ProjectInfo] = []
        self._shown: dict[str, tuple[str, float]] = {}      # ключ -> (подпись, когда показали)
        self._unsaved_since: dict[str, float] = {}          # проект -> когда впервые увидели несохранённые правки
        self._deferred: dict[str, float] = {}               # ключ -> до какого времени отложено («Позже»)
        self._snooze_until = 0.0

    # -- время ----------------------------------------------------------------

    def _ts(self) -> float:
        return self._now().timestamp()

    timestamp = _ts      # для вызывающего кода: «когда последний раз проверяли»

    @property
    def infos(self) -> list[ProjectInfo]:
        return list(self._infos)

    def due_for_check(self, last_check_ts: Optional[float]) -> bool:
        """Пора ли проверять по таймеру. Режим «только вручную» (0) - никогда."""
        minutes = self._settings().notify_check_min
        return minutes > 0 and (last_check_ts is None or self._ts() - last_check_ts >= minutes * 60)

    def snooze(self, hours: float = SNOOZE_HOURS) -> None:
        self._snooze_until = self._ts() + hours * 3600

    @property
    def snooze_until(self) -> Optional[datetime]:
        """До какого времени включено «Не беспокоить» (None, если не включено)."""
        return datetime.fromtimestamp(self._snooze_until) if self.snoozed else None

    def cancel_snooze(self) -> None:
        self._snooze_until = 0.0

    @property
    def snoozed(self) -> bool:
        return self._ts() < self._snooze_until

    def quiet_now(self) -> bool:
        s = self._settings()
        return in_quiet_hours(self._now(), s.notify_quiet, s.notify_quiet_from, s.notify_quiet_to,
                              s.notify_quiet_weekends)

    # -- состояние -------------------------------------------------------------

    def update(self, infos: list[ProjectInfo]) -> None:
        """Новый снимок проектов. Обновляет «правки не сохранены с …» и забывает прошлые уведомления,
        повод для которых исчез (получили новое - следующее новое снова будет уведомлением)."""
        now = self._ts()
        checked = [i for i in infos if not i.error]
        for info in checked:
            key = norm(info.path)
            if info.unsaved:
                self._unsaved_since.setdefault(key, now)
            else:
                self._unsaved_since.pop(key, None)
                self._shown.pop(KIND_UNSAVED + ":" + key, None)
            if not info.incoming:
                self._shown.pop(KIND_INCOMING + ":" + key, None)
            if not info.conflicts:
                self._shown.pop(KIND_CONFLICT + ":" + key, None)
        known = {norm(i.path) for i in infos}
        for key in [k for k in self._unsaved_since if k not in known]:   # папку убрали
            del self._unsaved_since[key]
        self._infos = infos

    def mark_shown(self, notice: Notice) -> None:
        now = self._ts()
        for key, signature in notice.parts:
            self._shown[key] = (signature, now)

    def postpone(self, notice: Notice, hours: float = POSTPONE_HOURS) -> None:
        """«Позже»: показать снова не раньше чем через hours (даже если подпись не изменилась)."""
        until = self._ts() + hours * 3600
        for key, _signature in notice.parts:
            self._deferred[key] = until
            self._shown.pop(key, None)

    # -- кандидаты ---------------------------------------------------------------

    def _fresh(self, key: str, signature: str, force: bool) -> bool:
        """Не показывали такое (ключ и подпись) и не отложено кнопкой «Позже»."""
        if force:
            return True
        if self._deferred.get(key, 0.0) > self._ts():
            return False
        seen = self._shown.get(key)
        return seen is None or seen[0] != signature

    def candidates(self, latest_version: str = "", current_version: str = "", force: bool = False) -> list[Notice]:
        """Что показать прямо сейчас. force=True («Проверить сейчас»): всё, что есть, без учёта «уже показывали»,
        пауз и «не беспокоить». Показанное нужно отметить через mark_shown."""
        s = self._settings()
        if not force and (not s.notify_enabled or self.snoozed or self.quiet_now()):
            return []
        muted = {norm(p) for p in s.notify_muted}
        notices: list[Notice] = []
        now = self._ts()
        for info in self._infos:
            key = norm(info.path)
            if info.error or key in muted:
                continue
            name = info.name
            if info.conflicts:
                k = KIND_CONFLICT + ":" + key
                if self._fresh(k, str(info.conflicts), force):
                    notices.append(Notice(
                        k, KIND_CONFLICT, name, f"В проекте «{name}» есть конфликт: вы и другой человек изменили "
                        "одно и то же. Разберите его, пока не потеряли правки.", info.path, TAB_CHANGES,
                        str(info.conflicts), important=True))
            if info.incoming and (s.notify_incoming or force) and not (s.notify_important_only and not force):
                k = KIND_INCOMING + ":" + key
                if self._fresh(k, str(info.incoming), force):
                    who = f" от {info.authors}" if info.authors else ""
                    files = f"{info.incoming} {plural(info.incoming, ('файл', 'файла', 'файлов'))}"
                    notices.append(Notice(k, KIND_INCOMING, name, f"В проекте «{name}» есть новое{who} ({files}).",
                                          info.path, TAB_INCOMING, str(info.incoming)))
            if info.unsaved and not info.conflicts and not info.read_only and s.notify_unsaved_hours:
                since = self._unsaved_since.get(key, now)
                k = KIND_UNSAVED + ":" + key
                if force or self._unsaved_due(k, since, now, s):
                    notices.append(Notice(
                        k, KIND_UNSAVED, name, f"В проекте «{name}» есть несохранённые правки — "
                        "сохраните на сервер.", info.path, TAB_CHANGES, "", important=True))
        latest = check_app_update(latest_version, current_version)
        if latest and (s.notify_app_update or force) and not (s.notify_important_only and not force):
            if self._app_update_due(latest, now, s, force):
                notices.append(Notice("app_update", KIND_APP_UPDATE, "SVN-lite",
                                      f"Доступна новая версия SVN-lite {latest}.", signature=latest))
        return self._group(notices)

    def _unsaved_due(self, key: str, since: float, now: float, s: Settings) -> bool:
        if self._deferred.get(key, 0.0) > now or now - since < s.notify_unsaved_hours * 3600:
            return False
        seen = self._shown.get(key)
        return seen is None or now - seen[1] >= s.notify_repeat_hours * 3600

    def _app_update_due(self, latest: str, now: float, s: Settings, force: bool) -> bool:
        if force:
            return True
        if self._deferred.get("app_update", 0.0) > now:
            return False
        seen = self._shown.get("app_update")
        return seen is None or seen[0] != latest or now - seen[1] >= s.notify_app_update_days * 86400

    @staticmethod
    def _group(notices: list[Notice]) -> list[Notice]:
        """Много однотипных уведомлений разом (например, после выходных) склеиваются в одно."""
        result: list[Notice] = []
        for kind, title, tab in ((KIND_INCOMING, "Новое на сервере", TAB_INCOMING),
                                 (KIND_UNSAVED, "Несохранённые правки", TAB_CHANGES)):
            same = [n for n in notices if n.kind == kind]
            if len(same) < GROUP_FROM:
                continue
            names = ", ".join(n.title for n in same[:3]) + (" и др." if len(same) > 3 else "")
            text = (f"Новое на сервере в {len(same)} проектах: {names}." if kind == KIND_INCOMING else
                    f"Несохранённые правки в {len(same)} проектах: {names}. Сохраните их на сервер.")
            notices = [n for n in notices if n.kind != kind]
            result.append(Notice(kind + ":*", kind, title, text, same[0].path, tab,
                                 important=kind == KIND_UNSAVED, members=tuple(p for n in same for p in n.parts)))
        return notices + result


def info_notice() -> Notice:
    """Итог ручной проверки, когда рассказывать нечего."""
    return Notice("info", KIND_INFO, "Проверка завершена", "Нового на сервере нет, несохранённых правок нет.")


def sample_notice() -> Notice:
    """Пробное окошко из «Параметров»."""
    return Notice("test", KIND_INFO, "Пробное уведомление",
                  "Так будут выглядеть уведомления: здесь, в правом нижнем углу экрана.")
