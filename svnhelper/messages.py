"""Тексты для пользователя: «технический код → русское сообщение», форматирование размеров."""
from __future__ import annotations

import platform
import re
from datetime import datetime
from typing import Optional

from . import __version__
from .svn_core import BranchHistory, ErrorKind, Estimate, LockInfo, WcStatus, extract_codes

_SHORT = {
    ErrorKind.AUTH: "Неверный логин или пароль.",
    ErrorKind.AUTHZ: "Нет доступа к этой папке, обратитесь к администратору.",
    ErrorKind.NO_CONNECTION: "Нет связи с сервером, проверьте сеть или VPN.",
    ErrorKind.NOT_FOUND: "Папка не найдена на сервере. Нажмите «⟳».",
    ErrorKind.TIMEOUT: "Сервер не отвечает. Попробуйте позже.",
    ErrorKind.CANCELLED: "Операция отменена.",
    ErrorKind.LOCKED: "Файл уже занят другим пользователем.",
    ErrorKind.OUT_OF_DATE: "На сервере есть более новая версия файла. "
                           "Сначала нажмите «Получить новое», потом занимайте файл.",
    ErrorKind.EXISTS: "Такая ветка или версия уже есть на сервере.",
    ErrorKind.UNRELATED: "Эта ветка не из этого проекта, переключиться на неё нельзя.",
    ErrorKind.BUSY: "Файл открыт в другой программе. Закройте Компас, Word и Excel с файлами этого проекта и повторите.",
    ErrorKind.LOCK_REQUIRED: "Этот файл нужно сначала занять, потом сохранять.",
    ErrorKind.READ_ONLY_LINE: "Версию менять нельзя. Создайте ветку и правьте в ней.",
    ErrorKind.LOCAL_HISTORY_MISSING: "История этого проекта на компьютере потеряна.",
    ErrorKind.OTHER: "Не удалось выполнить операцию.",
}


def short_message(kind: ErrorKind) -> str:
    return _SHORT[kind]


def create_undo_text(rolled_back, rollback_error: str = "") -> str:
    """Что стало с местом на сервере после сбоя или остановки создания проекта (importer.CreateError.rolled_back)."""
    if rolled_back is True:
        return "Всё на сервере возвращено в прежнее состояние."
    if rolled_back is False:
        return ("Проект на сервере мог остаться недоделанным"
                + (f" ({rollback_error})" if rollback_error else "") + ". Сообщите администратору.")
    return ""


# маскирует «--password X», «--password=X» и «password=...» / «password: ...» (в том числе в кавычках)
_PASSWORD_OPTION_RE = re.compile(r'(--password)(\s+|=)("[^"]*"|\S+)')
_PASSWORD_FIELD_RE = re.compile(r'(?i)(password\s*[=:]\s*)("[^"]*"|\S+)')


def redact(text: str) -> str:
    """Убирает пароли из текста, который пользователь отправит администратору."""
    text = _PASSWORD_OPTION_RE.sub(r"\1\2***", text)
    return _PASSWORD_FIELD_RE.sub(r"\1***", text)


def admin_report(action: str, kind: ErrorKind, raw: str, log_path: str,
                 extra: Optional[dict] = None) -> str:
    """Текст «для администратора»: что делали, что случилось и подробности svn (без паролей)."""
    lines = [
        f"SVN-lite {__version__}",
        f"Windows: {platform.platform()}",
        f"Время: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Действие: {action}",
        f"Вид ошибки: {kind.value}",
    ]
    codes = sorted(extract_codes(raw))
    if codes:
        lines.append("Коды svn: " + ", ".join(codes))
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {redact(str(value))}")
    tail = redact(raw or "").strip().splitlines()[-30:]
    if tail:
        lines += ["", "Ответ svn (последние строки):", *tail]
    lines += ["", f"Журнал: {log_path}"]
    return "\n".join(lines)


