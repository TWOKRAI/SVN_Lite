"""Панель подсказок внизу главного окна (docs/PLAN_ui_v4.md, раздел 5).

Слева - текст: пояснение элемента под курсором или в фокусе; иначе совет о тихом сбое (15 с); иначе результат
действия (10 с); иначе фоновый текст (что сделать с выбранным проектом). Справа - ход долгой операции и «Отмена».

Подключение: окно создаёт одну панель (HintBar) и вызывает install(bar). Дальше любой виджет получает пояснение
через hint(widget, ключ), строки таблиц - через hint_rows, ярлыки вкладок - через hint_tabs. Без установленной
панели (тесты отдельных виджетов, диалоги) эти функции ничего не делают. События вешаются с add="+", поэтому
уживаются с всплывающими подсказками (Tooltip) и собственными обработчиками виджета.

Тексты - в HINTS: ключ -> (коротко, подробно). У серой кнопки к тексту добавляется причина («Сейчас недоступно:
…»): своя у виджета (hint(..., reason=...)), иначе от окна (install(bar, reason=...)), иначе из REASONS.
Модуль не знает ни о svn, ни об окне: только tkinter (и список режимов из config).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Sequence, Union

from .. import messages, next_step as ns
from ..config import HINT_MODES

# режимы панели (настройка settings.hint_mode, меню «Вид → Подсказки внизу»)
MODES = HINT_MODES
MODE_DETAILED, MODE_SHORT, MODE_PROGRESS = MODES
DEFAULT_MODE = MODE_DETAILED
MODE_LABELS = {MODE_DETAILED: "Подробно (2 строки)", MODE_SHORT: "Коротко (1 строка)",
               MODE_PROGRESS: "Только ход работы"}
MENU_TITLE = "Подсказки внизу"
SMALL_WINDOW = 700        # окно ниже этого (px до масштаба экрана): всегда одна строка

HOVER_DELAY_MS = 150      # уход с элемента возвращает прежний текст не сразу: между соседними кнопками не мигает
ERROR_MS = 15_000         # сколько виден совет о тихом сбое
RESULT_MS = 10_000        # сколько виден результат действия

UNAVAILABLE = "Сейчас недоступно: {reason}."
GENERIC_REASON = "для выбранного проекта это сейчас не подходит"

# ключ -> (коротко, подробно). Подробный текст говорит, что делает элемент и когда он нужен.
HINTS: dict[str, tuple[str, str]] = {
    # колонка слева: проект целиком
    "project.check": ("Проверить состояние",
                      "Проверить состояние: заново узнать у сервера, что изменилось в проектах, и перечитать "
                      "открытую вкладку."),
    "project.resolve": ("Разобрать конфликт",
                        "Разобрать конфликт: вы и коллега изменили одно и то же. Для каждого такого файла "
                        "выберите, чью версию оставить."),
    "project.repair": ("Починить папку",
                       "Починить папку: после сбоя папка проекта осталась в незаконченном состоянии. Починка "
                       "приведёт её в порядок, ваши файлы не пострадают."),
    "project.restore": ("Восстановить историю",
                        "Восстановить историю: история проекта переехала вместе с папкой. Программа снова свяжет "
                        "с ней папку, файлы не тронутся."),
    "project.get_new": ("Получить новое",
                        "Получить новое: принесёт с сервера то, что сохранили коллеги. Ваши правки не пострадают."),
    "project.publish": ("Отправить на сервер…",
                        "Отправить на сервер: проект пока есть только на этом компьютере. Откроется окно, где "
                        "выбрать место на сервере."),
    "project.continue": ("Продолжить без истории",
                         "Продолжить без истории: файлы останутся, а прежние сохранения этого проекта станут "
                         "недоступны. Программа спросит подтверждение."),
    "project.open": ("Открыть папку", "Открыть папку: папка проекта на этом компьютере в Проводнике."),
    "project.server": ("Открыть на сервере",
                       "Открыть на сервере: папка проекта на сетевом диске в Проводнике."),
    "project.more": ("Ещё: редкие действия",
                     "Ещё: редкие действия - окна TortoiseSVN, отправка на сервер, удаление прежних сохранений, "
                     "пункты для опытных."),
    "project.new": ("Новый проект", "Новый проект: создать проект из шаблона или из папки на диске."),
    # колонка «Проекты»
    "tab.mine": ("Мои проекты", "Мои проекты: проекты, скачанные в выбранную папку на этом компьютере."),
    "tab.download": ("Скачать проект", "Скачать проект: найти проект на сервере и скачать его на компьютер."),
    "mine.folder": ("Папка с проектами",
                    "Папка с проектами: из какой папки на компьютере показывать проекты. В списке - папки, "
                    "которые вы уже выбирали."),
    "mine.browse": ("Выбрать папку", "Выбрать…: другая папка с проектами на этом компьютере."),
    "mine.rescan": ("Найти проекты", "Найти проекты: заново просмотреть папку и найти в ней проекты."),
    "mine.filter": ("Найти проект", "Найти проект: показывать только проекты, в названии которых есть этот текст."),
    "mine.table": ("Ваши проекты",
                   "Ваши проекты. Выберите проект, чтобы увидеть его состояние; двойной щелчок откроет вкладку "
                   "того, что нужно сделать."),
    "mine.empty_download": ("Скачать проект с сервера",
                            "Скачать проект с сервера: откроется «Скачать проект», где найти проект и скачать его."),
    "mine.empty_browse": ("Выбрать папку", "Выбрать…: папка не найдена, укажите, где лежат ваши проекты."),
    # «Скачать проект»: дерево сервера и форма
    "download.search": ("Найти на сервере", "Найти на сервере: поиск проекта по части названия."),
    "download.tree": ("Проекты на сервере",
                      "Проекты на сервере. Откройте папку и выберите проект (отмечен ✔), чтобы скачать его."),
    "download.refresh": ("Перечитать список с сервера",
                         "Перечитать список с сервера: показать проекты, которые появились недавно."),
    "download.url": ("Ссылка на проект", "Ссылка на проект: вставьте ссылку или путь, если проекта нет в списке."),
    "download.recent": ("Недавние", "Недавние: проекты, которые вы уже скачивали, и избранные."),
    "download.favorite": ("Избранное", "Избранное: запомнить этот проект, чтобы быстро найти его в «Недавних»."),
    "download.browse_disk": ("Выбрать папку на диске",
                             "Выбрать папку на диске: найти проект по папке на сетевом диске."),
    "download.line": ("Что скачать", "Что скачать: основную линию проекта, ветку или зафиксированную версию."),
    "download.folder": ("Куда скачать", "Куда скачать: папка для проектов на этом компьютере."),
    "download.browse": ("Выбрать папку", "Выбрать…: другая папка на этом компьютере, куда скачать проект."),
    "download.start": ("Скачать",
                       "Скачать: проект скачается в выбранную папку. Ход скачивания и «Отмена» - внизу окна."),
    "download.create": ("Создать проект из этой папки",
                        "Создать проект из этой папки: такого проекта на сервере ещё нет, программа создаст его "
                        "из папки на диске."),
    "download.cleanup": ("Удалить недокачанную папку",
                         "Удалить недокачанную папку: скачивание прервалось, в папке осталась только часть файлов."),
    # колонка «Состояние проекта»: ярлыки
    "tab.changes": ("Изменения", "Изменения: ваши несохранённые правки и сохранение на сервер."),
    "tab.incoming": ("Новое",
                     "Новое: что коллеги сохранили на сервере и чего ещё нет у вас. Получить: «Получить новое» слева."),
    "tab.history": ("История", "История: кто, когда и что сохранял в этом проекте."),
    "tab.locks": ("Занятые",
                  "Занятые: какие файлы сейчас заняты, кем и как давно. Здесь же занять и освободить файл."),
    "tab.lines": ("Ветки", "Ветки: ветки и зафиксированные версии проекта, переход между ними."),
    # «Изменения»
    "changes.table": ("Ваши правки",
                      "Ваши несохранённые правки. Щелчок по ☑ снимает или ставит отметку: сохранятся только "
                      "отмеченные файлы."),
    "changes.comment": ("Что вы сделали?",
                        "Что вы сделали? Коротко опишите правку: коллеги увидят этот текст в истории проекта."),
    "changes.save": ("Сохранить отмеченное",
                     "Сохранить отмеченное: отмеченные файлы уйдут на сервер вместе с текстом из поля «Что вы "
                     "сделали?». Коллеги получат их через «Получить новое»."),
    "changes.save_local": ("Сохранить у себя",
                           "Сохранить у себя: все правки сохранятся в истории проекта на этом компьютере. Текст "
                           "берётся из поля «Что вы сделали?», если вы его написали."),
    "changes.template": ("Готовый текст",
                         "Готовый текст: подставит его в поле «Что вы сделали?», дальше его можно дописать."),
    "changes.recent": ("Последние", "Последние: выбрать один из ваших недавних текстов для поля «Что вы сделали?»."),
    # «Новое», «История»
    "incoming.table": ("Новое на сервере",
                       "Что коллеги сохранили на сервере после вашего последнего «Получить новое»."),
    "history.table": ("История проекта", "Последние сохранения проекта: кто, когда и что сделал."),
    # «Занятые»
    "locks.table": ("Занятые файлы",
                    "Занятые файлы: ваши (их можно освободить) и чужие. Выберите свои строки и нажмите «Освободить»."),
    "locks.take": ("Занять файл…",
                   "Занять файл: пока файл занят вами, коллеги не смогут его сохранить. Займите чертёж перед "
                   "правкой, чтобы не появилось двух версий."),
    "locks.release": ("Освободить",
                      "Освободить: выбранные свои файлы снова смогут править коллеги. Ничего не выбрано - "
                      "откроется список ваших занятых файлов."),
    # «Ветки»
    "lines.table": ("Ветки и версии",
                    "Ветки и версии проекта. «●» отмечает ту, что сейчас в вашей папке."),
    "lines.switch": ("Перейти",
                     "Перейти: папка проекта начнёт показывать выбранную ветку или версию. Несохранённые правки "
                     "сначала нужно сохранить."),
    "lines.compare": ("Отличия", "Отличия: какие файлы в выбранной ветке отличаются от основной."),
    "lines.merge": ("Перенести правки…",
                    "Перенести правки: взять изменения из другой ветки в вашу папку (откроется окно TortoiseSVN)."),
    "lines.new_branch": ("Новая ветка…",
                         "Новая ветка: отдельная линия проекта для работы, которая не должна мешать основной."),
    "lines.download_other": ("Скачать отдельно",
                             "Скачать отдельно: выбранная ветка или версия скачается в свою папку, рядом с этой."),
    "lines.new_tag": ("Зафиксировать версию…",
                      "Зафиксировать версию: запомнить проект таким, как сейчас, под именем (например, «выдано "
                      "заказчику»). Такую версию потом можно только смотреть."),
    # переключатель вида колонки кнопок (стрелка вверху колонки, меню «Вид → Кнопки»)
    "buttons.compact": ("Свернуть до значков",
                        "Свернуть до значков: кнопки колонки станут маленькими, только со значками. Что делает "
                        "кнопка, видно при наведении."),
    "buttons.expand": ("Показать подписи", "Показать подписи: кнопки колонки снова со значками и подписями."),
    # сама панель
    "bar.cancel":("Отмена", "Отмена: остановить операцию. То, что уже пришло, останется в папке."),
}

# причина, почему кнопка серая, если окно не назвало общей (идёт операция, не выбран проект)
REASONS: dict[str, str] = {
    "project.open": "не выбран проект",
    "project.more": "не выбран проект",
    "project.server": "папка проекта на сетевом диске не указана в настройках",
    "project.publish": "отправить можно только проект, который есть лишь на этом компьютере",
    "download.start": "выберите проект в списке слева или вставьте ссылку на него",
    "download.favorite": "сначала выберите проект",
    "download.line": "сначала выберите проект",
    "locks.release": "выбраны только чужие файлы: их освобождает тот, кто занял",
    "lines.switch": "выберите в таблице другую ветку или версию",
    "lines.compare": "выберите в таблице ветку (у основной отличий от себя нет)",
    "lines.merge": "нужна программа TortoiseSVN, а в зафиксированную версию правки не переносят",
    "lines.download_other": "выберите в таблице ветку или версию",
    "lines.new_tag": "это уже зафиксированная версия",
}

# «Что делать дальше» (docs/PLAN_ui_v4.md, раздел 6.6): ключ NextStep.text_key -> messages.StepText (состояние, тег,
# коротко, подробно). Словарь один на карточку, таблицу и панель; лежит в messages, потому что строку «Что сделать»
# карточки и таблицы собирает messages.state_line, а messages не может зависеть от окна (его берут ops и CLI).
NEXT_STEP_TEXTS = messages.NEXT_STEP_TEXTS
_SILENT_WHEN_SHORT = (ns.NOTHING, ns.READ_ONLY)     # «всё в порядке»: в режиме «коротко» панель пуста


def step_texts(step: "ns.NextStep", **fields: str) -> tuple[str, str]:
    """(подробно, коротко) для панели по шагу next_step. Молчащий шаг (нет связи, идёт операция или проверка) - пусто:
    об этом говорят полоса «Нет связи» и ход работы справа."""
    if step.quiet:
        return "", ""
    detailed = messages.step_text(step, "detailed", **fields)
    short = "" if step.code in _SILENT_WHEN_SHORT else messages.step_text(step, "short", **fields)
    return tuple(_sentence(text) if text.strip() else "" for text in (detailed, short))    # type: ignore[return-value]


Reason = Union[str, Callable[[], str], None]
_bar: Optional["HintBar"] = None


def install(bar: Optional["HintBar"]) -> None:
    """Делает bar панелью окна: с этого момента hint() и остальные подключают к ней виджеты (None - отключить)."""
    global _bar
    _bar = bar


def installed(widget: Optional[tk.Misc] = None) -> Optional["HintBar"]:
    """Установленная живая панель (того же окна, что widget) или None."""
    bar = _bar
    if bar is None:
        return None
    try:
        if not bar.winfo_exists():
            return None
        if widget is not None and str(bar.winfo_toplevel()) != str(widget.winfo_toplevel()):
            return None      # диалог или другое окно: у них своей панели нет
    except tk.TclError:
        return None
    return bar


def key_of(widget: tk.Misc) -> Optional[str]:
    """Ключ пояснения виджета (для проверок): None, если hint() его не подключал."""
    return getattr(widget, "_hint_key", None)


def _disabled(widget: tk.Misc) -> bool:
    try:
        if isinstance(widget, ttk.Widget):
            return widget.instate(["disabled"])
        return str(widget.cget("state")) == "disabled"
    except (tk.TclError, AttributeError):
        return False


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith(("…", ".", "!", "?")) else text + "."


def describe(key: str, mode: str = DEFAULT_MODE, disabled: bool = False, reason: str = "") -> str:
    """Текст пояснения: коротко или подробно; у серого элемента ещё и причина."""
    short, detailed = HINTS[key]
    text = short if mode == MODE_SHORT else detailed
    if disabled:
        text = f"{_sentence(text)} {UNAVAILABLE.format(reason=(reason or GENERIC_REASON).rstrip('.'))}"
    return text


def _reason_for(widget: tk.Misc, key: str, bar: "HintBar") -> str:
    own = getattr(widget, "_hint_reason", None)
    text = own() if callable(own) else (own or "")
    if not text and bar.reason is not None:
        text = bar.reason(key)
    return text or REASONS.get(key, "")


def widget_text(widget: tk.Misc, bar: "HintBar") -> str:
    """Что панель показывает для виджета с ключом (его состояние читается сейчас, а не при наведении)."""
    key = key_of(widget)
    if key is None:
        return ""
    disabled = _disabled(widget)
    return describe(key, bar.mode, disabled, _reason_for(widget, key, bar) if disabled else "")


def hint(widget: tk.Misc, key: str, reason: Reason = None) -> None:
    """Пояснение widget по ключу HINTS. Повторный вызов меняет ключ (кнопка сменила назначение), события второй раз
    не вешаются. reason - почему виджет серый (строка или функция без аргументов); без него причину назовёт окно."""
    if key not in HINTS:
        raise KeyError(f"нет подсказки «{key}»")
    bar = installed(widget)
    if bar is None:
        return
    widget._hint_key = key              # type: ignore[attr-defined]
    widget._hint_reason = reason        # type: ignore[attr-defined]
    if getattr(widget, "_hint_bound", False):
        return
    widget._hint_bound = True           # type: ignore[attr-defined]
    text = lambda: widget_text(widget, bar)      # noqa: E731
    widget.bind("<Enter>", lambda _e: bar.hover(widget, text), add="+")
    widget.bind("<Leave>", lambda _e: bar.unhover(widget), add="+")
    widget.bind("<FocusIn>", lambda _e: bar.focus_in(widget, text), add="+")
    widget.bind("<FocusOut>", lambda _e: bar.focus_out(widget), add="+")
    widget.bind("<Destroy>", lambda _e: bar.forget(widget), add="+")


def hint_rows(tree: ttk.Treeview, fn: Callable[[str], str], key: str) -> None:
    """Таблица: над строкой - fn(строка) (пусто - общий текст таблицы по key), вне строк - общий текст."""
    hint(tree, key)
    bar = installed(tree)
    if bar is None:
        return

    last: dict[str, Optional[str]] = {"row": None}

    def motion(event) -> None:
        row = tree.identify_row(event.y)
        if row == last["row"] and bar.hovered() is tree:
            return                           # та же строка: текст тот же, не перерисовываем на каждое движение
        last["row"] = row
        own = fn(row) if row else ""
        bar.hover(tree, (lambda: own) if own else (lambda: widget_text(tree, bar)))

    tree.bind("<Motion>", motion, add="+")
    tree.bind("<Leave>", lambda _e: last.update(row=None), add="+")


def hint_tabs(notebook: ttk.Notebook, keys: Sequence[str]) -> None:
    """Ярлыки вкладок: keys[i] - ключ ярлыка с номером i (скрытые вкладки номер сохраняют)."""
    for key in keys:
        if key not in HINTS:
            raise KeyError(f"нет подсказки «{key}»")
    bar = installed(notebook)
    if bar is None:
        return
    notebook._hint_tab_keys = list(keys)     # type: ignore[attr-defined]

    def tab_at(x: int, y: int) -> Optional[int]:
        try:
            index = notebook.index(f"@{x},{y}")
        except tk.TclError:
            return None
        return index if isinstance(index, int) and 0 <= index < len(keys) else None

    def motion(event) -> None:
        index = tab_at(event.x, event.y)
        if index is None:
            bar.unhover(notebook)
        else:
            bar.hover(notebook, lambda: describe(keys[index], bar.mode))

    def focus(_event) -> None:
        try:
            index = notebook.index("current")
        except tk.TclError:
            return
        if isinstance(index, int) and 0 <= index < len(keys):
            bar.focus_in(notebook, lambda: describe(keys[index], bar.mode))

    notebook.bind("<Motion>", motion, add="+")
    notebook.bind("<Leave>", lambda _e: bar.unhover(notebook), add="+")
    notebook.bind("<FocusIn>", focus, add="+")
    notebook.bind("<FocusOut>", lambda _e: bar.focus_out(notebook), add="+")
    notebook.bind("<Destroy>", lambda _e: bar.forget(notebook), add="+")


def fit_lines(text: str, width: int, lines: int, measure: Callable[[str], int]) -> str:
    """Текст, разбитый по словам на строки не шире width (пикселей) и не больше lines строк; не поместилось -
    последняя строка кончается «…». width <= 1 (панель ещё не разложена): текст как есть, одной строкой."""
    words = text.split()
    if width <= 1 or not words:
        return " ".join(words)
    result: list[str] = []
    current = ""
    index = 0
    while index < len(words) and len(result) < lines:
        trial = f"{current} {words[index]}" if current else words[index]
        if not current or measure(trial) <= width:
            current = trial
            index += 1
        else:
            result.append(current)
            current = ""
    if current and len(result) < lines:
        result.append(current)
        current = ""
    cut = index < len(words) or bool(current)

    def shorten(line: str, force: bool) -> str:
        if not force and measure(line) <= width:
            return line
        while line and measure(line.rstrip() + "…") > width:
            line = line[:-1]
        return line.rstrip() + "…"

    result = [shorten(line, cut and i == len(result) - 1) for i, line in enumerate(result)]
    return "\n".join(result)


class HintBar(ttk.Frame):
    """Полоса внизу окна: слева текст подсказки, справа ход долгой операции и «Отмена».

    Высота задана самой полосой (grid_propagate выключен): строк текста столько, сколько велит режим, и она не
    прыгает, когда появляется кнопка «Отмена» или меняется длина текста. Цвета - только из стилей ttk (тёмная тема).
    reason(ключ) - почему элемент серый (окно знает, какая операция идёт); пустая строка - не знает.
    """

    def __init__(self, parent: tk.Misc, px: Callable[[int], int], mode: str = DEFAULT_MODE,
                 reason: Optional[Callable[[str], str]] = None):
        super().__init__(parent, style="Card.TFrame", padding=(px(10), px(4)))
        self.px = px
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.reason = reason
        self._hover: Optional[tuple[tk.Misc, Callable[[], str]]] = None
        self._focus: Optional[tuple[tk.Misc, Callable[[], str]]] = None
        self._error = self._result = self._background = ""
        self._background_short: Optional[str] = None
        self._timers: dict[str, Optional[str]] = {"leave": None, "error": None, "result": None}
        self._progress = ""
        self._cancel_cmd: Optional[Callable[[], None]] = None
        self._small = False
        self._dead = False                   # панель уничтожена (окно закрывается): больше ничего не рисуем
        self.grid_propagate(False)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text_label = ttk.Label(self, text="", justify="left", anchor="w")
        self.text_label.grid(row=0, column=0, sticky="w")
        self.progress_label = ttk.Label(self, text="", justify="right", anchor="e")
        self.progress_label.grid(row=0, column=1, sticky="e", padx=(px(12), 0))
        self.cancel_btn = ttk.Button(self, text="Отмена", style="Small.TButton", command=self._on_cancel)
        self.cancel_btn.grid(row=0, column=2, sticky="e", padx=(px(8), 0))
        self.progress_label.grid_remove()
        self.cancel_btn.grid_remove()
        self.bind("<Configure>", lambda _e: self._render(), add="+")
        self.winfo_toplevel().bind("<Configure>", self._on_window_configure, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._apply_height()

    # -- размеры ---------------------------------------------------------------------

    def _font(self) -> str:
        style = ttk.Style(self)
        return str(style.lookup("TLabel", "font") or style.lookup(".", "font") or "TkDefaultFont")

    def _measure(self, text: str) -> int:
        return int(self.tk.call("font", "measure", self._font(), text))

    def lines(self) -> int:
        """Сколько строк текста слева: «подробно» - 2, «коротко» и окно ниже SMALL_WINDOW - 1."""
        return 2 if self.mode == MODE_DETAILED and not self._small else 1

    def _apply_height(self) -> None:
        linespace = int(self.tk.call("font", "metrics", self._font(), "-linespace"))
        pad = self.px(4)
        inner = max(self.lines() * linespace, self.cancel_btn.winfo_reqheight())
        height = inner + 2 * pad + 2          # 2 - рамка Card.TFrame
        if int(str(self.cget("height")) or 0) != height:
            self.configure(height=height)

    def _on_window_configure(self, event) -> None:
        top = self.winfo_toplevel()
        if event.widget is not top:
            return
        height = top.winfo_height()
        small = 1 < height < self.px(SMALL_WINDOW)      # 1 - окно ещё не показано: считаем большим
        if small != self._small:
            self._small = small
            self._apply_height()
            self._render()

    # -- режим ----------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"режим подсказок: {', '.join(MODES)}, получено {mode!r}")
        self.mode = mode
        self._apply_height()
        self._render()

    # -- текст слева ----------------------------------------------------------------

    def text(self) -> str:
        """Текст слева целиком (на экране он может быть укорочен до одной-двух строк)."""
        if self.mode == MODE_PROGRESS:
            return ""
        for slot in (self._hover, self._focus):
            if slot is not None:
                try:
                    return slot[1]()
                except tk.TclError:          # виджет уже уничтожен
                    continue
        return self._error or self._result or self.background()

    def hovered(self) -> Optional[tk.Misc]:
        """Виджет под курсором, чьё пояснение показано (None - нет такого)."""
        return self._hover[0] if self._hover is not None else None

    def hover(self, widget: tk.Misc, text: Callable[[], str]) -> None:
        self._cancel("leave")
        self._hover = (widget, text)
        self._render()

    def unhover(self, widget: tk.Misc) -> None:
        if self._hover is None or self._hover[0] is not widget:
            return
        self._cancel("leave")
        self._timers["leave"] = self.after(HOVER_DELAY_MS, self._leave)

    def _leave(self) -> None:
        self._timers["leave"] = None
        self._hover = None
        self._render()

    def focus_in(self, widget: tk.Misc, text: Callable[[], str]) -> None:
        self._focus = (widget, text)
        self._render()

    def focus_out(self, widget: tk.Misc) -> None:
        if self._focus is not None and self._focus[0] is widget:
            self._focus = None
            self._render()

    def forget(self, widget: tk.Misc) -> None:
        """Виджет уничтожен: его пояснение больше не показываем."""
        if self._hover is not None and self._hover[0] is widget:
            self._hover = None
        if self._focus is not None and self._focus[0] is widget:
            self._focus = None
        self._render()

    def show_error(self, text: str) -> None:
        """Совет о сбое без окна («Нужен вход на сервер»): виден ERROR_MS, важнее результата и фонового текста."""
        self._error = text
        self._restart("error", ERROR_MS)
        self._render()

    def show_result(self, text: str) -> None:
        """Результат действия: виден RESULT_MS, важнее фонового текста."""
        self._result = text
        self._restart("result", RESULT_MS)
        self._render()

    def set_background(self, text: str, short: Optional[str] = None) -> None:
        """Фоновый текст: что делать с выбранным проектом (или подсказка, пока проект не выбран). short - он же в режиме
        «коротко» (None - тот же текст)."""
        if (text, short) != (self._background, self._background_short):
            self._background, self._background_short = text, short
            self._render()

    def background(self) -> str:
        """Фоновый текст для текущего режима (что видно, когда нет ни наведения, ни совета, ни результата)."""
        if self.mode == MODE_SHORT and self._background_short is not None:
            return self._background_short
        return self._background

    def _restart(self, name: str, ms: int) -> None:
        self._cancel(name)
        self._timers[name] = self.after(ms, lambda: self._expire(name))

    def _expire(self, name: str) -> None:
        self._timers[name] = None
        if name == "error":
            self._error = ""
        else:
            self._result = ""
        self._render()

    def _cancel(self, name: str) -> None:
        after_id = self._timers.get(name)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
            self._timers[name] = None

    def _on_destroy(self, event) -> None:
        if event.widget is self:
            self._dead = True
            for name in list(self._timers):
                self._cancel(name)

    # -- ход операции справа --------------------------------------------------------

    @property
    def progress_active(self) -> bool:
        return bool(self._progress)

    def progress_text(self) -> str:
        return self._progress

    def set_progress(self, text: str, cancel: Optional[Callable[[], None]] = None, cancel_enabled: bool = True) -> None:
        """Ход операции справа; cancel - что делает «Отмена» (без него кнопки нет: операцию не прервать)."""
        self._progress = text or "Идёт работа…"
        self._cancel_cmd = cancel
        self.progress_label.grid()
        if cancel is not None:
            self.cancel_btn.configure(state="normal" if cancel_enabled else "disabled")
            self.cancel_btn.grid()
        else:
            self.cancel_btn.grid_remove()
        self._render()

    def clear_progress(self) -> None:
        if not self._progress and self._cancel_cmd is None:
            return
        self._progress = ""
        self._cancel_cmd = None
        self.progress_label.grid_remove()
        self.cancel_btn.grid_remove()
        self._render()

    def _on_cancel(self) -> None:
        if self._cancel_cmd is not None and not self.cancel_btn.instate(["disabled"]):
            self._cancel_cmd()

    # -- вывод на экран --------------------------------------------------------------

    def _render(self) -> None:
        if self._dead:
            return
        try:
            self._draw()
        except tk.TclError:          # окно закрывается: части панели уже уничтожены
            self._dead = True

    def _draw(self) -> None:
        width = self.winfo_width()
        pad = 2 * self.px(10) + 2
        right = 0
        if self._progress:
            room = max(self.px(120), int(width * 0.45)) if width > 1 else 0
            shown = fit_lines(self._progress, room, 1, self._measure) if room else self._progress
            if str(self.progress_label.cget("text")) != shown:
                self.progress_label.configure(text=shown)
            right = self._measure(shown) + self.px(12)
            if self._cancel_cmd is not None:
                right += self.cancel_btn.winfo_reqwidth() + self.px(8)
        if self.mode == MODE_PROGRESS:
            self.text_label.grid_remove()
            return
        self.text_label.grid()
        room = width - pad - right if width > 1 else 0
        shown = fit_lines(self.text(), room, self.lines(), self._measure)
        if str(self.text_label.cget("text")) != shown:
            self.text_label.configure(text=shown)
