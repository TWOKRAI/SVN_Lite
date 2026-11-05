"""Журнал действий ИИ-помощника для окна (Task AG.8): разбор строк, подписи, фильтры, связь отмен и сама отмена.

Модуль не знает про tkinter: окно (`gui/agent_journal_dialog.py`) только рисует то, что он подготовил, поэтому лента и
отмена проверяются тестами без экрана.

Источник: файлы `agent/audit_ГГГГ-ММ.jsonl`, их пишет `ops/audit.py` (одна строка на вызов; чтение тоже). Старые строки
без новых полей (`undone_rev`) читаются как есть. Пароля в журнале нет.

Отмена: окно просит слой ops (`rollback_plan`, затем `rollback_apply` с токеном) от имени клиента `window` с политикой
`WindowPolicy`. Решение принимает человек: окно «Да/Нет» с перечнем файлов стоит между планом и применением. Сам слой
ops проверяет всё остальное (своё ли сохранение, не менял ли файлы кто-то потом, нет ли несохранённых правок) и
занимает папку межпроцессным замком, поэтому окно не берёт `folderlock.hold` второй раз: вложенный замок отказал бы.
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import agent_policy
from .ops import audit, texts
from .ops.context import OpsContext
from .ops.errors import OpsError
from .ops.registry import run
from .ops.result import EXIT_NO_RIGHTS, Result

# что показывать
FILTER_CHANGES, FILTER_ALL, FILTER_READ = "changes", "all", "read"
FILTERS = ((FILTER_CHANGES, "Только изменения"), (FILTER_ALL, "Все действия"), (FILTER_READ, "Только чтение"))
DEFAULT_FILTER = FILTER_CHANGES
# за какой срок: (месяцев, подпись); журнал хранится 12 месяцев (audit.KEEP_MONTHS)
PERIODS = ((1, "Этот месяц"), (3, "3 месяца"), (12, "Год"))
DEFAULT_MONTHS = 3

CLIENT_TEXTS = {"cli": "Консоль (CLI)", "mcp": "MCP", "window": "Окно SVN-lite (вы)", "test": "Проверка"}
# что сделано: операция журнала -> глагол прошедшего времени
DONE_TEXTS = {
    "get_new_apply": "получил новое с сервера",
    "save_apply": "сохранил изменения",
    "lock_apply": "занял файлы",
    "unlock_apply": "освободил файлы",
    "branch_apply": "создал ветку или версию",
    "switch_apply": "переключил проект на другую ветку или версию",
    "rollback_apply": "отменил сохранение",
    "create_project_apply": "создал проект",
    "publish_apply": "отправил папку на сервер",
}
REDO_TEXT = "вернул отменённое сохранение"             # отмена отмены читается как возврат
PLAN_TEXT = "составил план: {title}"
READ_TEXT = "посмотрел: {title}"
UNKNOWN_TEXT = "действие «{name}»"
NOT_UNDOABLE = "не отменяется"
OK_TEXT, PLAN_OK_TEXT, FAILED_TEXT = "выполнено", "план составлен", "не выполнено"
NO_PROJECT_TEXT = "в журнале не записан проект"
# вид правки в списке файлов: как пишет журнал (русские слова у занятия и веток, буквы у отмены)
ACTION_TEXTS = {"A": "был добавлен, будет удалён", "M": "был изменён, вернётся прежний вид",
                "D": "был удалён, вернётся", "R": "был заменён, вернётся прежний"}
FILES_SHOWN = 3                  # файлов в ячейке ленты
DIALOG_FILES = 15                # файлов в окне подтверждения

UNDO_OPERATION, REDO_SOURCE = "save_apply", "rollback_apply"
UNDOABLE = (UNDO_OPERATION, REDO_SOURCE)


# --------------------------------------------------------------------------
# Разбор строк журнала
# --------------------------------------------------------------------------

def _int(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class Entry:
    """Строка журнала. Отсутствующие и неверные поля (старые строки, правка вручную) дают пустые значения."""
    when: Optional[datetime]
    client: str
    client_info: str
    user: str
    operation: str
    read_only: bool
    project: str
    files: tuple                 # ((путь, вид), ...)
    files_total: int
    plan_id: str
    outcome: str
    exit_code: int
    rev_before: Optional[int]
    rev_after: Optional[int]
    undone_rev: Optional[int]
    comment: str

    @property
    def ok(self) -> bool:
        return self.outcome == "ok" and self.exit_code == 0

    @property
    def is_plan(self) -> bool:
        return self.operation.endswith("_plan")


def parse_entry(raw: dict) -> Entry:
    """Словарь строки журнала -> Entry. Не бросает исключений на любом словаре."""
    try:
        when = datetime.fromisoformat(_text(raw.get("ts")))
    except ValueError:
        when = None
    files = []
    for item in raw.get("files") if isinstance(raw.get("files"), list) else ():
        if isinstance(item, dict):
            files.append((_text(item.get("path")), _text(item.get("action"))))
    total = _int(raw.get("files_total"))
    return Entry(when, _text(raw.get("client")), _text(raw.get("client_info")), _text(raw.get("user")),
                 _text(raw.get("operation")), raw.get("read_only") is not False, _text(raw.get("project")),
                 tuple(files), total if total is not None else len(files), _text(raw.get("plan_id")),
                 _text(raw.get("outcome")), _int(raw.get("exit_code")) or 0, _int(raw.get("rev_before")),
                 _int(raw.get("rev_after")), _int(raw.get("undone_rev")), _text(raw.get("comment")))


def month_starts(count: int, now: Optional[datetime] = None) -> list:
    """Первые числа последних count месяцев (с текущего), свежие первыми."""
    now = now or datetime.now()
    index = now.year * 12 + now.month - 1
    return [datetime((index - back) // 12, (index - back) % 12 + 1, 1) for back in range(max(1, count))]


def load_entries(months: int = DEFAULT_MONTHS, now: Optional[datetime] = None) -> list:
    """Строки журнала за последние months месяцев по времени записи (старые первыми). Повреждённые строки пропущены."""
    entries = []
    for start in reversed(month_starts(months, now)):
        entries.extend(parse_entry(raw) for raw in audit.read_entries(start))
    return entries


# --------------------------------------------------------------------------
# Подписи
# --------------------------------------------------------------------------

def when_text(entry: Entry) -> str:
    if entry.when is None:
        return "—"
    moment = entry.when.astimezone() if entry.when.tzinfo else entry.when
    return moment.strftime("%d.%m.%Y %H:%M")


def who_text(entry: Entry) -> str:
    base = CLIENT_TEXTS.get(entry.client, entry.client or "—")
    return f"{base}: {entry.client_info}" if entry.client_info and entry.client != "window" else base


def project_text(entry: Entry) -> str:
    """Проект как его назвал помощник; если это путь, то имя последней папки."""
    name = entry.project.strip().rstrip("\\/")
    return os.path.basename(name.replace("\\", "/")) if ("\\" in name or "/" in name) else name


def _title(operation: str) -> str:
    """Название операции словами сотрудника из реестра (для чтения и планов)."""
    name = operation[:-5] if operation.endswith("_plan") else operation
    meta = texts.OPERATIONS.get(name)
    if meta is None:
        from .ops import texts_write
        meta = texts_write.WRITE_OPERATIONS.get(name)
    return (meta["title_ru"] if meta else "").lower()


def what_text(entry: Entry, is_redo: bool = False) -> str:
    if entry.operation in DONE_TEXTS:
        return REDO_TEXT if is_redo and entry.operation == "rollback_apply" else DONE_TEXTS[entry.operation]
    title = _title(entry.operation)
    if not title:
        return UNKNOWN_TEXT.format(name=entry.operation or "?")
    return (PLAN_TEXT if entry.is_plan else READ_TEXT).format(title=title)


def files_text(entry: Entry, shown: int = FILES_SHOWN) -> str:
    names = [os.path.basename(path.replace("\\", "/")) or path for path, _action in entry.files]
    if not names:
        return ""
    text = ", ".join(names[:shown])
    rest = max(entry.files_total, len(names)) - len(names[:shown])
    return f"{text} и ещё {rest}" if rest > 0 else text


def number_text(entry: Entry) -> str:
    return f"№ {entry.rev_after}" if entry.rev_after is not None and entry.ok and not entry.is_plan else ""


def outcome_text(entry: Entry) -> str:
    if entry.ok:
        return PLAN_OK_TEXT if entry.is_plan else OK_TEXT
    meta = texts.ERRORS.get(entry.outcome)
    return meta[0].rstrip(".") if meta else FAILED_TEXT


# --------------------------------------------------------------------------
# Лента
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    """Строка ленты для окна: готовые подписи и что с ней можно сделать."""
    index: int                   # номер Entry в списке, из которого построена лента
    entry: Entry
    when: str
    who: str
    what: str
    project: str
    files: str
    number: str
    outcome: str
    link: str                    # «отменено сохранением № N», «отменяет сохранение № N», «не отменяется»
    can_undo: bool               # кнопка «Отменить это сохранение»
    can_redo: bool               # кнопка «Вернуть отмену»
    is_change: bool
    failed: bool


def undone_map(entries: list) -> dict:
    """{номер сохранения: номер отмены, обратившей его}: по удавшимся отменам; при нескольких побеждает последняя."""
    undone = {}
    for entry in entries:
        if entry.operation == REDO_SOURCE and entry.ok and entry.undone_rev is not None and entry.rev_after is not None:
            undone[entry.undone_rev] = entry.rev_after
    return undone


def is_redo(entry: Entry, entries_by_rev: dict) -> bool:
    """Отмена, обратившая другую отмену: вернула прежнее состояние."""
    if entry.operation != REDO_SOURCE or entry.undone_rev is None:
        return False
    source = entries_by_rev.get(entry.undone_rev)
    return source is not None and source.operation == REDO_SOURCE


def _link(entry: Entry, undone: dict, by_rev: dict) -> str:
    parts = []
    if entry.operation == REDO_SOURCE and entry.ok and entry.undone_rev is not None:
        parts.append(f"отменяет сохранение № {entry.undone_rev}")
    if entry.rev_after is not None and entry.ok and entry.rev_after in undone:
        by = undone[entry.rev_after]
        if by in undone:
            parts.append(f"отмена № {by} сама отменена сохранением № {undone[by]}: снова действует")
        else:
            parts.append(f"отменено сохранением № {by}")
    if parts:
        return "; ".join(parts)
    if not entry.read_only and entry.ok and entry.operation not in UNDOABLE:
        return NOT_UNDOABLE
    return ""


def keep(entry: Entry, flt: str) -> bool:
    if flt == FILTER_ALL:
        return True
    return (not entry.read_only) if flt == FILTER_CHANGES else entry.read_only


def build_rows(entries: list, flt: str = DEFAULT_FILTER) -> list:
    """Лента: свежие действия первыми. Связи отмен считаются по всем записям, фильтр только прячет строки."""
    undone = undone_map(entries)
    by_rev = {e.rev_after: e for e in entries if e.rev_after is not None and e.ok and not e.read_only}
    rows = []
    for index, entry in enumerate(entries):
        if not keep(entry, flt):
            continue
        movable = (entry.operation in UNDOABLE and entry.ok and not entry.read_only and entry.rev_after is not None
                   and entry.rev_after not in undone)
        rows.append(Row(index, entry, when_text(entry), who_text(entry), what_text(entry, is_redo(entry, by_rev)),
                        project_text(entry), files_text(entry), number_text(entry), outcome_text(entry),
                        _link(entry, undone, by_rev), movable and entry.operation == UNDO_OPERATION,
                        movable and entry.operation == REDO_SOURCE, not entry.read_only, not entry.ok))
    rows.sort(key=lambda row: (row.entry.when.timestamp() if row.entry.when else 0, row.index), reverse=True)
    return rows


def empty_text(flt: str) -> str:
    return {FILTER_CHANGES: "За выбранный срок помощник ничего не менял.",
            FILTER_READ: "За выбранный срок помощник ничего не смотрел."}.get(flt, "За выбранный срок записей нет.")


def detail_text(row: Row) -> str:
    """Подробности выбранной строки: все файлы (сколько записано), комментарий, откуда вызов."""
    entry = row.entry
    lines = [f"{row.when}. {row.who}: {row.what}.", f"Проект: {row.project or '—'}"]
    if entry.rev_after is not None and entry.ok and not entry.is_plan:
        lines[1] += f". Сохранение № {entry.rev_after}"
    if entry.user:
        lines.append(f"Сотрудник: {entry.user}")
    lines.append(f"Итог: {row.outcome}" + (f". {row.link}" if row.link else ""))
    if entry.comment:
        lines.append(f"Комментарий: {entry.comment}")
    if entry.files:
        lines.append("Файлы:")
        for path, action in entry.files:
            lines.append(f"  {path}" + (f" ({ACTION_TEXTS.get(action, action)})" if action else ""))
        rest = entry.files_total - len(entry.files)
        if rest > 0:
            lines.append(f"  … и ещё {rest}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Отмена от имени человека
# --------------------------------------------------------------------------

class WindowPolicy(agent_policy.Policy):
    """Политика для действий, которые человек сам решил сделать в окне: разрешена только отмена сохранения, режим
    «с подтверждением» (между планом и применением человек отвечает «Да»). Потолок администратора и список папок
    помощника сюда не относятся (это не помощник), лимит записей в час не расходуется, комментарий без пометки «по
    просьбе помощника». Проект всё равно ищется по настройкам программы."""

    def check(self, action: str, project: object = None, base_dirs=None, line: str = "", *, consume: bool) -> None:
        if action not in self.allow:
            agent_policy.deny("allow", action=action)
        if line == "tag":
            raise OpsError("read_only_line", EXIT_NO_RIGHTS)


def window_policy() -> WindowPolicy:
    return WindowPolicy(file_mode=agent_policy.MODE_CONFIRM, ceiling="write", allow=("rollback",),
                        max_files_per_save=1000, mark_comments=False, signature=agent_policy.SIG_OK)


def window_context(store, config, client) -> OpsContext:
    """Контекст ops для окна. Клиент окна (с входом на сервер) подходит: пароль слой ops нигде не хранит и не пишет;
    в копии настроек пароль обнулён."""
    return OpsContext(store, dataclasses.replace(config, password=""), client, window_policy(), client_name="window")


def target_revision(entry: Entry) -> Optional[int]:
    return entry.rev_after


def plan_undo(ctx: OpsContext, entry: Entry) -> Result:
    """Шаг 1: составить план отмены (ничего не меняет). Нужен проект и номер сохранения."""
    if not entry.project.strip() or target_revision(entry) is None:
        return Result.failure("rollback_plan", _no_project_error())
    return run(ctx, "rollback_plan", project=entry.project, revision=target_revision(entry))


def apply_undo(ctx: OpsContext, entry: Entry, plan: Result) -> Result:
    """Шаг 2: выполнить отмену по токену плана. Вызывается только после ответа человека «Да»."""
    return run(ctx, "rollback_apply", project=entry.project, revision=target_revision(entry),
               token=plan.data.get("plan_id", ""))


def _no_project_error():
    from .ops.result import ErrorInfo
    return ErrorInfo("not_found", NO_PROJECT_TEXT[0].upper() + NO_PROJECT_TEXT[1:] + ".", "", False, 1)


def confirm_text(row: Row, plan: Result) -> str:
    """Вопрос человеку перед отменой: что и в каком проекте будет сделано, какие файлы затронет."""
    entry = row.entry
    revision = target_revision(entry)
    redo = entry.operation == REDO_SOURCE
    head = (f"Вернуть отмену № {revision} в проекте «{row.project}»?" if redo
            else f"Отменить сохранение № {revision} в проекте «{row.project}»?")
    lines = [head, "",
             "Будет сделано новое сохранение, которое вернёт файлы к виду до этого. История не стирается."]
    files = plan.data.get("files") or []
    if files:
        lines += ["", "Затронутые файлы:"]
        for item in files[:DIALOG_FILES]:
            action = ACTION_TEXTS.get(item.get("action", ""), item.get("action", ""))
            lines.append(f"  {item.get('path', '')}" + (f" ({action})" if action else ""))
        rest = int(plan.data.get("count") or len(files)) - min(len(files), DIALOG_FILES)
        if rest > 0:
            lines.append(f"  … и ещё {rest}")
    return "\n".join(lines)


def refusal_text(result: Result) -> str:
    """Почему отменить не вышло: человеческими словами, без кодов. Ничего не тронуто."""
    error = result.error
    if error is None:
        return result.text or FAILED_TEXT.capitalize() + "."
    if error.kind == "newer_changes":
        authors = [a for a in (result.data.get("authors") or []) if isinstance(a, str) and a]
        if len(authors) > 1:
            who = f"файлы меняли: {', '.join(authors)}"
        else:
            who = f"файл менял {authors[0]}" if authors else "файл менял кто-то ещё"
        return (f"После этого сохранения {who}. Отменить автоматически нельзя, чтобы не потерять чужую работу. "
                "Верните прежний вид нужных файлов вручную.")
    if error.kind == "policy_denied" and (error.svn or {}).get("reason") == "rollback_not_mine":
        return "Это сохранение сделано не вами: чужие сохранения отсюда не отменяются."
    return result.text or error.message


def done_text(row: Row, result: Result) -> str:
    """Сообщение после удавшейся отмены."""
    number = result.data.get("number")
    if row.entry.operation == REDO_SOURCE:
        return (f"Отмена № {target_revision(row.entry)} возвращена: файлы снова такие, какими были после "
                f"сохранения № {row.entry.undone_rev}. Новое сохранение № {number}.")
    return result.text or f"Сохранение № {target_revision(row.entry)} отменено. Новое сохранение № {number}."