def format_age(days: int) -> str:
    """0 → «сегодня», 1 → «вчера», 3 → «3 дня назад», 14 → «2 недели назад»."""
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} {plural(days, ('день', 'дня', 'дней'))} назад"
    if days < 30:
        weeks = days // 7
        if weeks == 1:
            return "неделю назад"
        return f"{weeks} {plural(weeks, ('неделя', 'недели', 'недель'))} назад"
    if days < 365:
        months = days // 30
        if months == 1:
            return "месяц назад"
        return f"{months} {plural(months, ('месяц', 'месяца', 'месяцев'))} назад"
    years = days // 365
    if years == 1:
        return "год назад"
    return f"{years} {plural(years, ('год', 'года', 'лет'))} назад"


OFFLINE_BAR = ("Нет связи с сервером (проверьте VPN). С файлами работать можно, "
               "сохранить получится, когда связь появится.")


PLAIN_TEXT = "Только на этом компьютере"
PLAIN_TODO = "Нажмите «Отправить на сервер…»: тогда проект увидят коллеги"
PLAIN_OFFLINE_TODO = "Отправить на сервер получится, когда появится связь"


def status_line(state: str, status: Optional[WcStatus], checked: bool) -> tuple[str, str, str]:
    """Состояние проекта в «Мои проекты»: (текст, тег цвета ok|new|bad|plain, подсказка «что сделать»).

    state: "svn" | "plain" | "broken". checked=False и status=None → проверка ещё не запускалась;
    checked=True и status=None → проверка идёт прямо сейчас.
    "plain" - обычная папка: ни истории, ни сервера; её можно отправить на сервер.
    """
    if state == "plain":
        return PLAIN_TEXT, "plain", PLAIN_TODO
    if state == "broken":
        return ("Папка повреждена", "bad", "Нажмите «Починить папку»")
    if status is None:
        return ("Проект с сервера", "ok", "Идёт проверка…" if checked else "Нажмите «⟳»")
    if status.conflicts:
        text, tag, hint = ("Конфликт: ваши и чужие правки в одном файле", "bad",
                           "Нажмите «Разобрать конфликт»")
    elif status.incoming and status.modified:
        text, tag, hint = ("Есть новое на сервере и ваши правки", "new",
                           "Сначала «Получить новое», потом «Сохранить на сервер»")
    elif status.incoming:
        text, tag, hint = "Есть новое на сервере", "new", "Нажмите «Получить новое»"
    elif status.modified:
        text, tag, hint = ("Есть несохранённые правки", "new",
                           "Нажмите «Сохранить на сервер»")
    else:
        text, tag, hint = "Актуален", "ok", "Ничего делать не нужно"
    if status.my_locks:
        hint += f". Не забудьте освободить занятые файлы ({len(status.my_locks)})"
    return text, tag, hint


HISTORY_NAMES_MAX = 4        # сколько имён авторов показываем, остальные «и ещё N»
HISTORY_LIMIT = 200          # сколько последних сохранений читаем у ветки или версии
COMMENT_MAX = 60


def line_label(line) -> str:
    """«Основная», «Ветка «x»», «Версия «x»» (line — server.Line)."""
    if line.kind == "branch":
        return f"Ветка «{line.name}»"
    if line.kind == "tag":
        return f"Версия «{line.name}»"
    if line.kind == "folder":
        return "Папка"
    return "Основная"


def line_title(line) -> str:
    """Для подробностей: версия помечена замком и словами «только для просмотра»."""
    return f"🔒 {line_label(line)} — только для просмотра" if line.kind == "tag" else line_label(line)


def files_count(count: int) -> str:
    return f"{count} {plural(count, ('файл', 'файла', 'файлов'))}"


def included_text(revision: Optional[int], modified: int) -> str:
    """Что войдёт в новую ветку или версию: только сохранённое на сервере."""
    text = "Войдёт то, что сохранено на сервере" + (f" (сохранение № {revision})" if revision else "") + "."
    if modified:
        text += f" Несохранённые правки ({files_count(modified)}) не войдут."
    return text


