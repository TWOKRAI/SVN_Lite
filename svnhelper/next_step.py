"""«Что делать дальше» для проекта: один источник для подсветки кнопки, строки «Что сделать», подсказки внизу окна и
для `next_action` помощника (docs/PLAN_ui_v4.md, раздел 6, Task U4.6).

Модуль чистый: без tkinter, svn и файлов, русских текстов здесь нет. Он получает факты о проекте (`Facts`) и отвечает
структурой `NextStep`: код состояния, кнопка, что делает щелчок по подсказке, код для ops и числа для текста.
Тексты по ключу `text_key` кладёт слой окна (`gui/hints.py`).

Порядок важности (меньше число - важнее, `PRIORITY`):
    вход не выполнен -> папка проектов не найдена -> конфликт -> ошибка проверки -> папка повреждена, переход не
    закончен, пропала история -> новое у коллег -> свои правки (в том числе «файл изменён, но не занят») ->
    нужный файл занят другим -> проект только на компьютере -> «нажмите ⟳» -> проблема другого проекта -> всё в порядке.
Молчат (quiet): нет связи, идёт операция, идёт проверка: об этом говорят другие места окна. Идущая операция глушит и
вход, и папку проектов: щелчок по подсказке не должен открывать их окна посреди работы.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:       # только для подсказок типов: модуль не тянет за собой ни svn, ни окно
    from .svn_core import WcStatus

# --------------------------------------------------------------------------
# Коды
# --------------------------------------------------------------------------

LOGIN = "login"                  # вход на сервер не выполнен
NO_FOLDER = "no_folder"          # папка проектов не найдена
CONFLICT = "conflict"            # конфликт: свои и чужие правки в одном файле
ERROR = "error"                  # проверка проекта не удалась (не связь)
BROKEN = "broken"                # папка повреждена
SWITCH = "switch"                # переход на другую ветку или версию не закончен
HISTORY = "history"              # пропала история проекта на компьютере, но найдена на диске
GET_NEW = "get_new"              # у коллег есть новое
SAVE = "save"                    # есть свои несохранённые правки
NEED_LOCK = "need_lock"          # файл изменён, но не занят (нужен «Занять файл…»)
TAG_CHANGES = "tag_changes"      # правки в версии (она только для просмотра): сохранить нельзя
LOCKED_OTHER = "locked_other"    # нужный мне файл занят другим человеком
PUBLISH = "publish"              # проект только на этом компьютере: можно отправить на сервер
CHECK = "check"                  # проверка ещё не запускалась: нажмите ⟳
CHECKING = "checking"            # проверка идёт сейчас
OFFLINE = "offline"              # нет связи с сервером
BUSY = "busy"                    # папку сейчас держит другая работа
OTHER = "other"                  # у выбранного проекта всё в порядке, а у другого есть дело
NOTHING = "nothing"              # всё сохранено и получено
READ_ONLY = "read_only"          # версия (только для просмотра), делать нечего
NONE = "none"                    # проект не выбран

CODES = (LOGIN, NO_FOLDER, CONFLICT, ERROR, BROKEN, SWITCH, HISTORY, GET_NEW, SAVE, NEED_LOCK, TAG_CHANGES,
         LOCKED_OTHER, PUBLISH, CHECK, CHECKING, OFFLINE, BUSY, OTHER, NOTHING, READ_ONLY, NONE)

# Ключ кнопки (подсвечивается; значения "resolve" ... "publish" - как у прежней `main_action`).
B_LOGIN, B_CHOOSE_FOLDER = "login", "choose_folder"          # имя в шапке окна, «Выбрать…» у папки проектов
B_RESOLVE, B_REPAIR, B_RESTORE = "resolve", "repair", "restore"
B_COMMIT, B_UPDATE, B_PUBLISH = "commit", "update", "publish"
B_LOCK = "lock"                                              # «Занять файл…» на вкладке «Занятые»

# Что делает щелчок по подсказке.
CLICK_INVOKE = "invoke"                    # нажать кнопку (у её обработчика свой диалог)
CLICK_OPEN_TAB_INVOKE = "open_tab_invoke"  # открыть вкладку `tab` и нажать кнопку (у неё окно выбора)
CLICK_FOCUS = "focus"                      # только перевести фокус на кнопку, действие делает человек
CLICK_OPEN_TAB = "open_tab"                # только открыть вкладку `tab` (на «Изменениях» курсор в поле)
CLICK_SELECT_PROJECT = "select_project"    # выбрать другой проект (код OTHER)
CLICK_NONE = "none"                        # щелчок ничего не делает
SAFE_CLICKS = (CLICK_INVOKE, CLICK_OPEN_TAB_INVOKE)

# Вкладки проекта (ключи `ProjectPanel`).
TAB_CHANGES, TAB_LOCKS, TAB_LINES = "changes", "locks", "lines"

# Коды ops `next_action`: nothing | get_new | save | take_lock | human_resolve_conflict | send_to_server | wait.
OP_NOTHING, OP_GET_NEW, OP_SAVE, OP_TAKE_LOCK = "nothing", "get_new", "save", "take_lock"
OP_HUMAN_RESOLVE, OP_SEND_TO_SERVER, OP_WAIT = "human_resolve_conflict", "send_to_server", "wait"

# Приоритет (меньше - важнее).
PRIORITY = {LOGIN: 10, NO_FOLDER: 20, CONFLICT: 30, ERROR: 35, BROKEN: 40, SWITCH: 40, HISTORY: 40, GET_NEW: 50,
            SAVE: 60, NEED_LOCK: 60, TAG_CHANGES: 60, LOCKED_OTHER: 70, PUBLISH: 80, CHECK: 85, OTHER: 90,
            NOTHING: 100, READ_ONLY: 100, CHECKING: 110, OFFLINE: 110, BUSY: 110, NONE: 120}
PRIORITY_HISTORY_LOST = 40        # «история потеряна и не найдена»: код PUBLISH, но срочность как у HISTORY
PRIORITY_QUIET = 110              # с этого числа подсказка молчит
PRIORITY_OTHER = PRIORITY[OTHER]  # дела других проектов показываются, только если свои хуже этого числа

_QUIET = (CHECKING, OFFLINE, BUSY)
_FILE_SEPARATORS = re.compile(r"[\\/]")


# --------------------------------------------------------------------------
# Вход и выход
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Facts:
    """Что известно о проекте. Всё, кроме `state`, по умолчанию «как у обычного проверенного проекта»."""
    state: Optional[str] = None           # "svn" | "plain" | "broken"; None - проект не выбран
    status: Optional["WcStatus"] = None   # результат проверки; None - ещё не пришёл (или проверка не удалась)
    read_only: bool = False               # версия (тег): правки не сохраняются
    local: bool = False                   # проект с историей на этом компьютере (без сервера)
    history_missing: bool = False         # у такого проекта пропала история
    history_found: bool = False           # пропавшая история лежит на диске в другом месте
    checking: bool = False                # проверка этого проекта идёт сейчас
    error: str = ""                       # проверка не удалась (текст ошибки); со status=None
    offline: bool = False                 # нет связи: у проекта с error это причина ошибки, у plain - общий флаг окна
    busy: bool = False                    # папку держит TortoiseSVN, починка, отправка или другая работа
    server_checked: bool = True           # сервер спрашивали: иначе «новое на сервере» неизвестно (как в ops)
    local_only: bool = False              # (для ops) обычная папка с меткой проекта: ей предлагают отправку на сервер
    login_failed: bool = False            # вход на сервер не выполнен
    folder_missing: bool = False          # папка проектов не найдена
    needed_locked: Optional[tuple] = None  # (файл, кто занял, с какого времени): нужный человеку файл занят другим
    other: Optional[tuple] = None         # (имя проекта, ключ текста NextStep.text_key) самой важной проблемы другого
                                          # проекта; окно берёт его из most_important


@dataclass(frozen=True)
class NextStep:
    """Ответ `next_step`."""
    code: str                       # код состояния (см. CODES): по нему выбирается текст
    button: Optional[str]           # ключ подсвеченной кнопки (B_*); None - подсвечивать нечего
    click: str                      # что делает щелчок по подсказке (CLICK_*)
    op: str                         # код для ops `next_action`
    priority: int                   # важность (меньше - важнее), см. PRIORITY
    text_key: str = ""              # ключ текста: код, а где текст различается - код с уточнением
    tab: Optional[str] = None       # вкладка проекта для CLICK_OPEN_TAB*
    quiet: bool = False             # подсказка молчит (нет связи, идёт операция или проверка)
    data: dict = field(default_factory=dict)   # числа и имена для текста

    @property
    def safe(self) -> bool:
        """Щелчок по подсказке сам нажимает кнопку (у обработчика свой диалог). False - только фокус или переход:
        обработчик пишет на диск без окна подтверждения, и действие делает человек."""
        return self.click in SAFE_CLICKS


def next_step(facts: Facts) -> NextStep:
    """Следующее действие по фактам о проекте (таблица приоритетов в докстринге модуля)."""
    if facts.busy:
        # идёт операция: подсказка молчит и щелчок по ней ничего не делает - даже о входе и папке проектов, иначе
        # щелчок открыл бы вход или выбор папки посреди скачивания (docs/PLAN_ui_v4.md, раздел 6.2). Подсветка
        # остаётся прежней.
        step = _project_step(facts, with_switch=True)
        return _make(BUSY, step.button, CLICK_NONE, facts, data=step.data)
    if facts.login_failed:
        return _make(LOGIN, B_LOGIN, CLICK_INVOKE, facts)
    if facts.folder_missing:
        return _make(NO_FOLDER, B_CHOOSE_FOLDER, CLICK_INVOKE, facts)
    step = _project_step(facts, with_switch=True)
    if (facts.other is not None and not step.quiet and step.priority >= PRIORITY_OTHER
            and step.code != OTHER):
        name, code = facts.other
        return _make(OTHER, None, CLICK_SELECT_PROJECT, facts, data={"project": name, "project_code": code})
    return step


def most_important(steps: Iterable[tuple]) -> Optional[tuple]:
    """Из пар (имя проекта, NextStep) - самая важная, у которой есть что сказать (не молчит и не «всё в порядке»).
    При равной важности остаётся первая. None, если говорить не о чем. Результат годится для `Facts.other`."""
    best = None
    for name, step in steps:
        if step.quiet or step.priority >= PRIORITY_OTHER or step.code == CHECK:   # «нажмите ⟳» - не дело, чтобы звать
            continue
        if best is None or step.priority < best[1].priority:
            best = (name, step)
    return best


def main_button(facts: Facts) -> Optional[str]:
    """Подсвеченная кнопка проекта (то, что раньше считала `main_action`), без входа, папки и чужих проектов."""
    return _project_step(facts, with_switch=True).button


# --------------------------------------------------------------------------
# Разбор состояния одного проекта
# --------------------------------------------------------------------------

def _make(code: str, button: Optional[str], click: str, facts: Facts, *, text_key: str = "",
          tab: Optional[str] = None, priority: Optional[int] = None, data: Optional[dict] = None,
          op: Optional[str] = None) -> NextStep:
    return NextStep(code=code, button=button, click=click, text_key=text_key or code, tab=tab,
                    op=op if op is not None else _ops_action(facts),
                    priority=PRIORITY[code] if priority is None else priority,
                    quiet=code in _QUIET, data=dict(data or {}))


def _idle_button(facts: Facts) -> Optional[str]:
    """Что подсвечено, когда делать нечего или проверка ещё не пришла: у проекта с историей на компьютере «Сохранить
    у себя», у остальных «Получить новое» (так было у `main_action`)."""
    return B_COMMIT if facts.local else B_UPDATE


def _basename(path: str) -> str:
    return _FILE_SEPARATORS.split(path.rstrip("\\/"))[-1] if path else ""


def _changes(status: "WcStatus") -> int:
    """Сколько своих правок (без конфликтных файлов) - только по списку `local_changes`, как у прежних `main_action` и
    ops. Счётчик `status.modified` сюда не идёт: в нём и папки `incomplete`/`obstructed` (прерванное «Получить
    новое», чужой файл на месте нужного), которые сохранять нечего."""
    listed = sum(1 for c in status.local_changes if c.kind != "conflicted")
    return listed or len(status.local_changes)


def _has_changes(status: "WcStatus") -> bool:
    """Есть свои правки, которые можно сохранить (`local_changes`, в том числе новые файлы)."""
    return bool(status.local_changes)


def _first_change(status: "WcStatus", *, kind: Optional[str] = None, needs_lock: bool = False) -> str:
    for change in status.local_changes:
        if kind is not None and change.kind != kind:
            continue
        if needs_lock and not (change.needs_lock and not change.locked_by_me):
            continue
        return _basename(change.path)
    return ""


def _incoming(facts: Facts, status: "WcStatus") -> int:
    """Сколько файлов нового у коллег; без проверки сервера неизвестно, поэтому 0."""
    return max(len(status.incoming_files), status.incoming) if facts.server_checked else 0


def _needs_lock_missing(status: "WcStatus") -> bool:
    return any(c.needs_lock and not c.locked_by_me for c in status.local_changes)


def _project_step(facts: Facts, with_switch: bool) -> NextStep:
    """Следующее действие по самому проекту (без входа, папки, занятости и чужих проектов)."""
    state = facts.state
    if state == "plain":
        return _plain_step(facts)
    if state == "broken":
        return _make(BROKEN, B_REPAIR, CLICK_FOCUS, facts)
    if state != "svn":
        return _make(NONE, None, CLICK_NONE, facts)
    if facts.local and facts.history_missing:
        return _history_lost_step(facts)
    status = facts.status
    if status is None:
        return _unchecked_step(facts)
    if facts.local:
        return _local_step(facts, status)
    return _server_step(facts, status, with_switch)


def _plain_step(facts: Facts) -> NextStep:
    """Обычная папка без истории и сервера: её можно отправить на сервер."""
    key = "publish_offline" if facts.offline else PUBLISH
    step = _make(PUBLISH, B_PUBLISH, CLICK_INVOKE, facts, text_key=key, data={"offline": facts.offline})
    return replace(step, quiet=True) if facts.offline else step      # без связи об этом говорит полоса «Нет связи»


def _history_lost_step(facts: Facts) -> NextStep:
    """История проекта на компьютере пропала: найдена на диске - «Восстановить историю», иначе «Отправить на сервер…»."""
    if facts.history_found:
        return _make(HISTORY, B_RESTORE, CLICK_FOCUS, facts, op=OP_HUMAN_RESOLVE)
    return _make(PUBLISH, B_PUBLISH, CLICK_INVOKE, facts, text_key="publish_history_lost",
                 priority=PRIORITY_HISTORY_LOST, data={"history_missing": True}, op=OP_HUMAN_RESOLVE)


def _unchecked_step(facts: Facts) -> NextStep:
    """Проверки ещё нет: идёт сейчас, не удалась (нет связи или другая ошибка) или не запускалась."""
    button = _idle_button(facts)
    if facts.checking:
        return _make(CHECKING, button, CLICK_NONE, facts)
    if facts.error and facts.offline:
        return _make(OFFLINE, button, CLICK_NONE, facts, data={"error": facts.error})
    if facts.error:
        return _make(ERROR, button, CLICK_NONE, facts, data={"error": facts.error})
    return _make(CHECK, button, CLICK_NONE, facts)


def _local_step(facts: Facts, status: "WcStatus") -> NextStep:
    """Проект с историей на компьютере: правки сохраняются «у себя», чистый проект можно отправить на сервер."""
    if _has_changes(status):
        return _make(SAVE, B_COMMIT, CLICK_OPEN_TAB, facts, text_key="save_local", tab=TAB_CHANGES,
                     data={"files": _changes(status), "local": True})
    return _make(PUBLISH, B_PUBLISH, CLICK_INVOKE, facts, text_key="publish_local", op=OP_NOTHING,
                 data={"local": True})


def _server_step(facts: Facts, status: "WcStatus", with_switch: bool) -> NextStep:
    """Проект с сервера: конфликт, незаконченный переход, новое у коллег, свои правки."""
    data = {"files": _changes(status), "my_locks": len(status.my_locks)}
    if status.conflicts:
        data.update(conflicts=status.conflicts, file=_first_change(status, kind="conflicted"))
        return _make(CONFLICT, B_RESOLVE, CLICK_INVOKE, facts, data=data)
    if with_switch and status.switched:
        return _make(SWITCH, _idle_button(facts), CLICK_OPEN_TAB, facts, tab=TAB_LINES, data=data)
    incoming = _incoming(facts, status)
    changes = _has_changes(status)
    if incoming:
        data.update(incoming=incoming, with_changes=changes)
        return _make(GET_NEW, B_UPDATE, CLICK_FOCUS, facts, data=data,
                     text_key="get_new_with_changes" if changes else GET_NEW)
    if changes:
        if facts.read_only:
            return _make(TAG_CHANGES, B_UPDATE, CLICK_OPEN_TAB, facts, tab=TAB_LINES, data=data)
        if _needs_lock_missing(status):
            data["file"] = _first_change(status, needs_lock=True)
            return _make(NEED_LOCK, B_LOCK, CLICK_OPEN_TAB_INVOKE, facts, tab=TAB_LOCKS, data=data)
        return _make(SAVE, B_COMMIT, CLICK_OPEN_TAB, facts, tab=TAB_CHANGES, data=data)
    if facts.needed_locked and not facts.read_only:
        file, who, since = facts.needed_locked
        data.update(file=file, who=who, since=since)
        return _make(LOCKED_OTHER, None, CLICK_OPEN_TAB, facts, tab=TAB_LOCKS, data=data)
    if not facts.server_checked:
        return _make(OFFLINE, B_UPDATE, CLICK_NONE, facts, data=data)   # новое на сервере неизвестно
    if facts.read_only:
        return _make(READ_ONLY, B_UPDATE, CLICK_NONE, facts, data=data)
    return _make(NOTHING, B_UPDATE, CLICK_NONE, facts, data=data)


# --------------------------------------------------------------------------
# Код для ops
# --------------------------------------------------------------------------

def _ops_action(facts: Facts) -> str:
    """`next_action` помощника. Совпадает с прежним `ops.projects._next_action`: занято - wait; конфликт - ручной
    разбор; новое - get_new (и при своих правках); правки - save или take_lock (в версии nothing); повреждённая папка
    и пропавшая история - ручной разбор; обычная папка с меткой проекта - send_to_server; остальное - nothing.
    Незаконченный переход здесь не учитывается: ops о нём не знает."""
    if facts.busy:
        return OP_WAIT
    if facts.state == "broken" or (facts.local and facts.history_missing):
        return OP_HUMAN_RESOLVE
    if facts.state == "plain":
        return OP_SEND_TO_SERVER if facts.local_only else OP_NOTHING
    status = facts.status
    if facts.state != "svn" or status is None:
        return OP_NOTHING
    if status.conflicts:
        return OP_HUMAN_RESOLVE
    if _incoming(facts, status):
        return OP_GET_NEW
    if _has_changes(status):
        if facts.read_only:
            return OP_NOTHING
        return OP_TAKE_LOCK if _needs_lock_missing(status) else OP_SAVE
    return OP_NOTHING