def _short_date(value: Optional[datetime], with_year: bool = False) -> str:
    if value is None:
        return ""
    return f"{value.astimezone():%d.%m.%Y}" if with_year else f"{value.astimezone():%d.%m}"


def _origin_text(history: BranchHistory) -> str:
    """«из Основной (№ 3)» / «из ветки «x»» / «из версии «y»»; пусто, если неизвестно."""
    parts = [p for p in history.copied_from.split("/") if p]
    rev = f" (№ {history.copied_from_revision})" if history.copied_from_revision else ""
    for i, part in enumerate(parts):
        if part == "trunk":
            return f"из Основной{rev}"
        if part in ("branches", "tags") and i + 1 < len(parts):
            return (f"из ветки «{parts[i + 1]}»" if part == "branches" else f"из версии «{parts[i + 1]}»") + rev
    return ""


def _author_name(name: str) -> str:
    return "неизвестный" if name == "?" else name


def last_change(history: BranchHistory) -> tuple[str, Optional[datetime]]:
    """Кто и когда менял линию последним: (имя, дата); имя пустое, если неизвестно."""
    newest = max((a for a in history.authors if a.last_date), key=lambda a: a.last_date, default=None)
    if newest is not None:
        return _author_name(newest.name), newest.last_date
    return "", history.last_date


def created_cells(history: BranchHistory) -> tuple[str, str]:
    """Для таблицы линий: («petrov», «19.10.2026»); «—», если неизвестно."""
    who = _author_name(history.created_by) if history.created_by else "—"
    return who, _short_date(history.created, with_year=True) or "—"


def description_cell(history) -> str:
    """Для таблиц: комментарий при создании ветки (версии) одной строкой; «…» — ещё грузится, «—» — комментария нет."""
    if history is None:
        return "…"
    comment = " ".join(history.comment.split()) if history else ""
    return comment or "—"


def last_change_cell(history: BranchHistory) -> str:
    """Для таблицы линий: «ivanov, 19.10»; «—», если неизвестно."""
    who, when = last_change(history)
    return ", ".join(p for p in (who, _short_date(when)) if p) or "—"


def branch_summary(history: BranchHistory) -> str:
    """Одна строка про ветку: «Создал petrov 19.10 · последнее изменение ivanov 19.10»."""
    def who_when(who: str, when: Optional[datetime]) -> str:
        return " ".join(p for p in (who, _short_date(when)) if p)

    created = who_when(_author_name(history.created_by) if history.created_by else "", history.created)
    last = who_when(*last_change(history))
    parts = []
    if created:
        parts.append(f"Создал {created}")
    if last and last != created:      # если после создания никто ничего не менял, повторять незачем
        parts.append(f"последнее изменение {last}")
    return " · ".join(parts)


_TABLE_SHORT = {
    "Конфликт: ваши и чужие правки в одном файле": "Конфликт",
    "Есть новое на сервере и ваши правки": "Есть новое и ваши правки",
    "Есть несохранённые правки": "Несохранённые правки",
    "Сначала «Получить новое», потом «Сохранить на сервер»": "«Получить новое», затем «Сохранить»",
    "Скачайте проект на вкладке «Скачать проект»": "Скачайте на вкладке «Скачать проект»",
}


def table_text(text: str) -> str:
    """Текст для узкой колонки таблицы: длинные формулировки заменены короткими, полный текст виден в подробностях."""
    text = text.replace(". Не забудьте освободить занятые файлы", ". Освободите файлы")
    return _TABLE_SHORT.get(text, text)


def history_lines(history: BranchHistory, locks: Optional[list[LockInfo]],
                  show_created: bool = True) -> list[str]:
    """Строки «Создал… / Работали… / Заняты файлы…» по сведениям о ветке или версии.

    locks=None или пусто — строка про занятые файлы не выводится.
    show_created=False — для Основной: кто создал, не интересно.
    """
    lines: list[str] = []
    if show_created and (history.created_by or history.created):
        head = f"Создал {_author_name(history.created_by or '?')}"
        if history.created:
            head += " " + _short_date(history.created, with_year=True)
        origin = _origin_text(history)
        if origin:
            head += " " + origin
        comment = " ".join(history.comment.split())
        if comment:
            if len(comment) > COMMENT_MAX:
                comment = comment[:COMMENT_MAX].rstrip() + "…"
            head += f": «{comment}»"
        lines.append(head)
    if history.authors:
        by_work = sorted(history.authors, key=lambda a: -a.commits)   # при равенстве порядок «новые первыми» сохраняется
        names = ", ".join(f"{_author_name(a.name)} — {a.commits}" for a in by_work[:HISTORY_NAMES_MAX])
        rest = len(by_work) - HISTORY_NAMES_MAX
        if rest > 0:
            names += f" и ещё {rest}"
        head = "Работали" if history.complete else f"Работали (последние {HISTORY_LIMIT} сохранений)"
        text = f"{head}: {names}"
        newest = max((a for a in history.authors if a.last_date), key=lambda a: a.last_date, default=None)
        if newest is not None:
            text += f". Последнее: {_author_name(newest.name)}, {_short_date(newest.last_date)}"
        lines.append(text)
    if locks:
        owners: dict[str, int] = {}
        for lock in locks:
            owners[lock.owner] = owners.get(lock.owner, 0) + 1
        ordered = sorted(owners.items(), key=lambda pair: -pair[1])
        lines.append("Заняты файлы: " + ", ".join(f"{who} — {count}" for who, count in ordered))
    return lines


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """plural(5, ('файл', 'файла', 'файлов')) → 'файлов'."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return forms[1]
    return forms[2]


def format_number(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def format_size(size: float) -> str:
    mb = size / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f}".replace(".", ",") + " ГБ"
    return f"{mb:.1f}".replace(".", ",") + " МБ"


def estimate_text(est: Estimate) -> str:
    files = f"{format_number(est.files)} {plural(est.files, ('файл', 'файла', 'файлов'))}"
    dirs = f"{format_number(est.dirs)} {plural(est.dirs, ('папка', 'папки', 'папок'))}"
    return (f"В проекте: {files}, {dirs}, {format_size(est.size)}. "
            f"На диске займёт около {format_size(est.size * 2)}.")


LOCK_REMINDER = (
    "Файлы Компас, Word, Excel и архивы PLC после скачивания доступны только для чтения. "
    "Так и должно быть. Чтобы изменить такой файл, займите его: вкладка «Занятые» → "
    "«Занять файл…»."
)

# окно «Освободить файлы»
UNLOCK_UNSAVED = "есть несохранённые правки"
UNLOCK_PROMPT = ("Эти файлы заняты вами. Выберите те, которые больше не правите. Файлы с несохранёнными правками "
                 "заранее не отмечены: сначала сохраните правки (вкладка «Изменения»), иначе коллега займёт файл, "
                 "и ваши правки разойдутся с его.")

TAG_EXPLAIN = "Версия только для просмотра: правки в неё сохранить нельзя."
TAG_TODO = "Для правок переключитесь на Основную или ветку."
TAG_VIEW_ONLY = "Только для просмотра: правки в неё сохранить нельзя."
TAG_MODIFIED = ("Правки в версию сохранить нельзя. Создайте из неё ветку "
                "(вкладка «Ветки» → «Новая ветка…») и перейдите на неё.")
TAG_MODIFIED_SHORT = "В версию нельзя: создайте ветку"

# сохранение на сервер (кнопка «Сохранить на сервер…»)
COMMIT_TITLE = "Сохранить на сервер"
COMMIT_TAG_REFUSED = "Версию менять нельзя, создайте ветку."
COMMIT_BUSY = "Проект сейчас занят другой операцией. Дождитесь окончания."
COMMIT_LOADING = "Ищу ваши правки…"
COMMIT_SAVING = "Сохраняю на сервер…"
COMMIT_NOTHING = "Сохранять было нечего: файлы не изменились."
COMMIT_OUT_OF_DATE = "На сервере есть новее. Получить новое и затем сохранить?"
COMMIT_STILL_OUT_OF_DATE = ("Пока шло сохранение, на сервере появилось ещё более новое. "
                            "Нажмите «Получить новое» и сохраните ещё раз.")
COMMIT_UPDATING = "Получаю новое с сервера, затем сохраню…"
COMMIT_CONFLICT = ("После получения нового в файлах появился конфликт: вы и другой человек изменили одно и то же. "
                   "Ваши правки не сохранены. Сначала разберите конфликт, потом сохраните.")
COMMIT_CONFLICT_ASK = COMMIT_CONFLICT + "\n\nРазобрать конфликт сейчас?"
COMMIT_LOCK_REQUIRED = "Файл {names} нужно сначала занять.\n\nЗанять и сохранить?"
COMMIT_LOCK_REQUIRED_MANY = "Файлы нужно сначала занять: {names}.\n\nЗанять и сохранить?"
COMMIT_KEEP_LOCKS = ("Вы всё ещё занимаете файлы, которые не меняли: {names}.\n\n"
                     "Освободить их, чтобы другие могли работать?")
COMMIT_UNSAVED_ON_CLOSE = ("Есть несохранённые правки в: {names}.\n\n"
                           "Правки останутся в папке, но не попадут на сервер. Закрыть программу?")
COMMIT_RUNNING_ON_CLOSE = "Идёт сохранение на сервер. Подождите, пока оно закончится."

# конфликт: выбор в самой программе
CONFLICT_TITLE = "Конфликт"
CONFLICT_FILE = "Файл «{name}» изменили и вы, и другой человек. Какую версию оставить?"
CONFLICT_COUNT = "Файл {index} из {total}."
CONFLICT_MINE_HINT = "Оставить мою версию: в файле останутся ваши правки, правки другого человека в нём пропадут."
CONFLICT_THEIRS_HINT = ("Взять версию с сервера: ваши правки в этом файле пропадут, "
                        "останется то, что сохранил другой человек.")
CONFLICT_NONE = "Файлов с конфликтом не нашлось. Состояние проекта сейчас обновится."
CONFLICT_RESOLVING = "Решаю конфликт…"
CONFLICT_DONE = "Конфликт решён. Теперь правки можно сохранить на сервер."
CONFLICT_FAILED_TITLE = "Не удалось решить конфликт"
CONFLICT_UNSUPPORTED = "Этот конфликт (например, файл удалили на сервере, а вы его правили) нельзя решить выбором версии. "
CONFLICT_UNSUPPORTED_TORTOISE = CONFLICT_UNSUPPORTED + (
    "Откройте «Ещё» → «Для опытных» → «Разобрать конфликт в TortoiseSVN».")
CONFLICT_UNSUPPORTED_ADMIN = CONFLICT_UNSUPPORTED + "Обратитесь к администратору."

# получение нового с сервера
UPDATE_DONE_TITLE = "Новое получено"
UPDATE_CANCELLED_TITLE = "Получение нового остановлено"
UPDATE_BUSY = "Проект сейчас открыт в TortoiseSVN или чинится. Дождитесь окончания."

# работа в фоне (трей)
TRAY_HINT_TITLE = "SVN-lite"
TRAY_HINT = ("Программа продолжает работать у часов (в области уведомлений). "
             "Открыть её можно по значку, закрыть совсем - пункт «Выйти».")
TRAY_UNAVAILABLE = "Не удалось показать значок в области уведомлений. Программа работает в обычном окне."
AUTOSTART_FAILED = "Не удалось изменить запуск вместе с Windows. Подробности записаны в журнал."

# уведомления (окошко в правом нижнем углу)
NOTICE_CHECK_TITLE = "Проверка"
NOTICE_OFFLINE = "Нет связи с сервером. Проверим снова позже."
NOTICE_AUTH = "Для проверки нужен вход на сервер. Откройте программу и войдите."
NOTICE_FAILED = "Не удалось проверить. Подробности записаны в журнал."
NOTICE_SNOOZED = "Уведомления отключены на {hours} {unit}."
NOTICE_SNOOZE_MENU_ON = "Не беспокоить: включено до {time} - выключить"
NOTICE_SNOOZE_OFF = "Уведомления снова включены."


# колонка «Состояние проекта»: шапка и вкладки
PANEL_NO_PROJECT = "Выберите проект в списке."
PANEL_NOT_SVN = "Эта папка не подключена к серверу."
PANEL_BROKEN = "Папка повреждена: сначала почините её (кнопка слева)."
PANEL_OFFLINE = "Нет связи с сервером. Когда связь появится, нажмите «⟳»."
PANEL_LOAD_FAILED = "Не удалось получить сведения: {reason}"
PANEL_AUTH = "Нужен вход на сервер. Нажмите «⟳», чтобы повторить."
INCOMING_NOTE = "Ваши файлы не пострадают: придёт только то, что вы не меняли."
INCOMING_LOADING = "Ищу, что нового на сервере…"
INCOMING_EMPTY = "На сервере нового нет."
HISTORY_LOADING = "Загружаю историю…"
HISTORY_EMPTY = "Сохранений пока нет."
LOCKS_NONE = "Занятых файлов нет."
LOCKS_UNCHECKED = "Занятые файлы покажутся после проверки проекта."
LOCKS_OWNER_ME = "вы"
LINES_PICK_PROJECT = "Выберите проект с сервера, чтобы увидеть его ветки и версии."
LINES_NO_INFO = "Для этой папки ветки и версии определить не удалось."
LINES_LOADING = "Загружаю список веток и версий…"
SWITCH_UNSAVED = "Сначала сохраните правки — их {count}. Потом переключайтесь."
SWITCH_UNSAVED_ASK = "Перейти к сохранению сейчас?"


def commit_done(count: int) -> str:
    return f"Сохранено на сервер: {count} {plural(count, ('файл', 'файла', 'файлов'))}"

SWITCH_UNFINISHED = "Переключение не закончено"
SWITCH_UNFINISHED_TODO = "Вкладка «Ветки» → «Перейти»"
MERGE_NOTE = ("В окне TortoiseSVN выберите, из какой ветки взять изменения. "
              "После слияния проверьте результат и сохраните правки на сервер.")

SVN_NOT_FOUND = (
    "Не найдена программа svn.exe, без неё скачивание невозможно.\n\n"
    "Установите TortoiseSVN и при установке включите пункт "
    "«command line client tools». После установки запустите программу снова."
)

SVN_NOT_FOUND_AFTER_INSTALL = (
    "TortoiseSVN установлен, но svn.exe не найден: при установке не включён пункт "
    "«command line client tools». Запустите установщик TortoiseSVN ещё раз (Modify) и включите его."
)

INSTALL_OFFER = (
    "На этом компьютере не найдены {what} (нужен TortoiseSVN с «command line client tools»).\n\n"
    "Установить TortoiseSVN сейчас? Файл установки лежит на сервере. "
    "Windows спросит разрешение администратора."
)


# вкладка «Ветки»: отличия выбранной ветки от Основной
DIFF_KIND_TEXT = {"added": "новый", "deleted": "удалён", "modified": "изменён"}   # вид изменения у svn → по-русски
DIFF_LOADING = "Сравниваю с Основной…"
DIFF_EMPTY = "Отличий от Основной нет."
DIFF_OFFLINE = "Нет связи с сервером. Когда связь появится, нажмите «Отличия» ещё раз."
DIFF_AUTH = "Нужен вход на сервер. Нажмите «Отличия», чтобы повторить."
