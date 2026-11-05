"""Дымовой тест окна: дерево → выбор проекта → скачивание, на настоящем svn через file:///."""
import gc
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_real_svn as real  # noqa: E402

PROJECT = real.PROJECT

_cache_patches = []
_REAL_SHOW_ERROR = None


def setUpModule():
    """Кэш дерева сервера не читается из профиля пользователя и не пишется туда; окно ошибки на экран не выходит."""
    global _REAL_SHOW_ERROR
    from svnhelper.gui import App
    _REAL_SHOW_ERROR = App.show_error
    for target, kwargs in (("svnhelper.gui.download_tab.load_tree_cache", {"return_value": {}}),
                           ("svnhelper.gui.download_tab.save_tree_cache", {}),
                           ("svnhelper.gui.App.show_error", {}),
                           # ни один тест не должен открыть настоящее окно TortoiseSVN на машине пользователя;
                           # тесты, которым нужен TortoiseSVN, сами задают tortoise_exe и патчат launch
                           ("svnhelper.tortoise.find_tortoiseproc", {"return_value": None}),
                           # локальный проект по умолчанию создаётся без истории (иначе в проектах появлялась бы .svnlite);
                           # тесты истории сами подставляют настоящий svnadmin
                           ("svnhelper.config.find_svnadmin", {"return_value": None}),
                           ("svnhelper.tortoise.launch",
                            {"side_effect": AssertionError("тест не должен запускать TortoiseSVN")})):
        patch = mock.patch(target, **kwargs)
        patch.start()
        _cache_patches.append(patch)


def tearDownModule():
    for patch in _cache_patches:
        patch.stop()
    gc.collect()      # мусор закрытых окон собирает главный поток (иначе Tcl_AsyncDelete в чужом потоке)


try:
    import tkinter as tk
    from tkinter import ttk
    _probe_root = tk.Tk()
    _probe_root.withdraw()
    _probe_root.destroy()
    HAS_DISPLAY = True
except Exception:
    HAS_DISPLAY = False


def quiet_clipboard(root):
    """Окно само читает буфер обмена при старте и при фокусе: настоящий буфер пользователя в тестах не трогаем."""
    root.clipboard_get = mock.Mock(side_effect=tk.TclError("clipboard disabled in tests"))


def widget_shown(widget):
    """Виджет показан в окне: у него и у всех родителей есть менеджер геометрии, а страница вкладки выбрана.
    Окно тестов скрыто (withdraw), поэтому winfo_viewable не годится."""
    while widget is not widget.winfo_toplevel():
        parent = widget.nametowidget(widget.winfo_parent())
        if isinstance(parent, ttk.Notebook):
            if parent.select() != str(widget):
                return False
        elif not widget.winfo_manager():
            return False
        widget = parent
    return True


def shown_accents(app):
    """Показанные в главном окне кнопки в стиле Accent.TButton (диалоги не в счёте). Должна быть одна."""
    found, stack = [], [app.root]
    while stack:
        for child in stack.pop().winfo_children():
            if isinstance(child, tk.Toplevel):
                continue
            stack.append(child)
            if isinstance(child, ttk.Button) and str(child.cget("style")) == "Accent.TButton" and widget_shown(child):
                found.append(child)
    return found


def read_job(jobs_dir):
    """Единственный журнал задачи в папке или None. Фоновая задача может как раз переписывать его (os.replace):
    тогда чтение повторяется, а не падает с PermissionError (jobs.read)."""
    from svnhelper import jobs
    for path in sorted(Path(jobs_dir).glob("*.json")):
        data = jobs.read(path)
        if data is not None:
            return data
    return None


def read_job_stage(jobs_dir):
    data = read_job(jobs_dir)
    return data.get("stage") if data else None


@unittest.skipUnless(HAS_DISPLAY and shutil.which("svn") and shutil.which("svnadmin"), "нет экрана или svn")
class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        real.RealSvnTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        real.RealSvnTests.tearDownClass.__func__(cls)

    def pump(self, root, cond, timeout=20):
        end = time.monotonic() + timeout
        while not cond():
            root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), end, "таймаут ожидания")

    def open_node(self, app, iid):
        app.tree.item(iid, open=True)
        app.tree.focus(iid)
        app.tree.event_generate("<<TreeviewOpen>>")
        self.pump(app.root, lambda: app.nodes[iid].state in ("loaded", "error") or app.nodes[iid].is_project)

    def child_by_text(self, app, parent, text):
        for iid in app.tree.get_children(parent):
            if text in app.tree.item(iid, "text"):
                return iid
        self.fail(f"нет узла {text}")

    def test_full_flow(self):
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient

        root = tk.Tk()
        root.withdraw()
        quiet_clipboard(root)
        from svnhelper.config import FilterRules
        cfg = Config(server=self.server, repos=["test"], work_dir=os.path.join(self.tmp.name, "dl"),
                     filter=FilterRules(junk_dirs=["backup"]))
        store = SettingsStore(Path(self.tmp.name) / "settings.json")
        app = App(root, cfg, store, SvnClient("svn"), os.path.join(self.tmp.name, "log.txt"))
        from svnhelper.gui import App as _App
        _App.show_error.reset_mock()      # в успешных сценариях окно ошибки не показывается
        try:
            node = app.tree.get_children()[0]
            for name in ("projects", "Specific_projects", "Russia_Azov_AKF"):
                self.open_node(app, node)
                node = self.child_by_text(app, node, name)
            self.open_node(app, node)
            project = self.child_by_text(app, node, PROJECT)
            self.assertIn("✔", app.tree.item(project, "text"))
            app.tree.selection_set(project)
            root.update()
            self.assertIsNotNone(app.project)
            self.assertEqual(app.project.name, PROJECT)
            self.assertIn(PROJECT, app.url_var.get())
            self.assertEqual(str(app.download_btn.cget("state")), "normal")

            asked = []
            with mock.patch("tkinter.messagebox.askyesno",
                            side_effect=lambda t, m, **k: asked.append(m) or False), \
                    mock.patch("tkinter.messagebox.showerror") as err:
                app._on_download()
                self.pump(root, lambda: app.job is None)
                err.assert_not_called()
            dest = os.path.join(cfg.work_dir, PROJECT)
            self.assertTrue(os.path.isfile(os.path.join(dest, "чертёж 1.txt")))
            # U4.4: окна «Готово» нет, результат в карточке нового проекта, он выделен в «Мои проекты»
            self.assertEqual(asked, [])
            note = app._action_notes[os.path.normcase(dest)]
            self.assertIn("«Занять файл…»", note)
            self.assertNotIn("Get lock", note)
            self.assertIn("Сохранение № 1 на сервере", note)
            self.assertEqual(app._list_mode(), "mine")
            self.pump(root, lambda: app._selected_item() is not None and app._selected_item().name == PROJECT)
            self.assertEqual(store.settings.last_dir, cfg.work_dir)
            self.assertIn(cfg.work_dir, store.settings.project_dirs)       # папка скачивания - в общем списке
            app.show_error.assert_not_called()

            # повторное скачивание в ту же папку должно быть отклонено
            with mock.patch("tkinter.messagebox.showerror") as err:
                app._on_download()
                self.assertIn("Получить новое", err.call_args[0][1])
            self.assertIsNone(app.job)

            # ввод ссылки руками (с /trunk и слэшем на конце) в другую папку
            shutil.rmtree(dest, onerror=lambda f, path, _e: (os.chmod(path, 0o700), f(path)))
            app.url_var.set(self.server + "/test/" + "/".join(real.PROJECT_PATH) + "/trunk/")
            self.assertEqual(app.project.name, PROJECT)
            with mock.patch("tkinter.messagebox.askyesno", return_value=False),                     mock.patch("tkinter.messagebox.showerror") as err:
                app._on_download()
                self.pump(root, lambda: app.job is None)
                err.assert_not_called()
            self.assertTrue(os.path.isfile(os.path.join(dest, "чертёж 1.txt")))

            # ссылка на несуществующий проект: понятное сообщение
            app.url_var.set(self.server + "/test/projects/nope")
            with mock.patch("svnhelper.gui.App.show_error") as err:
                app._on_download()
                self.pump(root, lambda: app.job is None)
                self.assertIn("не найден", err.call_args[0][1])
                self.assertEqual(err.call_args.kwargs["action"], "Скачивание проекта")

            # проект с мусором: отчёт показывается, мусор не остаётся в папке
            app.url_var.set(self.server + "/test/projects/junk_project")
            junk_dest = os.path.join(cfg.work_dir, "junk_project")
            with mock.patch.object(App, "_confirm_audit", return_value=True) as confirm, \
                    mock.patch("tkinter.messagebox.askyesno", side_effect=lambda t, m, **k: asked.append(m) or False), \
                    mock.patch("tkinter.messagebox.showerror") as err:
                app._on_download()
                self.pump(root, lambda: app.job is None)
                err.assert_not_called()
                confirm.assert_called_once()
            self.assertEqual(sorted(n for n in os.listdir(junk_dest) if n != ".svn"), ["keep файл.txt"])
            self.assertIn("Не оставлено в папке как лишнее: 3 файла", app._action_notes[os.path.normcase(junk_dest)])

            # отказ в отчёте: ничего не скачивается
            shutil.rmtree(junk_dest, onerror=lambda f, path, _e: (os.chmod(path, 0o700), f(path)))
            with mock.patch.object(App, "_confirm_audit", return_value=False):
                app._on_download()
                self.pump(root, lambda: app.job is None)
            self.assertFalse(os.path.exists(junk_dest))

            # вкладка «Мои проекты»: список, статус с сервера, обновление
            os.makedirs(os.path.join(cfg.work_dir, "просто папка"), exist_ok=True)
            app._scan_projects()
            # после скачивания список уже искали: ждём именно новую папку, а не любые две строки
            self.pump(root, lambda: "просто папка" in {i.name for i in app.mine.values()})
            by_name = {i.name: i for i in app.mine.values()}
            self.assertEqual(by_name[PROJECT].state, "svn")
            self.assertEqual(by_name["просто папка"].state, "plain")
            app._on_check_all()
            self.pump(root, lambda: not app._checking)
            iid = app._iid_of(by_name[PROJECT])
            self.assertEqual(app.mine_tree.item(iid, "values")[2], "● Актуален")
            app.mine_tree.selection_set(iid)
            root.update()
            self.assertEqual(str(app.update_btn.cget("state")), "normal")
            with mock.patch("tkinter.messagebox.showinfo") as info:
                app._on_update_selected()
                self.pump(root, lambda: app.upd is None)
                self.assertIn("не было новых изменений", info.call_args[0][1])
            self.pump(root, lambda: not app._checking)

            # Get lock / Release lock
            target = os.path.join(dest, "docs sub", "b.bin")
            self.assertFalse(os.access(target, os.W_OK))
            with mock.patch("tkinter.filedialog.askopenfilenames", return_value=[target]),                     mock.patch("tkinter.messagebox.showinfo") as info:
                app._on_get_lock()
                self.pump(root, lambda: info.call_count >= 1)
                self.assertIn("b.bin", info.call_args[0][1])
            self.assertTrue(os.access(target, os.W_OK))
            self.pump(root, lambda: not app._checking)
            picked = []

            class FakePick:
                def __init__(self, parent, title, prompt, items, preselect=None):
                    picked.extend(items)
                    self.preselect = preselect
                    self.result = list(items)
            with mock.patch("svnhelper.gui.projects_tab.PickDialog", FakePick),                     mock.patch.object(root, "wait_window"),                     mock.patch("tkinter.messagebox.showinfo") as info:
                app._on_release_lock()
                self.pump(root, lambda: info.call_count >= 1)
            self.assertEqual(len(picked), 1)
            self.assertTrue(picked[0].startswith("docs sub" + os.sep + "b.bin"), picked[0])
            self.assertIn("занят", picked[0])  # в списке показан возраст
            self.assertFalse(os.access(target, os.W_OK))
            self.pump(root, lambda: not app._checking)

            # кривая ссылка: кнопка выключена
            app.url_var.set("http:/oops")
            self.assertEqual(str(app.download_btn.cget("state")), "disabled")
        finally:
            root.destroy()


@unittest.skipUnless(HAS_DISPLAY and shutil.which("svn") and shutil.which("svnadmin"), "нет экрана или svn")
class MineTabTests(unittest.TestCase):
    """Вкладка «Мои проекты»: состояния, автопроверка, блокировки, вход, закрытие окна."""

    FILE = "keep файл.txt"

    @classmethod
    def setUpClass(cls):
        real.RealSvnTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        real.RealSvnTests.tearDownClass.__func__(cls)

    def pump(self, root, cond, timeout=20):
        end = time.monotonic() + timeout
        while not cond():
            root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), end, "таймаут ожидания")

    def checkout(self, name):
        """Копия проекта upd_project в новой папке name; возвращает (папка, путь к копии)."""
        base = os.path.join(self.tmp.name, name)
        os.makedirs(base)
        dest = os.path.join(base, "upd_project")
        real.run("svn", "checkout", self.server + "/test/projects/upd_project/trunk", dest)
        return base, dest

    def make_app(self, name, scan=True, visible=False, scale=None):
        """Окно с настоящим svn и одной скачанной копией проекта в папке name (скрытое, если не просили показать).
        scale - масштаб экрана Windows (1.25 = 125%): px() и шрифты считаются, как на таком экране."""
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        base, _dest = self.checkout(name)
        root = tk.Tk()
        if scale is not None:
            root.tk.call("tk", "scaling", 96 * scale / 72)
        if not visible:
            root.withdraw()               # тесты ничего не показывают на экране
        quiet_clipboard(root)
        store = SettingsStore(Path(self.tmp.name) / f"{name}.json")
        app = App(root, Config(server=self.server, repos=["test"], work_dir=base), store, SvnClient("svn"),
                  os.path.join(self.tmp.name, "log.txt"))
        app.tortoise_exe = None           # результат не должен зависеть от установленного TortoiseSVN
        app._apply_tortoise()
        app.dir_var.set(base)
        if scan:
            app._scan_projects()
            self.pump(root, lambda: len(app.mine) >= 1)
            self.pump(root, lambda: not app._checking)  # автопроверка после поиска
        else:
            app._mine_dirty = False       # «Мои проекты» теперь первая вкладка: иначе первый update() запустит поиск
        return root, app, base

    def select_first(self, root, app):
        iid = next(iter(app.mine))
        app.mine_tree.selection_set(iid)
        root.update()
        return iid, app.mine[iid]

    def test_sort_by_opened_date(self):
        from svnhelper.gui.models import MineItem
        root, app, _base = self.make_app("sort", scan=False)
        try:
            for name in ("a", "b", "c"):
                item = MineItem(f"p_{name}", name, "svn")
                app.mine[app._insert_mine_row(item)] = item
            app.settings.opened = {os.path.normcase("p_b"): 200.0, os.path.normcase("p_c"): 100.0}
            app.settings.mine_sort = "opened"      # старое значение settings работает как начальная сортировка
            app._apply_mine_sort()
            names = [app.mine_tree.set(i, "name") for i in app.mine_tree.get_children()]
            self.assertEqual(names, ["b", "c", "a"])        # недавние первыми, без даты внизу
            app._on_heading_sort("name")           # то же, что щелчок по заголовку «Проект»
            app._apply_mine_sort()
            names = [app.mine_tree.set(i, "name") for i in app.mine_tree.get_children()]
            self.assertEqual(names, ["a", "b", "c"])
        finally:
            root.destroy()

    def test_status_line_in_table(self):
        from svnhelper.gui.models import MineItem
        from svnhelper.svn_core import WcStatus
        root, app, _base = self.make_app("st", scan=False)
        try:
            item = MineItem("p", "x", "svn")
            self.assertEqual(app._row_values(item)[0][3], "Нажмите «⟳»")
            item.checking = True
            self.assertEqual(app._row_values(item)[0][3], "Идёт проверка…")
            item.checking, item.status = False, WcStatus(modified=1)
            values, tag = app._row_values(item)
            self.assertEqual((values[2], tag), ("● Несохранённые правки", "new"))      # в таблице короче, чем в подробностях
            self.assertIn("Сохранить на сервер", values[3])
            item.status = WcStatus(conflicts=1)
            self.assertEqual(app._row_values(item)[1], "bad")
            self.assertEqual(app._row_values(MineItem("p", "x", "broken"))[0][3], "Нажмите «Починить папку»")

            # в настоящей таблице: колонка «Что сделать» после автопроверки
            app._scan_projects()
            self.pump(root, lambda: len(app.mine) >= 1)
            self.pump(root, lambda: not app._checking)
            iid, _item = self.select_first(root, app)
            self.assertEqual(app.mine_tree.item(iid, "values")[2:4], ("● Актуален", "Ничего делать не нужно"))
            self.assertEqual(app.mine_tree.heading("todo", "text"), "Что сделать")
            self.assertIn("Что сделать: Ничего делать не нужно", app.project_panel.header_text())
        finally:
            root.destroy()

    def test_offline_bar_and_short_status(self):
        from svnhelper import messages
        from svnhelper.gui.models import MineItem
        from svnhelper.svn_core import ErrorKind, WcStatus
        root, app, _base = self.make_app("offline", scan=False)
        try:
            offline = messages.short_message(ErrorKind.NO_CONNECTION)
            item = MineItem("p", "x", "svn")
            app.mine[app._insert_mine_row(item)] = item
            self.assertFalse(app.offline_bar.winfo_manager())
            app._on_item_status(item, None, offline)
            values, tag = app._row_values(item)
            self.assertEqual((values[2], tag), ("● Нет связи", "plain"))     # коротко, не обрезано
            self.assertTrue(app.offline_bar.winfo_manager())
            other = MineItem("q", "y", "svn")
            app.mine[app._insert_mine_row(other)] = other
            app._on_item_status(other, None, offline)                        # вторая ошибка: полоса всё та же
            self.assertEqual(len(app.offline_bar.master.pack_slaves()), len(set(app.offline_bar.master.pack_slaves())))
            self.assertIn("проверьте VPN", messages.OFFLINE_BAR)
            self.assertIn("Нет связи", app.lamp_text)
            app._on_item_status(item, WcStatus(), "")                        # сервер ответил: полоса исчезает
            self.assertFalse(app.offline_bar.winfo_manager())
            self.assertNotIn("Подключено к SVN", app.project_panel.header_text())
        finally:
            root.destroy()

    def test_scan_selects_first_row_and_offline_hint(self):
        root, app, _base = self.make_app("autosel")
        try:
            iid = next(iter(app.mine))
            self.assertEqual(app.mine_tree.selection(), (iid,))
            self.assertEqual(str(app.update_btn.cget("state")), "normal")
            self.assertNotIn(os.path.normcase(app.mine[iid].path), app.settings.opened)   # автовыбор не «открытие»
            hints = []
            with mock.patch.object(app, "_mine_hint", side_effect=hints.append):
                app._on_scanned(_base, list(app.mine.values()), None)      # без сети: про связь, а не «Подключено»
                app._set_offline(True)
                app._on_scanned(_base, list(app.mine.values()), None)
            self.assertIn("Найдено проектов: 1.", hints[0])
            self.assertIn("Нет связи с сервером", hints[1])
            self.assertFalse(any("Подключено к SVN" in h for h in hints))
        finally:
            root.destroy()

    def test_buttons_fit_into_window(self):
        """Кнопки колонки 1 и все три колонки видны целиком при 100%, 125% и 150% масштабе экрана."""
        from svnhelper.svn_core import WcStatus
        for scale in (None, 1.25, 1.5):
            with self.subTest(scale=scale):
                root, app, _base = self.make_app(f"fit{scale or ''}", visible=True, scale=scale)
                try:
                    app.tortoise_exe = "C:\\fake\\TortoiseProc.exe"
                    app._apply_tortoise()
                    iid, item = self.select_first(root, app)
                    # переход на вкладку сам проверяет проекты: дожидаемся, иначе настоящий ответ svn затрёт
                    # подставленный ниже конфликт (так тест падал через раз)
                    app.select_list_mode("mine")
                    root.update()
                    self.pump(root, lambda: not app._checking and not item.checking)
                    item.status = WcStatus(conflicts=1)  # показаны все кнопки сразу
                    app._refresh_row(item)
                    root.update()
                    right = root.winfo_rootx() + root.winfo_width()
                    bottom = root.winfo_rooty() + root.winfo_height()
                    buttons = [w for w in app.actions_row._items if w not in app.actions_row._hidden]
                    self.assertIn(app.special_btn, buttons)
                    for widget in buttons:
                        self.assertTrue(widget.winfo_ismapped(), widget)
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right, widget)
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom, widget)
                        # подпись не обрезана: кнопке дали не меньше, чем она просит
                        self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), widget)
                    self.assertFalse(app.mine_cancel_btn.winfo_ismapped())      # «Отмена» только во время операции
                    for column in (app.mine_col2, app.mine_col3):               # обе колонки помещаются в окно
                        self.assertTrue(column.body.winfo_ismapped(), column)
                        self.assertLessEqual(column.winfo_rootx() + column.winfo_width(), right, column)
                    self.assertTrue(app.mine_tree.winfo_ismapped())     # столбцы шире окна листаются полосой прокрутки
                    self.assertTrue(app.project_tabs.winfo_ismapped())
                finally:
                    root.destroy()

    def test_two_project_folders(self):
        """Две папки в списке: переключение меняет список проектов, после перезапуска обе на месте, текущая - последняя."""
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        root, app, base_a = self.make_app("dirs_a")
        base_b = os.path.join(self.tmp.name, "dirs_b")
        os.makedirs(os.path.join(base_b, "только во второй"))
        store_path = app.settings_store.path
        try:
            self.assertEqual(app.settings.project_dirs, [base_a])        # папка по умолчанию попала в список
            names = lambda: sorted(i.name for i in app.mine.values())   # noqa: E731
            self.assertEqual(names(), ["upd_project"])
            with mock.patch("tkinter.filedialog.askdirectory", return_value=base_b):
                app.mine_browse_btn.invoke()                             # «Выбрать…» над списком проектов
            self.pump(root, lambda: names() == ["только во второй"])
            values = list(app.mine_dir_combo.tk.splitlist(app.mine_dir_combo.cget("values")))
            self.assertEqual(values, [base_a, base_b, app.PROJECT_DIR_REMOVE])
            self.assertEqual(app.mine_dir_var.get(), base_b)

            app.mine_dir_var.set(base_a)                                 # выбор в выпадающем списке
            app.mine_dir_combo.event_generate("<<ComboboxSelected>>")
            self.pump(root, lambda: names() == ["upd_project"])
            self.pump(root, lambda: not app._checking)
            app.mine_dir_var.set(base_b)
            app.mine_dir_combo.event_generate("<<ComboboxSelected>>")
            self.pump(root, lambda: names() == ["только во второй"])
            # на «Скачать проект» тот же список, новая папка оттуда тоже попадает в список
            self.assertEqual(list(app.dir_entry.tk.splitlist(app.dir_entry.cget("values"))), [base_a, base_b])
            base_c = os.path.join(self.tmp.name, "dirs_c")
            with mock.patch("tkinter.filedialog.askdirectory", return_value=base_c):
                app.browse_btn.invoke()
            self.assertEqual(app.settings.project_dirs, [base_a, base_b, base_c])
            app.mine_dir_var.set(base_b)
            app.mine_dir_combo.event_generate("<<ComboboxSelected>>")
            self.pump(root, lambda: names() == ["только во второй"] and not app._checking)
        finally:
            root.destroy()

        root = tk.Tk()
        root.withdraw()
        quiet_clipboard(root)
        try:
            app = App(root, Config(server=self.server, repos=["test"], work_dir=base_a), SettingsStore(store_path),
                      SvnClient("svn"), os.path.join(self.tmp.name, "log.txt"))
            self.assertEqual(app.settings.project_dirs, [base_a, base_b, base_c])
            self.assertEqual(app.dir_var.get(), base_b)                  # текущая - последняя выбранная
            self.assertEqual(app.mine_dir_var.get(), base_b)
            # «Убрать эту папку из списка»: с диска ничего не удаляется, текущей становится первая из оставшихся
            app.mine_dir_var.set(app.PROJECT_DIR_REMOVE)
            app.mine_dir_combo.event_generate("<<ComboboxSelected>>")
            self.assertEqual(app.settings.project_dirs, [base_a, base_c])
            self.assertEqual(app.dir_var.get(), base_a)
            self.assertTrue(os.path.isdir(base_b))
            self.assertEqual(SettingsStore(store_path).settings.project_dirs, [base_a, base_c])
            self.pump(root, lambda: app._mine_scanned and not app._checking)
        finally:
            root.destroy()

    def test_tortoise_buttons_hidden_without_tortoise(self):
        root, app, _base = self.make_app("notort")
        try:
            self.select_first(root, app)
            self.assertEqual(str(app._commit_panel.save_button.cget("state")), "normal")   # сохранение не зависит от TortoiseSVN
            self.assertFalse(hasattr(app, "no_tortoise_label"))              # подписи «Установите TortoiseSVN…» нет
            self.assertEqual(sorted(app._more_idx), ["experts", "publish"])           # пункта «через TortoiseSVN» нет
            self.assertEqual(sorted(app._expert_idx), ["cleanup"])                    # в «Для опытных» только «Починить папку»
        finally:
            root.destroy()

    def test_tortoise_commit_waits_and_rechecks(self):
        root, app, _base = self.make_app("tort")
        try:
            app.tortoise_exe = "C:\\fake\\TortoiseProc.exe"
            app._apply_tortoise()
            iid, item = self.select_first(root, app)
            self.assertIn("commit", app._more_idx)         # «Ещё → Сохранить через TortoiseSVN…»
            self.assertEqual(app.more_menu.entrycget(app._more_idx["commit"], "label"), "Сохранить через TortoiseSVN…")
            calls = []
            proc = mock.Mock()
            proc.wait.side_effect = lambda: time.sleep(0.2)
            with mock.patch("svnhelper.tortoise.launch", side_effect=lambda a: calls.append(a) or proc):
                before = item.last_check
                app.more_menu.invoke(app._more_idx["commit"])
                self.assertIn(item.path, app._tortoise_open)
                self.pump(root, lambda: item.path not in app._tortoise_open)
                self.pump(root, lambda: not app._checking)
            self.assertEqual(calls[0][1], "/command:commit")
            self.assertEqual(calls[0][2], "/path:" + item.path)
            self.assertGreater(item.last_check, before)  # после закрытия окна проект проверен заново
        finally:
            root.destroy()

    def test_auto_check_never_runs_in_parallel(self):
        root, app, _base = self.make_app("auto")
        try:
            iid, item = self.select_first(root, app)
            state = {"now": 0, "max": 0, "calls": 0}
            real_status = app.client.status

            def slow_status(*a, **k):
                state["now"] += 1
                state["calls"] += 1
                state["max"] = max(state["max"], state["now"])
                time.sleep(0.3)
                try:
                    return real_status(*a, **k)
                finally:
                    state["now"] -= 1

            with mock.patch.object(app.client, "status", slow_status):
                app._check_status([item])
                app._check_status([item])         # вторая проверка встаёт в очередь
                self.assertEqual(len(app._check_queue), 1)
                self.assertTrue(item.checking)
                self.pump(root, lambda: not app._checking)
                self.assertEqual((state["max"], state["calls"]), (1, 2))
                # автопроверка не чаще раза в 5 минут на проект
                app._auto_check()
                self.assertFalse(app._checking)
                item.last_check = time.monotonic() - 301
                app._auto_check()
                self.assertTrue(app._checking)
                self.pump(root, lambda: not app._checking)
                self.assertEqual(state["calls"], 3)
        finally:
            root.destroy()

    def test_get_lock_busy_file_shows_owner(self):
        root, app, _base = self.make_app("busy")
        _other_base, other = self.checkout("busy_other")
        other_file = os.path.join(other, self.FILE)
        real.run("svn", "lock", other_file, "-m", "editing", "--username", "petrov")
        try:
            iid, item = self.select_first(root, app)
            mine_file = os.path.join(item.path, self.FILE)
            with mock.patch("tkinter.filedialog.askopenfilenames", return_value=[mine_file]), \
                    mock.patch("svnhelper.gui.App.show_error") as warn:
                app._on_get_lock()
                self.pump(root, lambda: warn.call_count >= 1)
            text = warn.call_args[0][1]
            self.assertIn("Файл занят: petrov", text)
            self.assertIn("(комментарий: editing)", text)
            self.pump(root, lambda: not app._checking)
            tree = app.project_panel.locks_tree                         # и на вкладке «Занятые»
            self.assertEqual([tree.set(i, "who") for i in tree.get_children()], ["petrov"])
            self.assertEqual(tree.set(tree.get_children()[0], "file"), self.FILE)
        finally:
            real.run("svn", "unlock", other_file, "--username", "petrov")
            root.destroy()

    def test_get_lock_on_stale_file_offers_update(self):
        root, app, _base = self.make_app("stale")
        _other_base, other = self.checkout("stale_other")
        Path(other, self.FILE).write_text("changed by other", encoding="utf-8")
        real.run("svn", "commit", "-m", "other", other, "--username", "petrov")
        try:
            iid, item = self.select_first(root, app)
            old_revision = item.revision
            mine_file = os.path.join(item.path, self.FILE)
            with mock.patch("tkinter.filedialog.askopenfilenames", return_value=[mine_file]), \
                    mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask, \
                    mock.patch("tkinter.messagebox.showinfo"), \
                    mock.patch("tkinter.messagebox.showwarning"):
                app._on_get_lock()
                self.pump(root, lambda: ask.call_count >= 1)
                self.assertIn("более новая версия", ask.call_args[0][1])
                self.assertIn("Получить новое сейчас?", ask.call_args[0][1])
                self.pump(root, lambda: app.upd is None)
                self.assertGreater(item.revision, old_revision)
                self.pump(root, lambda: not app._checking)
            self.assertEqual(Path(mine_file).read_text(encoding="utf-8"), "changed by other")
        finally:
            root.destroy()

    def test_remember_password_controls_store_auth(self):
        root, app, base = self.make_app("remember", scan=False)
        try:
            st = app.settings
            retried = []

            def login(remember):
                dlg = mock.Mock()
                dlg.result, dlg.remember = ("ivanov", "secret"), remember
                with mock.patch("svnhelper.gui.login.LoginDialog", return_value=dlg) as cls, \
                        mock.patch.object(root, "wait_window"):
                    app.login.request(retry=lambda: retried.append(1), give_up=lambda: None,
                                      job_gen=app.login.stamp())
                return cls

            cls = login(False)
            self.assertTrue(cls.call_args.kwargs["remember"])        # по умолчанию «запомнить»
            self.assertEqual((app.client.store_auth, st.password_enc, st.remember_password), (False, "", False))
            self.assertEqual(retried, [1])
            cls = login(True)
            self.assertFalse(cls.call_args.kwargs["remember"])       # теперь по умолчанию как выбрал пользователь
            self.assertTrue(app.client.store_auth)
            if sys.platform == "win32":
                self.assertTrue(st.password_enc)
                self.assertNotIn("secret", Path(app.settings_store.path).read_text(encoding="utf-8"))

            # те же правила в «Параметрах»
            values = {"config_path": "", "username": "ivanov", "password": "secret", "remember": False,
                      "server": self.server, "repos": "test", "last_dir": base, "svn_path": "",
                      "skip_junk": True, "show_report": True}
            with mock.patch.dict(os.environ, {"APPDATA": self.tmp.name}):
                app._apply_settings(values)
            self.assertEqual((app.client.store_auth, st.password_enc, st.remember_password), (False, "", False))
        finally:
            root.destroy()

    def test_login_request_without_svn_gives_up(self):
        root, app, _base = self.make_app("nosvn", scan=False)
        try:
            stamp = app.login.stamp()
            app.client = None
            gave_up = []
            with mock.patch("svnhelper.gui.login.LoginDialog") as dlg:
                app.login.request(retry=lambda: None, give_up=lambda: gave_up.append(1), job_gen=stamp)
                dlg.assert_not_called()
            self.assertEqual(gave_up, [1])
        finally:
            root.destroy()

    def test_attempts_not_reset_by_shared_config_from_disk(self):
        root, app, _base = self.make_app("l3", scan=False)
        try:
            app.login.attempts = 2
            app.settings.config_path = os.path.join(self.tmp.name, "shared.json")
            app._on_shared_loaded(app.settings.shared_source, False, app.shared_data, None, app.login.stamp())
            self.assertEqual(app.login.attempts, 2)
        finally:
            root.destroy()

    def test_close_waits_for_update_to_stop(self):
        from svnhelper.gui.models import MineItem, UpdateJob
        root, app, _base = self.make_app("close", scan=False)
        try:
            app.upd = UpdateJob(MineItem("p", "x", "svn"))
            with mock.patch("tkinter.messagebox.askyesno", return_value=True), \
                    mock.patch.object(root, "destroy") as destroy:
                app._on_close()
                self.assertTrue(app.upd.cancel.cancelled)
                self.assertEqual(app.mine_status.get(), "Останавливаю…")
                end = time.monotonic() + 0.5
                while time.monotonic() < end:  # пока задача не завершилась, окно живёт
                    root.update()
                    time.sleep(0.02)
                destroy.assert_not_called()
                app.upd = None                  # cleanup после отмены закончился
                self.pump(root, lambda: destroy.called)
        finally:
            root.destroy()

    def test_close_gives_up_after_timeout(self):
        from svnhelper.gui.models import MineItem, UpdateJob
        root, app, _base = self.make_app("close2", scan=False)
        try:
            app.upd = UpdateJob(MineItem("p", "x", "svn"))
            with mock.patch("tkinter.messagebox.askyesno", return_value=True), \
                    mock.patch("svnhelper.gui.app.CLOSE_WAIT_SEC", 0.3), \
                    mock.patch.object(root, "destroy") as destroy:
                app._on_close()
                self.pump(root, lambda: destroy.called, timeout=5)
        finally:
            root.destroy()

    # ---- раунд исправлений 2 -------------------------------------------------------------

    def slow_status(self, app, delay=0.4):
        """Подменяет client.status медленным вариантом (чтобы успеть вмешаться в проверку)."""
        real_status = app.client.status

        def slow(*a, **k):
            time.sleep(delay)
            return real_status(*a, **k)

        return mock.patch.object(app.client, "status", slow)

    def test_busy_folder_blocks_update_and_locks(self):
        """Пока проект открыт в TortoiseSVN (или чинится), Update и Get/Release lock недоступны."""
        root, app, _base = self.make_app("busy_path")
        try:
            app.tortoise_exe = "C:\\fake\\TortoiseProc.exe"
            app._apply_tortoise()
            iid, item = self.select_first(root, app)
            self.assertEqual(str(app.update_btn.cget("state")), "normal")
            app._tortoise_open.add(item.path.upper())      # регистр пути значения не имеет
            app._update_mine_buttons()
            for btn in (app.update_btn, app.project_panel.lock_btn):      # «Занять файл…» справа, на «Занятых»
                self.assertEqual(str(btn.cget("state")), "disabled")
            self.assertEqual(str(app.project_panel.release_btn.cget("state")), "disabled")
            with mock.patch.object(app.client, "update") as upd, mock.patch.object(app.client, "lock") as lock:
                self.assertFalse(app._begin_update(item))
                self.assertIsNone(app.upd)
                app._run_locks(item, [os.path.join(item.path, self.FILE)], lock=True)
                app._on_update_selected()
                time.sleep(0.2)
                root.update()
                upd.assert_not_called()
                lock.assert_not_called()
            app._tortoise_open.clear()
            app._update_mine_buttons()
            self.assertEqual(str(app.update_btn.cget("state")), "normal")
        finally:
            root.destroy()

    def test_cleanup_without_tortoise_marks_folder_busy(self):
        root, app, _base = self.make_app("cleanup_busy")
        try:
            iid, item = self.select_first(root, app)
            calls = []
            real_cleanup = app.client.cleanup

            def slow_cleanup(path):
                calls.append(path)
                time.sleep(0.4)
                return real_cleanup(path)

            with mock.patch.object(app.client, "cleanup", slow_cleanup):
                app._on_cleanup()
                self.assertTrue(app._path_busy(item.path))
                self.assertEqual(str(app.update_btn.cget("state")), "disabled")
                app._on_cleanup()                                  # второй запуск отклонён
                self.assertFalse(app._begin_update(item))          # Update во время cleanup тоже
                self.pump(root, lambda: not app._path_busy(item.path))
            self.assertEqual(len(calls), 1)
            self.assertEqual(str(app.update_btn.cget("state")), "normal")
            self.pump(root, lambda: not app._checking)
        finally:
            root.destroy()

    def test_rescan_during_check_does_not_leave_item_checking(self):
        """«Найти проекты» во время проверки: новая строка получает результат и не застревает в «Идёт проверка…»."""
        root, app, _base = self.make_app("rescan")
        try:
            iid, old_item = self.select_first(root, app)
            with self.slow_status(app):
                app._check_status([old_item])
                self.assertTrue(old_item.checking)
                app._scan_projects()
                self.pump(root, lambda: app.mine and next(iter(app.mine.values())) is not old_item)
                self.pump(root, lambda: not app._checking and not app._check_queue, timeout=30)
            new_item = next(iter(app.mine.values()))
            self.assertFalse(new_item.checking)
            self.assertIsNotNone(new_item.status)
            self.assertNotIn("Идёт проверка", app.mine_tree.item(next(iter(app.mine)), "values")[2])
        finally:
            root.destroy()

    def test_checks_queued_during_update_run_after_it(self):
        """Очередь проверок не теряется, если обновление началось во время проверки."""
        from svnhelper.gui.models import UpdateJob
        root, app, _base = self.make_app("upd_queue")
        try:
            iid, item = self.select_first(root, app)
            with self.slow_status(app, 0.3):
                app._check_status([item])
                app._check_status([item])                  # встала в очередь за текущей
                app.upd = UpdateJob(item)                  # началось обновление
                self.pump(root, lambda: not app._checking)
                self.assertEqual(len(app._check_queue), 1)  # проверка ждёт конца обновления, а не потеряна
                app._end_update()
                self.assertTrue(app._checking)
                self.pump(root, lambda: not app._checking and not app._check_queue)
            self.assertFalse(item.checking)
            self.assertIsNotNone(item.status)
            # во время обновления новая проверка не стартует и не оставляет «Идёт проверка…»
            app.upd = UpdateJob(item)
            item.checking = False
            app._check_status([item])
            self.assertFalse(app._checking)
            self.assertFalse(item.checking)
            app._end_update()
            self.pump(root, lambda: not app._checking and not app._check_queue)
        finally:
            root.destroy()

    def test_tortoise_close_after_rescan_rechecks_current_item(self):
        root, app, _base = self.make_app("tort_rescan")
        try:
            app.tortoise_exe = "C:\\fake\\TortoiseProc.exe"
            app._apply_tortoise()
            iid, old_item = self.select_first(root, app)
            proc = mock.Mock()
            proc.wait.side_effect = lambda: time.sleep(0.3)
            with mock.patch("svnhelper.tortoise.launch", return_value=proc):
                app.more_menu.invoke(app._more_idx["commit"])
                app._scan_projects()
                self.pump(root, lambda: app.mine and next(iter(app.mine.values())) is not old_item)
                self.pump(root, lambda: not app._path_busy(old_item.path))
                self.pump(root, lambda: not app._checking and not app._check_queue)
            new_item = next(iter(app.mine.values()))
            self.assertFalse(new_item.checking)
            self.assertIsNotNone(new_item.status)
        finally:
            root.destroy()

    def test_locks_not_started_twice_and_progress_kept_while_checking(self):
        root, app, _base = self.make_app("lock_twice")
        try:
            iid, item = self.select_first(root, app)
            target = os.path.join(item.path, self.FILE)
            gate = []
            real_lock = app.client.lock

            def slow_lock(path):
                gate.append(path)
                time.sleep(0.4)
                return real_lock(path)

            with mock.patch.object(app.client, "lock", slow_lock), \
                    mock.patch("tkinter.messagebox.showinfo"):
                app._run_locks(item, [target], lock=True)
                self.assertEqual(str(app.project_panel.lock_btn.cget("state")), "disabled")
                app._run_locks(item, [target], lock=True)          # повтор отклонён
                with self.slow_status(app, 0.8):
                    app._check_status([item])                      # проверка идёт параллельно
                    self.pump(root, lambda: not app._locking)
                    self.assertTrue(app._checking)
                    self.assertTrue(app._mine_spinning)            # полосу блокировка не погасила
                    self.pump(root, lambda: not app._checking)
                self.assertFalse(app._mine_spinning)
            self.assertEqual(gate, [target])
            real.run("svn", "unlock", target)
        finally:
            root.destroy()

    def test_login_cancelled_when_client_vanished_gives_up(self):
        root, app, _base = self.make_app("vanish", scan=False)
        try:
            gave_up, retried = [], []
            dlg = mock.Mock()
            dlg.result, dlg.remember = ("ivanov", "secret"), True
            with mock.patch("svnhelper.gui.login.LoginDialog", return_value=dlg), \
                    mock.patch.object(root, "wait_window", side_effect=lambda _d: setattr(app, "client", None)):
                app.login.request(retry=lambda: retried.append(1), give_up=lambda: gave_up.append(1),
                                  job_gen=app.login.stamp())
            self.assertEqual((gave_up, retried), ([1], []))
        finally:
            root.destroy()

    def test_login_label_confirmed_only_after_server_answer(self):
        root, app, _base = self.make_app("label", scan=False)
        try:
            app.client.set_credentials("alice", "wrong")
            app._update_login_label()
            self.assertEqual(app.login_var.get(), "Логин: alice")
            app.login.reset_attempts()                      # сервер ответил успешно
            self.assertEqual(app.login_var.get(), "Логин: alice")
            app.client.set_credentials("alice", "other")    # данные сменились: пока не подтверждены
            app._update_login_label()
            self.assertEqual(app.login_var.get(), "Логин: alice")
            app.login.reset_attempts()
            app.mark_login_failed()                         # сервер отверг
            self.assertEqual(app.login_var.get(), "Логин: alice")
        finally:
            root.destroy()

    def test_header_has_no_double_period(self):
        from svnhelper.gui.models import MineItem
        root, app, _base = self.make_app("details", scan=False)
        try:
            item = MineItem("p", "x", "svn", url="u", checking=True)
            item.line = None
            app.mine[app._insert_mine_row(item)] = item
            app.mine_tree.selection_set(app._iid_of(item))
            root.update()
            text = app.project_panel.header_text()
            self.assertIn("Что сделать: Идёт проверка…", text)
            self.assertNotIn("…. ", text)
            self.assertNotIn("….", text)
            self.assertNotIn("..", text)
            self.assertEqual(app._sentence("Нажмите «Проверить»"), "Нажмите «Проверить».")
            self.assertEqual(app._sentence("Ура!"), "Ура!")
            self.assertEqual(app._sentence("Конец."), "Конец.")
        finally:
            root.destroy()

    def test_table_columns_favor_todo_and_menu_buttons_have_arrow(self):
        root, app, _base = self.make_app("cols", scan=False)
        try:
            width = lambda col: int(app.mine_tree.column(col, "width"))  # noqa: E731
            self.assertGreater(width("todo"), width("status"))
            self.assertEqual(list(app.mine_tree["displaycolumns"]), ["brief"])           # узкий вид: один столбец
            app.mine_col3.collapse()                                                     # широкий вид
            shown = list(app.mine_tree["displaycolumns"])
            self.assertEqual(shown, ["name", "status", "line", "changed", "todo"])
            self.assertEqual([app.mine_tree.heading(c, "text") for c in shown],
                             ["Проект ▲", "Состояние", "Ветка", "Изменён", "Что сделать"])
            self.assertEqual(app.mine_tree.heading("path", "text"), "Путь на сервере")   # колонка осталась, но не показана
            self.assertEqual(app.check_btn.cget("text"), "⟳")
            self.assertEqual(app.more_btn.cget("text"), "Ещё ▾")
            self.assertEqual(app.recent_btn.cget("text"), "Недавние ▾")
            self.assertFalse(hasattr(app, "list_btn"))      # дерево сервера видно сразу: «Выбрать из списка ▾» нет
        finally:
            root.destroy()

    def test_header_has_branch_revision_and_no_branch_history(self):
        from svnhelper.gui.models import MineItem
        from svnhelper.svn_core import LockInfo, WcStatus
        root, app, _base = self.make_app("rows", scan=False)
        try:
            item = MineItem("p", "x", "svn", url="u", revision=7, line=None,
                            status=WcStatus(modified=2, incoming=1, unversioned=3, head=9,
                                            my_locks=[LockInfo("a", "me")],
                                            other_locks=[LockInfo(f"f{i}", "petrov") for i in range(4)]))
            title, status, tag, branch, todo, note = app._header_fields(item)
            self.assertEqual((title, tag, note), ("x", "new", ""))
            self.assertIn("сохранение № 7 (на сервере уже № 9)", branch)
            self.assertTrue(todo.endswith("."))
            text = " ".join((title, status, branch, todo))
            self.assertNotIn("Создал", text)
            self.assertNotIn("Работали", text)
            self.assertFalse(hasattr(app, "mine_card"))             # карточка подробностей упразднена
            self.assertFalse(hasattr(app, "detail_rows"))
        finally:
            root.destroy()

    def test_cancel_buttons_visible_only_during_operations(self):
        from svnhelper.gui.models import DownloadJob, UpdateJob
        root, app, _base = self.make_app("cancel")
        try:
            iid, item = self.select_first(root, app)
            self.assertEqual(app.cancel_btn.winfo_manager(), "")
            self.assertFalse(app.mine_cancel_btn.winfo_ismapped())
            app.job = DownloadJob(url="u", name="n", dest="d")
            app._update_buttons()
            self.assertEqual(app.cancel_btn.winfo_manager(), "pack")
            self.assertEqual(str(app.cancel_btn.cget("state")), "normal")
            app.job = None
            app._update_buttons()
            self.assertEqual(app.cancel_btn.winfo_manager(), "")
            app.upd = UpdateJob(item)
            app._update_buttons()
            self.assertNotIn(app.mine_cancel_btn, app.actions_row._hidden)
            self.assertEqual(str(app.update_btn.cget("state")), "disabled")
            app.upd = None
            app._update_buttons()
            self.assertIn(app.mine_cancel_btn, app.actions_row._hidden)
        finally:
            root.destroy()

    def test_close_without_dialogs_and_releases_mutex_before_destroy(self):
        """Идёт закрытие: ошибки скачивания не показывают окон, мьютекс освобождается до destroy."""
        from svnhelper.gui.models import DownloadJob
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app, _base = self.make_app("closing", scan=False)
        try:
            order = mock.Mock()
            with mock.patch("svnhelper.gui.App.show_error") as err, \
                    mock.patch("tkinter.messagebox.showerror") as box, \
                    mock.patch("tkinter.messagebox.showinfo") as info, \
                    mock.patch.object(app.login, "request") as request:
                app._closing = True
                job = DownloadJob(url="u", name="n", dest="d", client=app.client)
                app.job = job
                app._finish_error_text(job, "Мало места", "текст")
                self.assertIsNone(app.job)
                job = app.job = DownloadJob(url="u", name="n", dest="d", client=app.client)
                app._on_estimated(job, None, SvnError(ErrorKind.AUTH, "svn: E170001"), app.login.stamp())
                self.assertIsNone(app.job)
                job = app.job = DownloadJob(url="u", name="n", dest="d", client=app.client)
                app._on_checked_out(job, None, SvnError(ErrorKind.AUTH, "svn: E170001"), app.login.stamp())
                self.assertIsNone(app.job)
                job = app.job = DownloadJob(url="u", name="n", dest="d", client=app.client)
                app._on_checked_out(job, None, OSError(5, "нет доступа"), app.login.stamp())
                self.assertIsNone(app.job)
                job = app.job = DownloadJob(url="u", name="n", dest="d", client=app.client)
                app._finish_error(job, ErrorKind.OTHER, "raw")
                self.assertIsNone(app.job)
                err.assert_not_called()
                box.assert_not_called()
                info.assert_not_called()
                request.assert_not_called()
            with mock.patch("svnhelper.winutil.release_single_instance", order.release), \
                    mock.patch.object(root, "destroy", order.destroy):
                app._finish_close()
            self.assertEqual([c[0] for c in order.mock_calls], ["release", "destroy"])
        finally:
            root.destroy()

    def test_close_waits_for_partial_folder_removal(self):
        root, app, _base = self.make_app("close_rm", scan=False)
        try:
            app._removing_partial = True
            with mock.patch("tkinter.messagebox.askyesno", return_value=True), \
                    mock.patch.object(root, "destroy") as destroy:
                app._on_close()
                end = time.monotonic() + 0.5
                while time.monotonic() < end:
                    root.update()
                    time.sleep(0.02)
                destroy.assert_not_called()                # удаление папки ещё идёт
                app._removing_partial = False
                self.pump(root, lambda: destroy.called)
        finally:
            root.destroy()

    def test_auth_declined_shows_no_error_dialog(self):
        """Окно входа закрыто отменой: второе окно «Не удалось скачать» не показывается, хватит строки статуса."""
        from svnhelper.gui.models import DownloadJob
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app, _base = self.make_app("declined", scan=False)
        try:
            with mock.patch("svnhelper.gui.App.show_error") as err, \
                    mock.patch("tkinter.messagebox.showerror") as box, \
                    mock.patch.object(app.login, "request", side_effect=lambda retry, give_up, job_gen: give_up()):
                job = app.job = DownloadJob(url="u", name="n", dest="d", client=app.client)
                app._on_estimated(job, None, SvnError(ErrorKind.AUTH, "svn: E170001"), app.login.stamp())
                self.assertIsNone(app.job)
                self.assertIn("нужен вход", app.status_var.get())
                job = app.job = DownloadJob(url="u", name="n", dest="d", client=app.client)
                app._on_checked_out(job, None, SvnError(ErrorKind.AUTH, "svn: E170001"), app.login.stamp())
                self.assertIsNone(app.job)
                err.assert_not_called()
                box.assert_not_called()
        finally:
            root.destroy()

    def test_error_and_login_dialogs_never_stack(self):
        """Окно входа не открывается поверх окна ошибки, а окно ошибки поверх окна входа."""
        root, app, _base = self.make_app("stack", scan=False)
        try:
            retried = []
            dlg = mock.Mock()
            dlg.result, dlg.remember = ("ivanov", "secret"), True
            real_show = _REAL_SHOW_ERROR
            seen = {}

            def fake_error_dialog(*a, **k):
                seen["login_during_error"] = None
                # пока ErrorDialog «открыт», просим вход: окно входа показываться не должно
                with mock.patch("svnhelper.gui.login.LoginDialog", return_value=dlg) as login_cls:
                    app.login.request(retry=lambda: retried.append(1), give_up=lambda: None,
                                      job_gen=app.login.stamp())
                    seen["login_during_error"] = login_cls.call_count
                return mock.Mock()

            with mock.patch("svnhelper.gui.app.ErrorDialog", side_effect=fake_error_dialog), \
                    mock.patch.object(root, "wait_window"):
                real_show(app, "Ошибка", "Текст", action="Проверка")
                self.assertEqual(seen["login_during_error"], 0)
                self.assertEqual(len(app.login.waiters), 1)         # задача ждёт, а не потеряна
                with mock.patch("svnhelper.gui.login.LoginDialog", return_value=dlg) as login_cls:
                    self.pump(root, lambda: retried)                 # после закрытия ошибки вход запрашивается
                    self.assertEqual(login_cls.call_count, 1)
                # и наоборот: ошибка во время окна входа ждёт его закрытия
                shown = []
                app.login.dialog_open = True
                real_show(app, "Ошибка 2", "Текст", action="Проверка")
                app.login.dialog_open = False
                self.assertEqual(len(app._pending_errors), 1)
                with mock.patch("svnhelper.gui.App.show_error", side_effect=lambda *a, **k: shown.append(a)):
                    app.after_modal_closed()
                self.assertEqual(shown[0][0], "Ошибка 2")
        finally:
            root.destroy()

    def test_lock_holder_text(self):
        from datetime import datetime, timezone
        from svnhelper.gui.models import lock_holder_text
        from svnhelper.svn_core import LockInfo
        info = LockInfo("f", "Петров", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc), "правлю чертёж")
        self.assertEqual(lock_holder_text(info), "Файл занят: Петров, с 12.10 (комментарий: правлю чертёж)")
        self.assertEqual(lock_holder_text(LockInfo("f", "Петров")), "Файл занят: Петров")


class PureHelpersTests(unittest.TestCase):
    """Логика без окна: ETA, поиск по кэшу, удаление папки, сообщения проверки подключения."""

    def test_eta_needs_total_and_five_seconds(self):
        from svnhelper.progress import EtaTracker, format_eta
        now = [0.0]
        eta = EtaTracker(clock=lambda: now[0])
        self.assertIsNone(eta.update(0, 1000))
        now[0] = 3.0
        self.assertIsNone(eta.update(300, 1000))           # прошло меньше 5 с
        now[0] = 6.0
        self.assertIsNone(eta.update(600, None))           # размер неизвестен
        self.assertAlmostEqual(eta.update(600, 1000), 4.0, places=1)   # 100 объектов/с, осталось 400
        # скорость считается по последним ~10 с: старая быстрая часть забывается
        now[0] = 30.0
        eta.update(700, 1000)
        now[0] = 40.0
        self.assertAlmostEqual(eta.update(800, 1000), 20.0, delta=1.0)  # 10 объектов/с
        self.assertEqual(format_eta(None), "")
        self.assertEqual(format_eta(20), "осталось меньше минуты")
        self.assertEqual(format_eta(180), "осталось ~3 мин")
        self.assertEqual(format_eta(3600 * 1.5), "осталось ~1 ч 30 мин")

    def test_search_projects_in_cache(self):
        from svnhelper.gui.models import search_projects
        folders = {
            "konstruktor": {"dirs": [["Азов", False], ["Бетон_проект", True]], "ts": 1},
            "konstruktor/Азов": {"dirs": [["Цех азов", True], ["мусор", "bad"], 5], "ts": 1},
            "automation": {"dirs": [], "is_project": True, "ts": 1},
            "broken": "мусор",
        }
        hits, cut = search_projects("азов", [["konstruktor", "Азов", "Цех азов"]], folders)
        self.assertEqual(hits, [["konstruktor", "Азов", "Цех азов"]])      # дубль из узла и кэша схлопнут
        self.assertFalse(cut)
        self.assertEqual(search_projects("automation", [], folders)[0], [["automation"]])
        hits, cut = search_projects("п", [], {"a": {"dirs": [[f"п{i}", True] for i in range(10)]}}, limit=3)
        self.assertEqual((len(hits), cut), (3, True))

    def test_remove_partial_dir_handles_readonly_and_guards_base(self):
        import stat
        from svnhelper import fsutil
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "base")
            dest = os.path.join(base, "proj")
            os.makedirs(os.path.join(dest, "sub"))
            locked = os.path.join(dest, "sub", "чертёж.dwg")
            Path(locked).write_text("x")
            os.chmod(locked, stat.S_IREAD)                  # как файл с needs-lock
            fsutil.remove_partial_dir(dest, base)
            self.assertFalse(os.path.exists(dest))
            self.assertTrue(os.path.isdir(base))
            with self.assertRaises(ValueError):
                fsutil.remove_partial_dir(base, base)       # саму базу удалять нельзя
            with self.assertRaises(ValueError):
                fsutil.remove_partial_dir(tmp, base)        # и папку выше неё

    def test_connection_error_texts(self):
        from svnhelper.gui.login import connection_error_text
        from svnhelper.svn_core import ErrorKind
        self.assertIn("Неверный логин или пароль", connection_error_text(ErrorKind.AUTH, "test", True))
        self.assertIn("Введите", connection_error_text(ErrorKind.AUTH, "test", False))
        self.assertIn("Нет связи", connection_error_text(ErrorKind.NO_CONNECTION, "test", True))
        self.assertIn("«test»", connection_error_text(ErrorKind.AUTHZ, "test", True))

    @unittest.skipUnless(sys.platform == "win32", "только Windows")
    def test_shortcut_with_apostrophe_in_path(self):
        from svnhelper import winutil
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "Иван's папка")
            os.makedirs(folder)
            link = os.path.join(folder, "SVN-lite's.lnk")
            winutil.create_shortcut(sys.executable, link, "тест")
            self.assertTrue(os.path.isfile(link))

    def test_single_instance_mutex_second_acquire_fails(self):
        from svnhelper import winutil
        name = "Local\\SvnHelper_test_%d" % os.getpid()
        saved = winutil._mutex_handle
        try:
            self.assertTrue(winutil.acquire_single_instance(name))
            self.assertEqual(winutil.acquire_single_instance(name), sys.platform != "win32")
        finally:
            winutil._mutex_handle = saved


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class Stage3GuiTests(unittest.TestCase):
    """Вкладка «Скачать проект»: буфер обмена, недавние, поиск, ошибки, отложенная пересборка, проверка подключения."""

    def make_app(self, **config_kwargs):
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        grab = mock.patch.object(tk.Toplevel, "grab_set")      # у скрытого окна захват ввода невозможен
        grab.start()
        self.addCleanup(grab.stop)
        root = tk.Tk()
        root.withdraw()                                        # тесты ничего не показывают на экране
        quiet_clipboard(root)
        self.addCleanup(root.destroy)
        cfg = Config(server="svn://srv", repos=["test"], work_dir=os.path.join(tmp.name, "dl"), **config_kwargs)
        app = App(root, cfg, SettingsStore(Path(tmp.name) / "s.json"), SvnClient("svn"),
                  os.path.join(tmp.name, "log.txt"))
        return root, app, tmp.name

    def test_theme_switch_recolors_and_is_remembered(self):
        from svnhelper import theme
        from svnhelper.config import SettingsStore
        root, app, tmp = self.make_app()
        app.set_theme("dark")
        self.assertIs(app.palette, theme.DARK)
        self.assertEqual(app.lock_warn.cget("background"), theme.DARK.warn_bg)
        self.assertEqual(str(app.mine_tree.tag_configure("ok", "foreground")), theme.DARK.success)
        self.assertEqual(SettingsStore(Path(tmp) / "s.json").settings.theme, "dark")
        app.set_theme("light")
        self.assertEqual(app.lock_warn.cget("background"), theme.LIGHT.warn_bg)
        app.set_theme("нет такой")                              # неизвестный режим игнорируется
        self.assertEqual(app.settings.theme, "light")

    def reopen(self, tmp):
        """Второе окно с теми же настройками (как после перезапуска программы)."""
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        root = tk.Tk()
        root.withdraw()
        quiet_clipboard(root)
        self.addCleanup(root.destroy)
        return App(root, Config(server="svn://srv", repos=["test"], work_dir=os.path.join(tmp, "dl")),
                   SettingsStore(Path(tmp) / "s.json"), SvnClient("svn"), os.path.join(tmp, "log.txt"))

    def test_columns_collapse_and_state_survives_restart(self):
        from svnhelper import theme
        from svnhelper.config import SettingsStore
        from svnhelper.gui.widgets import CollapsibleColumn
        root, app, tmp = self.make_app()
        tab, col2, col3 = app.mine_tab, app.mine_col2, app.mine_col3
        weight = lambda index: int(tab.grid_columnconfigure(index, "weight"))   # noqa: E731
        self.assertFalse(col2.collapsed or col3.collapsed)
        self.assertEqual((weight(0), weight(1), weight(3)), (0, 0, 1))          # растягивается колонка 3
        self.assertEqual(app.mine_sash.winfo_manager(), "grid")                  # между развёрнутыми колонками - разделитель
        self.assertEqual(col2.body.winfo_manager(), "grid")
        self.assertEqual(col2.strip.winfo_manager(), "")
        toggled = []
        col2.bind(CollapsibleColumn.EVENT, lambda _e: toggled.append("col2"), add="+")

        col2.toggle_btn.invoke()                                                 # стрелка «‹» сворачивает
        self.assertTrue(col2.collapsed)
        self.assertEqual(toggled, ["col2"])
        self.assertEqual((col2.body.winfo_manager(), col2.strip.winfo_manager()), ("", "grid"))
        self.assertTrue(col2.strip.find_withtag("title"))                         # вертикальная подпись нарисована
        self.assertEqual(col2.strip.itemcget("title", "text"), "Проекты")
        self.assertTrue(SettingsStore(Path(tmp) / "s.json").settings.mine_col2_collapsed)
        self.assertTrue(col2.strip.bind("<Button-1>"))                           # щелчок по полосе разворачивает
        col2.expand()                                                            # (у скрытого окна щелчков нет)
        self.assertFalse(col2.collapsed)
        self.assertFalse(SettingsStore(Path(tmp) / "s.json").settings.mine_col2_collapsed)

        col3.toggle_btn.invoke()                                                 # колонка 3 свёрнута:
        self.assertEqual((weight(1), weight(3)), (1, 0))                         # колонка 2 занимает её место
        self.assertEqual(app.mine_sash.winfo_manager(), "")                      # разделитель не нужен
        self.assertEqual(col3.strip.itemcget("title", "text"), "Состояние")
        app.set_theme("dark")                                                    # полоса перерисована в тёмной теме
        self.assertEqual(col3.strip.itemcget("title", "fill"), theme.DARK.muted)
        self.assertEqual(col3.strip.cget("background"), theme.DARK.surface_alt)
        col2.collapse()                                                          # обе свёрнуты: ничего не растягивается,
        self.assertEqual((weight(1), weight(3)), (0, 1))                         # полоса «Состояние» у правого края
        self.assertEqual(col3.grid_info()["sticky"], "nes")

        again = self.reopen(tmp)                                                 # после перезапуска - как было
        self.assertTrue(again.mine_col2.collapsed and again.mine_col3.collapsed)
        again.mine_col2.expand()
        self.assertEqual((int(again.mine_tab.grid_columnconfigure(1, "weight")),
                          int(again.mine_tab.grid_columnconfigure(3, "weight"))), (1, 0))

    def test_column_sash_resizes_projects_column_and_remembers_width(self):
        from svnhelper.config import SettingsStore
        root, app, tmp = self.make_app()
        root.deiconify()
        root.geometry("1100x700")
        root.update()
        col2, sash = app.mine_col2, app.mine_sash
        before = col2.winfo_width()

        def drag(dx):
            start = sash.winfo_rootx() + 2
            sash.event_generate("<ButtonPress-1>", x=2, y=5, rootx=start, rooty=0)
            sash.event_generate("<B1-Motion>", x=2 + dx, y=5, rootx=start + dx, rooty=0)
            sash.event_generate("<ButtonRelease-1>", x=2 + dx, y=5, rootx=start + dx, rooty=0)
            root.update()

        drag(60)
        self.assertGreater(col2.winfo_width(), before + 40)                      # колонка «Проекты» стала шире
        self.assertEqual(SettingsStore(Path(tmp) / "s.json").settings.mine_col2_width,
                         round(col2.width / (app.px(1000) / 1000)))               # и ширина запомнена
        drag(-5000)
        self.assertEqual(col2.width, app.px(app.MINE_COL2_MIN))                  # меньше минимума не сузить
        drag(5000)
        self.assertGreaterEqual(app.mine_col3.winfo_width(), app.px(app.MINE_COL3_MIN) - 5)   # колонке 3 хватает места
        self.assertEqual(self.reopen(tmp).mine_col2.width, col2.width)           # после перезапуска - та же ширина

    def test_menu_buttons_fit_their_text(self):
        root, app, tmp = self.make_app()
        root.update_idletasks()
        buttons = [w for w in app.menu_bar.winfo_children()]
        self.assertEqual([b.cget("text") for b in buttons], ["Настройки", "Вид", "Справка"])
        widths = {b.cget("text"): b.winfo_reqwidth() for b in buttons}
        self.assertLess(widths["Вид"], widths["Справка"])                        # короткая подпись - узкая кнопка
        self.assertLess(widths["Справка"], widths["Настройки"])

    def test_open_project_tab_expands_column(self):
        from svnhelper.config import SettingsStore
        root, app, tmp = self.make_app()
        self.assertEqual([key for key, _title in app.PROJECT_TABS], ["changes", "incoming", "history", "locks", "lines"])
        self.assertEqual([app.project_tabs.tab(f, "text") for f in app.project_tabs.tabs()],
                         ["Изменения", "Новое", "История", "Занятые", "Ветки"])
        app.mine_col3.collapse()
        app.open_project_tab("lines")
        self.assertFalse(app.mine_col3.collapsed)
        self.assertEqual(app.project_tabs.select(), str(app.project_tab_frames["lines"]))
        self.assertFalse(SettingsStore(Path(tmp) / "s.json").settings.mine_col3_collapsed)
        app.open_project_tab("changes")                                          # уже развёрнута: просто другая вкладка
        self.assertEqual(app.project_tabs.select(), str(app.project_tab_frames["changes"]))
        with self.assertRaises(ValueError):
            app.open_project_tab("нет такой")

    def test_offline_bar_grows_window(self):
        """Полоса «Нет связи» подгоняет высоту окна, как полоса «Доступна новая версия», и возвращает её обратно."""
        root, app, _tmp = self.make_app()
        before = (app._min_height, app._base_height)
        app._set_offline(True)
        grown = app._min_height - before[0]
        self.assertGreaterEqual(grown, app.offline_bar.winfo_reqheight())
        self.assertEqual(app._base_height - before[1], grown)
        app._set_offline(True)                                                   # повторный показ окно не растит
        self.assertEqual(app._min_height - before[0], grown)
        app._set_offline(False)
        self.assertEqual((app._min_height, app._base_height), before)

    def pump(self, root, cond, timeout=10):
        end = time.monotonic() + timeout
        while not cond():
            root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), end, "таймаут ожидания")

    def test_paste_from_clipboard(self):
        root, app, _ = self.make_app()
        clip = ["svn://srv/test/projects/Мой проект/trunk"]    # настоящий буфер обмена пользователя не трогаем
        with mock.patch.object(root, "clipboard_get", side_effect=lambda: clip[0]):
            app._paste_from_clipboard()
            self.assertEqual(app.project.name, "Мой проект")
            self.assertIn("из буфера обмена", app.url_status.cget("text"))
            # то, что человек уже ввёл, не перезаписывается
            app.url_var.set("svn://srv/test/projects/own")
            clip[0] = "svn://srv/test/projects/other"
            app._paste_from_clipboard()
            self.assertEqual(app.project.name, "own")
            # чужой текст игнорируется
            app.url_var.set("")
            app._clip_used = ""
            clip[0] = "https://example.com/page"
            app._paste_from_clipboard()
            self.assertEqual(app.url_var.get(), "")
        # в буфере не текст
        with mock.patch.object(root, "clipboard_get", side_effect=tk.TclError("not text")):
            app._paste_from_clipboard()
        self.assertEqual(app.url_var.get(), "")

    def test_recent_and_favorites_menu(self):
        from svnhelper.config import add_recent
        root, app, _ = self.make_app()
        add_recent(app.settings, "svn://srv/test/a", "a", "C:\\a")
        add_recent(app.settings, "svn://srv/test/b", "b", "C:\\b")
        app.url_var.set("svn://srv/test/a")
        app._toggle_favorite()
        self.assertEqual(app.settings.favorites, ["svn://srv/test/a"])
        self.assertEqual(app.fav_btn.cget("text"), "★ Убрать из избранного")
        app._fill_recent_menu()
        menu = app.recent_menu
        commands = [i for i in range(menu.index("end") + 1) if menu.type(i) == "command"]
        labels = [menu.entrycget(i, "label") for i in commands]
        self.assertTrue(labels[0].startswith("★"))            # избранное сверху
        self.assertIn("svn://srv/test/b", labels[1])
        menu.invoke(commands[1])
        self.assertEqual(app.url_var.get(), "svn://srv/test/b")
        app.url_var.set("svn://srv/test/a")
        app._toggle_favorite()                                 # a убрана из избранного
        self.assertEqual(app.fav_btn.cget("text"), "☆ В избранное")

    def test_search_uses_cache_and_click_fills_url(self):
        root, app, _ = self.make_app()
        app.tree_cache = {"server": "svn://srv", "folders": {
            "test": {"dirs": [["Азов", False], ["Бетон", True]], "ts": 1},
            "test/Азов": {"dirs": [["Цех", True]], "ts": 1}}}
        app.search_var.set("цех")
        self.pump(root, lambda: app.results_pane.winfo_manager() and app._search_hits)
        self.assertEqual(list(app._search_hits.values()), [["test", "Азов", "Цех"]])
        app.results.selection_set("r0")
        root.update()
        self.assertEqual(app.url_var.get(), "svn://srv/test/Азов/Цех")
        app.search_var.set("")
        root.update()
        self.assertFalse(app.results_pane.winfo_manager())

    def test_cached_folder_shown_before_server_answers(self):
        from svnhelper.server import FolderEntry, FolderListing
        root, app, _ = self.make_app()
        app.tree_cache = {"server": "svn://srv", "folders": {"test": {"dirs": [["Кэш", True]], "ts": 1}}}
        iid = app.tree.get_children()[0]

        def slow(*_a):
            time.sleep(0.3)
            return FolderListing(entries=[FolderEntry("Свежий", True)])

        with mock.patch("svnhelper.server.load_folder", side_effect=slow):
            app._load_node(iid)
            self.assertIn("Кэш", app.tree.item(app.tree.get_children(iid)[0], "text"))
            self.assertIn("обновляется", app.tree_status.cget("text"))
            self.pump(root, lambda: app.nodes[iid].state == "loaded")
        self.assertEqual([app.tree.item(c, "text") for c in app.tree.get_children(iid)], ["✔  Свежий"])
        self.assertEqual(app.tree_cache["folders"]["test"]["dirs"], [["Свежий", True]])

    def test_error_dialog_copies_report_without_password(self):
        from svnhelper import messages
        from svnhelper.dialogs import ErrorDialog
        from svnhelper.svn_core import ErrorKind
        root, app, _ = self.make_app(support="Иванов И., доб. 123")
        report = messages.admin_report(
            "Скачивание проекта", ErrorKind.NO_CONNECTION,
            "svn: E170013: Unable to connect\nsvn --password hunter2 checkout", app.log_path)
        dialog = ErrorDialog(root, "Ошибка", "Нет связи", report, support=app.config.support,
                             log_path=app.log_path)
        copied = []
        try:
            with mock.patch.object(dialog, "clipboard_clear"),                     mock.patch.object(dialog, "clipboard_append", side_effect=copied.append):
                dialog.copy_report()
            clip = copied[0]
            self.assertIn("E170013", clip)
            self.assertNotIn("hunter2", clip)
            self.assertEqual(dialog.copy_btn.cget("text"), "Скопировать отчёт для администратора")
        finally:
            dialog.destroy()

    def test_missing_tools_offer_install_from_server(self):
        root, app, _ = self.make_app(tortoise_installer_path="\\srv\common\TortoiseSVN.msi")
        app.tortoise_exe = None
        with mock.patch("svnhelper.gui.app.messagebox.askyesno", return_value=True) as ask,                 mock.patch.object(app, "_install_tortoise") as install:
            app._check_tools(shared_settled=False)
            app._check_tools(shared_settled=True)      # второй раз не спрашиваем
        self.assertEqual(ask.call_count, 1)
        install.assert_called_once_with("\\srv\common\TortoiseSVN.msi")

    def test_missing_tools_without_installer_waits_for_shared_config(self):
        root, app, _ = self.make_app()
        app.tortoise_exe = None
        with mock.patch("svnhelper.gui.app.messagebox.askyesno") as ask:
            app._check_tools(shared_settled=True)
        ask.assert_not_called()

    def test_show_error_passes_support_and_report(self):
        from svnhelper.svn_core import ErrorKind
        root, app, _ = self.make_app(support="Иванов И.")
        with mock.patch("svnhelper.gui.app.ErrorDialog") as cls, mock.patch.object(root, "wait_window"):
            _REAL_SHOW_ERROR(app, "Заголовок", "Текст", action="Обновление проекта", kind=ErrorKind.TIMEOUT,
                           raw="svn: E175012: timeout")
        args, kwargs = cls.call_args
        self.assertIn("Обновление проекта", args[3])
        self.assertIn("E175012", args[3])
        self.assertEqual(kwargs["support"], "Иванов И.")

    def test_rebuild_config_deferred_during_job(self):
        from svnhelper.gui.models import DownloadJob
        root, app, _ = self.make_app()
        client = app.client
        app.job = DownloadJob(url="u", name="n", dest="d", client=client)
        app.settings.repos = ["other"]
        app._rebuild_config()
        self.assertTrue(app._pending_rebuild)
        self.assertEqual(app.config.repos, ["test"])           # пока идёт задача, конфиг не меняется
        self.assertIs(app.job.client, client)
        app._end_job()
        self.assertFalse(app._pending_rebuild)
        self.assertEqual(app.config.repos, ["other"])

    def test_remove_partial_folder_flow(self):
        import stat
        from svnhelper.gui.models import DownloadJob
        root, app, tmp = self.make_app()
        base = os.path.join(tmp, "dl")
        dest = os.path.join(base, "proj")
        os.makedirs(dest)
        locked = os.path.join(dest, "a.bin")
        Path(locked).write_text("x")
        os.chmod(locked, stat.S_IREAD)
        job = DownloadJob(url="u", name="proj", dest=dest, base=base, dest_fresh=True)
        app._offer_cleanup(job)
        self.assertTrue(app.cleanup_btn.winfo_manager())
        with mock.patch("tkinter.messagebox.askyesno", return_value=False):
            app._on_remove_partial()
        self.assertTrue(os.path.isdir(dest))                   # без подтверждения ничего не удаляется
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            app._on_remove_partial()
            self.pump(root, lambda: not os.path.exists(dest))
        self.pump(root, lambda: not app.cleanup_btn.winfo_manager())
        # папка, которая существовала до старта и не была пустой, не предлагается к удалению
        os.makedirs(dest)
        Path(dest, "mine.txt").write_text("мои файлы")
        app._offer_cleanup(DownloadJob(url="u", name="proj", dest=dest, base=base, dest_fresh=False))
        self.assertFalse(app.cleanup_btn.winfo_manager())

    def test_connection_check_with_mock(self):
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app, _ = self.make_app()
        values = {"username": "ivanov", "password": "secret", "server": "svn://srv", "repos": "test, x",
                  "svn_path": ""}
        fake = mock.Mock()
        fake.version.return_value = (1, 14)
        fake.list_dirs.return_value = ["a"]
        with mock.patch("svnhelper.gui.app.SvnClient", return_value=fake), \
                mock.patch("svnhelper.gui.app.find_svn", return_value="svn"):
            text, ok = app._connection_check(values)
            self.assertTrue(ok)
            self.assertIn("вы вошли как ivanov", text)
            self.assertIn("1.14", text)
            fake.list_dirs.assert_called_once_with("svn://srv/test")
            fake.list_dirs.side_effect = SvnError(ErrorKind.AUTH, "svn: E170001")
            text, ok = app._connection_check(values)
            self.assertFalse(ok)
            self.assertIn("Неверный логин или пароль", text)
            fake.list_dirs.side_effect = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013")
            self.assertIn("Нет связи", app._connection_check(values)[0])
        with mock.patch("svnhelper.gui.app.find_svn", return_value=None):
            self.assertIn("Не найдена программа svn.exe", app._connection_check(values)[0])

    def test_dialogs_build(self):
        """Дымовое построение окон: Параметры с проверкой подключения, О программе, полоса новой версии."""
        from svnhelper.dialogs import AboutDialog
        from svnhelper.gui.widgets import SettingsDialog
        root, app, _ = self.make_app(latest_version="99.0", update_path="C:\\new")
        self.assertTrue(app.update_bar.winfo_manager())
        self.assertIn("99.0", app.update_var.get())
        root.update()
        self.assertLessEqual(root.winfo_width(), 800)
        shown = []
        dlg = SettingsDialog(root, app.config, app.settings, False,
                             check_cb=lambda values, show: show("Всё в порядке", True) or shown.append(values))
        try:
            dlg._check()
            self.assertEqual(dlg._check_label.cget("text"), "Всё в порядке")
            self.assertEqual(len(shown), 1)
        finally:
            dlg.destroy()
        about = AboutDialog(root, "1.0.0", ["Программа svn: версия 1.14"], support="Иванов")
        about.destroy()
        app.config.latest_version = ""
        app._update_version_bar()
        self.assertFalse(app.update_bar.winfo_manager())
        self.assertEqual(str(app.help_menu.entrycget(1, "state")), "disabled")  # не exe: ярлык недоступен

    def test_notify_only_for_long_background_tasks(self):
        root, app, _ = self.make_app()
        with mock.patch("svnhelper.winutil.flash_window") as flash, \
                mock.patch("svnhelper.winutil.beep") as beep, \
                mock.patch.object(root, "focus_displayof", return_value=None):
            app._notify_done(time.monotonic() - 5)             # короткая задача
            flash.assert_not_called()
            app._notify_done(time.monotonic() - 60)
            flash.assert_called_once()
            beep.assert_called_once()
            app._notify_done(time.monotonic() - 60, cancelled=True)
            self.assertEqual(flash.call_count, 1)
        with mock.patch("svnhelper.winutil.flash_window") as flash, \
                mock.patch.object(root, "focus_displayof", return_value=root):
            app._notify_done(time.monotonic() - 60)             # окно в фокусе
            flash.assert_not_called()


    # ---- раунд исправлений 2 -------------------------------------------------------------

    def settings_values(self, tmp, **override):
        values = {"config_path": "", "username": "ivanov", "password": "", "remember": True,
                  "server": "svn://srv", "repos": "test", "last_dir": tmp, "svn_path": "",
                  "skip_junk": True, "show_report": True}
        values.update(override)
        return values

    def test_settings_empty_password_keeps_saved_password(self):
        root, app, tmp = self.make_app()
        st = app.settings
        st.username, st.password_enc = "ivanov", "ENC"
        with mock.patch.dict(os.environ, {"APPDATA": tmp}):
            # клиент без введённого пароля (например, расшифровка не удалась) и пустое поле: пароль не стирается
            self.assertIsNone(app.client.credentials)
            app._apply_settings(self.settings_values(tmp))
            self.assertEqual(st.password_enc, "ENC")
            self.assertTrue(st.remember_password)
            # снят флажок: пароль стирается
            app._apply_settings(self.settings_values(tmp, remember=False))
            self.assertEqual(st.password_enc, "")

    def test_settings_login_change_does_not_reuse_old_password(self):
        root, app, tmp = self.make_app()
        st = app.settings
        st.username, st.password_enc = "ivanov", "ENC"
        app.client.set_credentials("ivanov", "old-secret")
        with mock.patch.dict(os.environ, {"APPDATA": tmp}):
            app._apply_settings(self.settings_values(tmp, username="petrov"))
        self.assertEqual(st.password_enc, "")                       # пароль принадлежал другому логину
        self.assertEqual(app.client.credentials.username, "ivanov")  # и новому логину не приписан
        self.assertEqual(app.client.credentials.password, "old-secret")
        # тот же логин и пустое поле: пароль из памяти остаётся в силе
        with mock.patch.dict(os.environ, {"APPDATA": tmp}):
            app._apply_settings(self.settings_values(tmp, username="ivanov"))
        self.assertEqual(app.client.credentials.password, "old-secret")

    def test_login_dialog_empty_password_new_login_clears_saved(self):
        root, app, tmp = self.make_app()
        st = app.settings
        st.username, st.password_enc = "ivanov", "ENC"
        dlg = mock.Mock()
        dlg.result, dlg.remember = ("petrov", ""), True
        with mock.patch("svnhelper.gui.login.LoginDialog", return_value=dlg), mock.patch.object(root, "wait_window"), \
                mock.patch.dict(os.environ, {"APPDATA": tmp}):
            app.login.request(retry=lambda: None, give_up=lambda: None, job_gen=app.login.stamp())
        self.assertEqual(st.password_enc, "")
        dlg.result = ("petrov", "")
        st.password_enc = "ENC2"
        with mock.patch("svnhelper.gui.login.LoginDialog", return_value=dlg), mock.patch.object(root, "wait_window"), \
                mock.patch.dict(os.environ, {"APPDATA": tmp}):
            app.login.attempts = 0
            app.login.request(retry=lambda: None, give_up=lambda: None, job_gen=app.login.stamp())
        self.assertEqual(st.password_enc, "ENC2")                    # тот же логин: сохранённый пароль цел

    def test_update_path_opens_only_folder(self):
        root, app, tmp = self.make_app()
        folder = os.path.join(tmp, "новая версия")
        os.makedirs(folder)
        exe = os.path.join(tmp, "setup.exe")
        Path(exe).write_text("x")
        with mock.patch("os.startfile", create=True) as start, \
                mock.patch("subprocess.Popen") as popen:
            app._open_update_target(folder)
            start.assert_called_once_with(folder)
            popen.assert_not_called()
            app._open_update_target(exe)                            # файл не запускаем, а показываем в проводнике
            start.assert_called_once()
            popen.assert_called_once_with(f'explorer /select,"{exe}"')
            with self.assertRaises(OSError):
                app._open_update_target("ftp://server/new.exe")
            with self.assertRaises(OSError):
                app._open_update_target(os.path.join(tmp, "нет такого"))

    def test_info_folder_fills_docs_menu_and_help(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        info = os.path.join(holder.name, "INFO")
        os.makedirs(info)
        Path(info, "ШПАРГАЛКА.html").write_text("x")
        Path(info, "Регламент.pdf").write_text("x")
        Path(info, "run.exe").write_text("x")
        root, app, tmp = self.make_app(info_path=info)
        app._rescan_docs()
        deadline = time.monotonic() + 5
        while len(app.docs) < 2 and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        self.assertEqual([e.title for e in app.docs], ["Регламент", "ШПАРГАЛКА"])
        self.assertEqual(app.help_menu.entrycget("Документы", "state"), "normal")
        self.assertTrue(app._help_target().endswith("ШПАРГАЛКА.html"))   # help_path пуст: берётся из папки
        labels = [app.docs_menu.entrycget(i, "label") for i in range(app.docs_menu.index("end") + 1)
                  if app.docs_menu.type(i) == "command"]
        self.assertEqual(labels, ["Регламент", "ШПАРГАЛКА", "Обновить список"])

    def test_menu_bar_is_inside_window_and_themed(self):
        """Строка меню нарисована в окне (родное меню Windows тему не принимает и остаётся белым)."""
        root, app, tmp = self.make_app()
        root.update()
        self.assertEqual(str(root.cget("menu")), "")
        buttons = [w for w in app.menu_bar.winfo_children() if w.winfo_class() == "TMenubutton"]
        self.assertEqual([str(b.cget("text")) for b in buttons], ["Настройки", "Вид", "Справка"])
        for button in buttons:
            self.assertEqual(button.winfo_manager(), "pack")
            self.assertLessEqual(button.winfo_x() + button.winfo_width(), app.menu_bar.winfo_width())
        self.assertIs(app.menu_bar.master, app.header)      # в строке шапки: высоту окна не отнимает

    def test_docs_menu_disabled_without_info_path(self):
        root, app, tmp = self.make_app()
        app._rescan_docs()
        root.update()
        self.assertEqual(app.help_menu.entrycget("Документы", "state"), "disabled")

    def test_manual_update_check_reports_latest_or_offers(self):
        from svnhelper import __version__
        root, app, tmp = self.make_app()
        with mock.patch("tkinter.messagebox.showinfo") as info, mock.patch.object(app, "_offer_self_update") as offer:
            app.config.latest_version = __version__
            app._report_update_check()
            self.assertIn("последняя версия", info.call_args[0][1])
            offer.assert_not_called()
            app.config.latest_version = "99.0.0"
            app.config.update_path = tmp
            with mock.patch("svnhelper.selfupdate.is_supported", return_value=True):
                app._report_update_check()
            offer.assert_called_once()
            with mock.patch("svnhelper.selfupdate.is_supported", return_value=False):
                app._report_update_check()                      # запуск из исходников: только сообщение
            self.assertIn("99.0.0", info.call_args[0][1])
            self.assertEqual(offer.call_count, 1)

    def test_update_check_and_poll_finish_pending_cli_swap(self):
        """Помощник закрылся, svnlite.exe.new освободился: окно доводит подмену при проверке обновления и раз в час."""
        root, app, tmp = self.make_app()
        app.settings.config_path = os.path.join(tmp, "svn_helper.json")
        with mock.patch("svnhelper.selfupdate.apply_pending", return_value=[]) as pending,                 mock.patch("tkinter.messagebox.showinfo"), mock.patch.object(app, "reload_shared_config"),                 mock.patch.object(root, "after"):
            app._check_for_update()                           # до проверки
            pending.assert_called_once_with()
            pending.reset_mock()
            app._report_update_check()                        # и после неё
            pending.assert_called_once_with()
            pending.reset_mock()
            app._update_poll()                                # и при часовой проверке
            pending.assert_called_once_with()

    def test_hourly_update_poll_rereads_shared_config_quietly(self):
        root, app, tmp = self.make_app()
        app.settings.config_path = os.path.join(tmp, "svn_helper.json")      # общий конфиг на диске
        with mock.patch.object(app, "reload_shared_config") as reload,                 mock.patch.object(root, "after") as after:
            app._update_poll()
            reload.assert_called_once_with()                      # тихо: без окон про «настройки обновлены»
            after.assert_called_once_with(app.UPDATE_POLL_MS, app._update_poll)   # и снова через час
            app.job = object()                                    # идёт скачивание: ничего не трогаем
            app._update_poll()
            reload.assert_called_once()
            app.job = None

    def test_update_offer_asked_once_per_version(self):
        from svnhelper import __version__
        root, app, tmp = self.make_app()
        app.config.latest_version = "99.0.0"
        root.deiconify()                                         # окно на экране (в трее вопрос откладывается)
        with mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask,                 mock.patch.object(app, "_start_self_update") as start:
            app._offer_self_update()
            self.assertEqual(app._offered_version, "99.0.0")
            ask.assert_called_once()
            start.assert_not_called()
            app.job = object()                                    # занято: не спрашиваем и не помечаем
            app._offered_version = ""
            app._offer_self_update()
            ask.assert_called_once()
            self.assertEqual(app._offered_version, "")
            app.job = None

    def test_update_loop_guard(self):
        """Обновление до версии уже запускали, а версия не выросла (на сервере старый exe): сами больше не спрашиваем."""
        root, app, tmp = self.make_app()
        app.config.latest_version = "99.0.0"
        app.config.update_path = tmp
        with mock.patch("svnhelper.selfupdate.is_supported", return_value=True),                 mock.patch.object(root, "after") as after:
            app.settings.update_attempt = "99.0.0"
            app._update_version_bar(resize=False)
            self.assertIn("старый файл", app.update_var.get())
            self.assertFalse(any(c.args[1:] == (app._offer_self_update,) for c in after.call_args_list))
            app.settings.update_attempt = ""
            app._update_version_bar(resize=False)
            self.assertTrue(any(c.args[1:] == (app._offer_self_update,) for c in after.call_args_list))

    def test_help_path_allows_only_documents(self):
        from svnhelper.gui import App
        for ok in ("C:\\x\\ШПАРГАЛКА.html", "\\\\srv\\share\\help.HTM", "d:\\help.pdf", "https://wiki/help"):
            self.assertTrue(App.help_target_allowed(ok), ok)
        for bad in ("C:\\x\\setup.exe", "\\\\srv\\share\\run.bat", "ftp://srv/help.html", "file:///C:/x.html",
                    "C:\\x\\help"):
            self.assertFalse(App.help_target_allowed(bad), bad)
        root, app, tmp = self.make_app(help_path=os.path.join(tmp_path(), "run.exe"))
        with mock.patch("tkinter.messagebox.showinfo") as info, \
                mock.patch("os.startfile", create=True) as start:
            app._open_help()
            info.assert_called_once()
            self.assertIn("Шпаргалку можно открыть только", info.call_args[0][1])
            time.sleep(0.2)
            root.update()
            start.assert_not_called()

    def test_cache_store_ignores_listing_from_previous_server(self):
        from svnhelper.server import FolderEntry, FolderListing
        root, app, _ = self.make_app()
        app.tree_cache = {"server": "svn://srv", "folders": {}}
        listing = FolderListing(entries=[FolderEntry("a", True)])
        with mock.patch("svnhelper.gui.download_tab.save_tree_cache") as save:
            app._cache_store(["test"], listing, "svn://old-server")     # загрузка началась до смены сервера
            self.assertEqual(app.tree_cache["folders"], {})
            save.assert_not_called()
            app._cache_store(["test"], listing, "svn://srv")
            self.assertEqual(list(app.tree_cache["folders"]), ["test"])

    def test_settings_and_login_dialogs_wrap_long_text(self):
        from svnhelper.gui.widgets import LoginDialog, SettingsDialog
        root, app, _ = self.make_app()
        dlg = SettingsDialog(root, app.config, app.settings, False, check_cb=lambda values, show: None)
        try:
            root.update()
            self.assertLessEqual(dlg.winfo_reqwidth(), app.px(720))
            self.assertLessEqual(dlg.winfo_width(), app.px(720))
        finally:
            dlg.destroy()
        login = LoginDialog(root, "ivanov", "Для доступа к серверу нужны ваш логин и пароль SVN.")
        try:
            root.update()
            self.assertLessEqual(login.winfo_reqwidth(), app.px(520))
        finally:
            login.destroy()

    def test_second_instance_without_window_tells_user(self):
        """Первый экземпляр ещё закрывается (окна нет): второй говорит об этом, а не молча выходит."""
        from svnhelper import gui
        fake_root = mock.Mock()
        with mock.patch("svnhelper.winutil.acquire_single_instance", return_value=False), \
                mock.patch("svnhelper.tray.notify_running_instance", return_value=False), \
                mock.patch("svnhelper.winutil.activate_existing_window", return_value=False), \
                mock.patch("tkinter.Tk", return_value=fake_root), \
                mock.patch("tkinter.messagebox.showinfo") as info, \
                mock.patch("svnhelper.gui.app.setup_logging") as logging_setup:
            gui.main()
            info.assert_called_once()
            self.assertIn("уже запущена или закрывается", info.call_args[0][1])
            logging_setup.assert_not_called()                           # дальше запуск не пошёл
        with mock.patch("svnhelper.winutil.acquire_single_instance", return_value=False), \
                mock.patch("svnhelper.tray.notify_running_instance", return_value=False), \
                mock.patch("svnhelper.winutil.activate_existing_window", return_value=True), \
                mock.patch("tkinter.messagebox.showinfo") as info:
            gui.main()
            info.assert_not_called()                                    # окно нашли и показали: сообщение не нужно

    def test_unreadable_settings_show_message_release_mutex_and_return(self):
        """settings.json занят и после ожидания: окно говорит об этом, отпускает мьютекс и не открывается."""
        from svnhelper import gui
        from svnhelper.config import READ_WAIT_WINDOW, SettingsUnreadable
        from svnhelper.gui import app as app_module
        fake_root = mock.Mock()
        with mock.patch("svnhelper.winutil.acquire_single_instance", return_value=True),                 mock.patch("svnhelper.winutil.release_single_instance") as release,                 mock.patch.object(app_module, "enable_dpi_awareness"),                 mock.patch.object(app_module, "setup_logging", return_value=Path("w.log")),                 mock.patch("svnhelper.selfupdate.apply_pending"),                 mock.patch("svnhelper.selfupdate.record_window_version"),                 mock.patch.object(app_module, "SettingsStore", side_effect=SettingsUnreadable("занят")) as store,                 mock.patch.object(app_module, "load_shared_cache") as shared,                 mock.patch("tkinter.Tk", return_value=fake_root) as tk_root,                 mock.patch("tkinter.messagebox.showerror") as error:
            gui.main()
        store.assert_called_once_with(load_wait=READ_WAIT_WINDOW)     # окно ждёт дольше, чем CLI и сервер помощника
        self.assertGreaterEqual(READ_WAIT_WINDOW, 3.0)
        error.assert_called_once()
        self.assertIn("файл настроек", error.call_args[0][1])
        release.assert_called_once_with()
        shared.assert_not_called()                                   # дальше запуск не пошёл
        tk_root.assert_called_once()                                 # только скрытый корень для сообщения

    def test_exit_if_threads_alive_flushes_log_and_exits(self):
        from svnhelper import gui
        stop = threading.Event()
        worker = threading.Thread(target=stop.wait, name="probe_0")      # не-демон, как поток пула
        worker.start()
        try:
            with mock.patch("os._exit") as hard_exit, \
                    mock.patch("logging.shutdown") as flush:
                gui.exit_if_threads_alive()
                flush.assert_called_once()
                hard_exit.assert_called_once_with(0)
        finally:
            stop.set()
            worker.join()
        # потоки пулов окон из прошлых тестов могут ещё жить: здесь важно только поведение без живых потоков
        with mock.patch("threading.enumerate", return_value=[threading.main_thread()]), \
                mock.patch("os._exit") as hard_exit:
            gui.exit_if_threads_alive()                                 # живых потоков нет: выходим как обычно
            hard_exit.assert_not_called()

    def test_find_window_finds_tk_window(self):
        from svnhelper import winutil
        if not winutil.IS_WINDOWS:
            self.skipTest("только Windows")
        root, app, _ = self.make_app()
        root.title(f"{winutil.WINDOW_TITLE_PREFIX} test-find")
        root.update()                         # скрытое окно EnumWindows тоже перечисляет
        self.assertTrue(winutil.find_window(f"{winutil.WINDOW_TITLE_PREFIX} test-find"))
        self.assertIsNone(winutil.find_window("Такого окна нет xyz"))
        self.assertTrue(winutil.toplevel_handle(root.winfo_id()))


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class ListModeTests(unittest.TestCase):
    """U4.3: «Скачать проект» - подвкладка колонки «Проекты», вкладок окна нет (docs/PLAN_ui_v4.md, раздел 3)."""

    def make_app(self, scale=None, visible=False, folder=True):
        """Окно без настоящего svn; folder - создать папку для проектов (пустую). Поиск проектов при запуске идёт
        сам с первым update(), как у настоящей программы."""
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tk.Tk()
        if scale is not None:
            root.tk.call("tk", "scaling", 96 * scale / 72)
        if not visible:
            root.withdraw()
        quiet_clipboard(root)
        self.addCleanup(lambda: root.winfo_exists() and root.destroy())
        work_dir = os.path.join(tmp.name, "dl")
        if folder:
            os.makedirs(work_dir)
        self.settings_path = Path(tmp.name) / "s.json"
        app = App(root, Config(server="svn://srv", repos=["test"], work_dir=work_dir),
                  SettingsStore(self.settings_path), SvnClient("svn"), os.path.join(tmp.name, "log.txt"),
                  startup_jobs=False)
        return root, app, work_dir

    def pump(self, root, cond, timeout=10):
        end = time.monotonic() + timeout
        while not cond():
            root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), end, "таймаут ожидания")

    @staticmethod
    def inside(widget, container):
        return str(widget).startswith(str(container) + ".")

    def test_download_subtab_takes_both_columns_and_hides_left_one(self):
        root, app, _ = self.make_app()
        app._mine_dirty = False
        root.update()
        self.assertEqual(app._list_mode(), "mine")
        self.assertEqual((app.actions_row.winfo_manager(), app.project_panel.winfo_manager(),
                          app.download_form.winfo_manager()), ("grid", "grid", ""))
        self.assertEqual(app.mine_col3.title_label.cget("text"), "Состояние проекта")
        self.assertEqual(app.mine_col2.width, app.px(app.MINE_COL2_WIDTH))
        # дерево сервера - в колонке «Проекты», форма и её кнопки - в колонке 3
        self.assertTrue(self.inside(app.tree, app.list_frames["download"]))
        self.assertTrue(self.inside(app.mine_tree, app.list_frames["mine"]))
        for widget in (app.url_entry, app.line_combo, app.dir_entry, app.progress, app.download_btn):
            self.assertTrue(self.inside(widget, app.mine_col3.body), widget)

        app.select_list_mode("download")
        self.assertEqual(app._list_mode(), "download")
        self.assertEqual((app.actions_row.winfo_manager(), app.project_panel.winfo_manager(),
                          app.download_form.winfo_manager()), ("", "", "grid"))
        cell = app.download_form.grid_info()
        self.assertEqual((int(cell["row"]), int(cell["column"])), (0, 0))       # та же ячейка, что у панели
        self.assertEqual(app.mine_col3.title_label.cget("text"), "Скачивание")
        self.assertEqual(app.mine_col2.width, app.px(app.MINE_COL2_DOWNLOAD_WIDTH))   # дереву нужно шире
        app.mine_col3.collapse()
        self.assertEqual(app.mine_col3.strip.itemcget("title", "text"), "Скачивание")
        app.mine_col3.expand()

        app.select_list_mode("mine")
        self.assertEqual((app.actions_row.winfo_manager(), app.project_panel.winfo_manager(),
                          app.download_form.winfo_manager()), ("grid", "grid", ""))
        self.assertEqual(app.mine_col3.title_label.cget("text"), "Состояние проекта")
        self.assertEqual(app.mine_col2.width, app.px(app.MINE_COL2_WIDTH))
        with self.assertRaises(ValueError):
            app.select_list_mode("нет такой")

    def test_download_subtab_expands_columns_also_on_click(self):
        from svnhelper.config import SettingsStore
        root, app, _ = self.make_app()
        app._mine_dirty = False
        root.update()
        app.mine_col2.collapse()
        app.mine_col3.collapse()
        app.select_list_mode("download")              # кнопка или первый запуск: обе колонки разворачиваются
        self.assertFalse(app.mine_col2.collapsed or app.mine_col3.collapsed)
        saved = SettingsStore(self.settings_path).settings
        self.assertFalse(saved.mine_col2_collapsed or saved.mine_col3_collapsed)
        app.select_list_mode("mine")
        app.mine_col3.collapse()
        app.list_tabs.select(app.list_frames["download"])     # щелчок по ярлыку: то же через событие
        root.update()
        self.assertFalse(app.mine_col3.collapsed)
        self.assertEqual(app.download_form.winfo_manager(), "grid")
        self.assertEqual(app.actions_row.winfo_manager(), "")

    def test_mine_subtab_refreshes_once_per_visit_and_auto_check_waits(self):
        root, app, _ = self.make_app()
        app._mine_dirty = False
        root.update()
        app.select_list_mode("download")
        root.update()
        app._mine_dirty = True                         # например, папку сменили на «Скачать проект»
        with mock.patch.object(app, "_scan_projects") as scan, mock.patch.object(app, "_on_check_all") as check:
            app.select_list_mode("mine")
            root.update()                              # запоздавшее событие ярлыков ничего не повторяет
            app.select_list_mode("mine")
            root.update()
            scan.assert_called_once_with()
            check.assert_not_called()
            app._mine_dirty = False
            app.select_list_mode("download")
            app.select_list_mode("mine")
            root.update()
            check.assert_called_once_with()            # при каждом возвращении состояние свежее
        with mock.patch.object(app, "_check_status") as status,                 mock.patch.object(root, "state", return_value="normal"):     # окно на экране, а не в трее
            app._auto_refresh_tick()                   # на «Мои проекты» автопроверка идёт
            self.assertEqual(status.call_count, 1)
            app.select_list_mode("download")
            app._auto_refresh_tick()                   # на «Скачать проект» - нет
            self.assertEqual(status.call_count, 1)

    def test_empty_folder_shows_one_highlighted_way_to_download(self):
        root, app, work_dir = self.make_app()
        app._start_tab_chosen = True                   # не первый запуск: подвкладка сама не переключается
        self.pump(root, lambda: app._mine_scanned)
        self.assertEqual(app._list_mode(), "mine")
        self.assertEqual((app.mine_empty.winfo_manager(), app.mine_table.winfo_manager(),
                          app.mine_find_row.winfo_manager()), ("grid", "", ""))
        self.assertEqual(app.mine_empty_label.cget("text"), "В этой папке пока нет проектов.")

        # на всём экране одна подсветка: «＋ Новый проект…» слева обычного стиля
        self.assertEqual(shown_accents(app), [app.mine_empty_btn])
        self.assertEqual(app.mine_empty_btn.cget("text"), "Скачать проект с сервера")
        app.mine_empty_btn.invoke()
        self.assertEqual(app._list_mode(), "download")
        os.makedirs(os.path.join(work_dir, "папка"))   # появился проект (обычная папка): снова таблица
        app.select_list_mode("mine")
        app._scan_projects()
        self.pump(root, lambda: app.mine)
        self.assertEqual((app.mine_empty.winfo_manager(), app.mine_table.winfo_manager()), ("", "grid"))
        self.assertEqual(app._list_mode(), "mine")

    def test_missing_folder_on_first_run_offers_download_without_switching(self):
        root, app, _ = self.make_app(folder=False)    # первый запуск: папку ещё никто не выбирал, её создаст скачивание
        self.pump(root, lambda: app._mine_scanned)
        self.assertEqual(app._list_mode(), "mine")    # подвкладку сами не меняем
        self.assertEqual(app.mine_empty.winfo_manager(), "grid")
        self.assertIn("не найдена", app.project_panel.header_text())
        self.assertEqual(app.mine_empty_btn.cget("text"), "Скачать проект с сервера")

    def test_vanished_folder_offers_to_choose_another_not_to_download(self):
        """Папка, которую человек уже выбирал, пропала (отключён сетевой диск): текст про папку и подсвеченная
        «Выбрать…», без «пока нет проектов» и без совета скачивать; пока папки нет, скачивание не предлагается."""
        root, app, work_dir = self.make_app(folder=False)
        app.settings.last_dir = work_dir               # папка выбрана раньше, не по умолчанию
        self.pump(root, lambda: app._mine_scanned)
        self.assertEqual(app._list_mode(), "mine")
        self.assertEqual(app.mine_empty.winfo_manager(), "grid")
        texts = " ".join(str(w.cget("text")) for w in (app.mine_empty_label, app.mine_empty_hint))
        self.assertIn(f"Папка «{work_dir}» не найдена.", texts)
        self.assertIn("подключён ли сетевой диск", texts)
        self.assertIn("выберите другую папку", texts)
        for forbidden in ("пока нет проектов", "Скачайте", "Скачать"):
            self.assertNotIn(forbidden, texts)
        self.assertEqual(app.mine_empty_btn.cget("text"), "Выбрать…")
        self.assertEqual(shown_accents(app), [app.mine_empty_btn])                 # одна подсветка
        with mock.patch.object(app, "_on_browse") as browse:
            app.mine_empty_btn.invoke()
        browse.assert_called_once_with()
        self.assertEqual(app._list_mode(), "mine")                                 # на «Скачать проект» не уходим
        os.makedirs(work_dir)                                                      # диск подключили: обычная пустая папка
        app._scan_projects()
        self.pump(root, lambda: not app._scan_gen or app.mine_empty_label.cget("text") == app.MINE_EMPTY_TEXT)
        self.assertEqual(app.mine_empty_btn.cget("text"), "Скачать проект с сервера")
        self.assertIn("пока нет проектов", app.mine_empty_label.cget("text"))

    def test_download_form_has_own_side_buttons(self):
        root, app, _ = self.make_app()
        app._mine_dirty = False
        bar = app.download_actions
        shown = lambda: [w for w in bar._items if w not in bar._hidden]   # noqa: E731
        self.assertEqual([w.cget("text") for w in bar._items],
                         ["Скачать", "Отмена", "Создать проект из этой папки…", "Удалить недокачанную папку"])
        self.assertEqual(shown(), [app.download_btn])
        self.assertEqual(str(app.download_btn.cget("style")), "Accent.TButton")
        app._set_create_target(("C:\\папка", "svn://srv", ["test", "папка"]))
        self.assertEqual(shown(), [app.download_btn, app.create_btn])
        app._set_create_target(None)
        self.assertEqual(shown(), [app.download_btn])
        self.assertEqual(app.refresh_btn.cget("text"), "⟳")              # «Обновить» больше не пишем
        for gone in ("list_btn", "tree_visible", "_toggle_tree", "tree_frame"):
            self.assertFalse(hasattr(app, gone), gone)

    def test_remembered_widths_are_per_subtab(self):
        from svnhelper.config import SettingsStore
        root, app, _ = self.make_app(visible=True)
        app._mine_dirty = False
        root.geometry("1300x700")
        app.select_list_mode("download")
        root.update()
        sash = app.mine_sash
        start = sash.winfo_rootx() + 2
        sash.event_generate("<ButtonPress-1>", x=2, y=5, rootx=start, rooty=0)
        sash.event_generate("<B1-Motion>", x=42, y=5, rootx=start + 40, rooty=0)
        sash.event_generate("<ButtonRelease-1>", x=42, y=5, rootx=start + 40, rooty=0)
        root.update()
        saved = SettingsStore(self.settings_path).settings
        self.assertGreater(saved.mine_col2_download_width, app.MINE_COL2_DOWNLOAD_WIDTH)
        self.assertEqual(saved.mine_col2_width, 0)                            # ширина «Мои проекты» не тронута
        app.select_list_mode("mine")
        self.assertEqual(app.mine_col2.width, app.px(app.MINE_COL2_WIDTH))
        app.select_list_mode("download")
        self.assertEqual(app.mine_col2.width, app.px(saved.mine_col2_download_width))

    def test_download_progress_fits_smallest_window(self):
        """Ход скачивания (полоса, строка хода, размер проекта) виден в окне 900x640 вместе со ссылкой из буфера,
        веткой и папкой: подсказка шага 3 уступает место ходу работы."""
        from svnhelper.gui.models import DownloadJob
        root, app, _ = self.make_app(visible=True)
        app._mine_dirty = False
        root.geometry(f"{app.px(900)}x{app.px(640)}")
        app.select_list_mode("download")
        app.url_var.set("svn://srv/test/projects/Очень длинное название проекта для проверки")
        app.job = DownloadJob(url="u", name="n", dest="d")
        app._update_buttons()
        app._show(app.progress, True)
        app.status_var.set("Скачано 120 из 400 (30%), осталось ~1 мин")
        app.info_var.set("Размер проекта: 1,2 ГБ, 400 файлов и папок.")
        app._show(app.line_row, True)                  # проект с ветками: строка «Что скачать» тоже на месте
        root.update()
        bottom = app.mine_col3.winfo_rooty() + app.mine_col3.winfo_height()    # колонка обрезает то, что ниже
        info = next(w for w in app.progress.master.winfo_children()
                    if "textvariable" in w.keys() and str(w.cget("textvariable")) == str(app.info_var))
        for widget in (app.progress, app.cancel_btn, app.download_btn, app.line_combo, info):
            self.assertTrue(widget.winfo_ismapped(), widget)
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom, widget)
        app.job = None
        app._update_buttons()

    def test_download_side_buttons_are_not_clipped_at_minimum_window(self):
        """«Создать проект из этой папки…» и «Удалить недокачанную папку» длиннее «Скачать проект»: колонка кнопок
        формы растёт под показанные кнопки, подписи не обрезаны в окне 900 px при 100, 125 и 150%."""
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale):
                root, app, _ = self.make_app(scale=scale, visible=True)
                app._mine_dirty = False
                root.geometry(f"{app.px(900)}x{app.px(640)}")
                app.select_list_mode("download")
                root.update()
                narrow = app.download_actions.winfo_width()
                app._set_create_target(("C:\\папка", "svn://srv", ["test", "папка"]))
                app.download_actions.set_visible(app.cleanup_btn, True)
                root.update()
                self.assertGreater(app.download_actions.winfo_width(), narrow)       # колонка выросла под длинные подписи
                right = root.winfo_rootx() + root.winfo_width()
                for button in (app.download_btn, app.create_btn, app.cleanup_btn):
                    self.assertTrue(button.winfo_ismapped(), button.cget("text"))
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth(), button.cget("text"))
                    self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), right, button.cget("text"))
                app._set_create_target(None)
                app.download_actions.set_visible(app.cleanup_btn, False)
                root.update()
                self.assertEqual(app.download_actions.winfo_width(), narrow)         # длинных нет - форме больше места
                root.withdraw()

    def test_search_labels_differ_between_subtabs(self):
        root, app, _ = self.make_app()
        labels = lambda frame: [str(w.cget("text")) for w in self.all_labels(frame)]   # noqa: E731
        self.assertIn("Найти проект:", labels(app.mine_find_row))
        self.assertIn("Найти на сервере:", labels(app.server_browser))

    @staticmethod
    def all_labels(widget):
        from tkinter import ttk
        found = [widget] if isinstance(widget, ttk.Label) else []
        for child in widget.winfo_children():
            found.extend(ListModeTests.all_labels(child))
        return found

    def test_subtab_labels_tree_and_form_fit_at_every_scale(self):
        """Ярлыки «Мои проекты | Скачать проект» не обрезаны в колонке «Проекты», дерево, поля и кнопки формы
        видны целиком в самом маленьком окне (900x640) при 100, 125 и 150%."""
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale):
                root, app, _ = self.make_app(scale=scale, visible=True)
                app._mine_dirty = False
                root.geometry(f"{app.px(900)}x{app.px(640)}")
                root.update()
                for mode in ("mine", "download"):
                    app.select_list_mode(mode)
                    root.update()
                    tabs, y = app.list_tabs, app.px(10)
                    on_tab = [x for x in range(tabs.winfo_width()) if tabs.identify(x, y)]
                    self.assertEqual(tabs.index(f"@{on_tab[0]},{y}"), 0, mode)
                    self.assertEqual(tabs.index(f"@{on_tab[-1]},{y}"), 1, mode)
                    self.assertLess(on_tab[-1], tabs.winfo_width() - 2, f"{mode}: ярлыки не помещаются")
                right = root.winfo_rootx() + root.winfo_width()
                bottom = root.winfo_rooty() + root.winfo_height()
                for widget in (app.tree, app.search_entry, app.url_entry, app.dir_entry, app.browse_btn,
                               app.download_btn, app.refresh_btn):
                    self.assertTrue(widget.winfo_ismapped(), widget)
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right, widget)
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom, widget)
                for button in (app.download_btn, app.browse_btn, app.refresh_btn):     # подписи не обрезаны
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth(), button)
                root.withdraw()           # окно закроет addCleanup; следующий масштаб - в новом окне


def tmp_path():
    """Путь внутри системной временной папки (файл не создаётся)."""
    return os.path.join(tempfile.gettempdir(), "svnhelper_test_nonexistent")


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class LoginFlowTests(unittest.TestCase):
    """Две одновременные задачи получают ошибку входа: окно входа должно открыться один раз."""

    def tearDown(self):
        os.environ.pop("FAKE_SVN_MODE", None)

    def make_app(self, tmp, repos):
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        fake = [sys.executable, str(Path(__file__).resolve().parent / "fake_svn.py")]
        root = tk.Tk()
        root.withdraw()
        quiet_clipboard(root)
        app = App(root, Config(server="svn://x", repos=repos), SettingsStore(Path(tmp) / "s.json"),
                  SvnClient(fake), os.path.join(tmp, "log.txt"))
        return root, app

    def pump(self, root, cond, timeout=20):
        end = time.monotonic() + timeout
        while not cond():
            root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), end, "таймаут ожидания")

    def run_scenario(self, dialog_results):
        os.environ["FAKE_SVN_MODE"] = "auth"
        with tempfile.TemporaryDirectory() as tmp:
            root, app = self.make_app(tmp, ["a", "b"])
            try:
                shown = []

                def fake_ask(parent, username, text):
                    shown.append(text)
                    return dialog_results[len(shown) - 1]

                with mock.patch("tkinter.messagebox.showerror"),                         mock.patch("svnhelper.gui.login.LoginDialog") as dlg,                         mock.patch.object(root, "wait_window"):
                    def make(parent, username, text, **_kw):
                        shown.append(text)
                        obj = mock.Mock()
                        obj.result = dialog_results[len(shown) - 1]
                        return obj
                    dlg.side_effect = make
                    for iid in app.tree.get_children():
                        app._load_node(iid)
                    self.pump(root, lambda: all(n.state in ("loaded", "error") for n in app.nodes.values()))
                return app, shown
            finally:
                root.destroy()

    def test_single_dialog_for_parallel_failures(self):
        app, shown = self.run_scenario([("u", "good")])
        self.assertEqual(len(shown), 1)

    def test_wrong_password_three_attempts_then_gives_up(self):
        app, shown = self.run_scenario([("u", "bad")] * 3)
        self.assertEqual(len(shown), 3)
        self.assertIn("попытка 2 из 3", shown[1])

    def test_cancel_gives_up_all_waiters(self):
        app, shown = self.run_scenario([None])
        self.assertEqual(len(shown), 1)
        self.assertTrue(all(n.state == "error" for n in app.nodes.values()))


@unittest.skipUnless(HAS_DISPLAY and sys.platform == "win32", "нужны экран и Windows")
class SettingsFlowTests(unittest.TestCase):
    def test_shared_config_and_personal_settings(self):
        import json
        from svnhelper.config import Config, Settings, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        fake = [sys.executable, str(Path(__file__).resolve().parent / "fake_svn.py")]
        appdata = os.environ.get("APPDATA")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["APPDATA"] = tmp
            shared = Path(tmp) / "shared.json"
            shared.write_text(json.dumps({"server": "svn://shared", "repos": ["x", "y"],
                                          "filter": {"skip_junk": False}}), encoding="utf-8")
            store = SettingsStore(Path(tmp) / "s.json")
            store.settings.config_path = str(shared)
            root = tk.Tk()
            root.withdraw()
            quiet_clipboard(root)
            try:
                app = App(root, Config(), store, SvnClient(fake), os.path.join(tmp, "log.txt"))
                app.reload_shared_config(manual=True)
                with mock.patch("tkinter.messagebox.showinfo") as info:
                    end = time.monotonic() + 15
                    while info.call_count == 0 and time.monotonic() < end:
                        root.update()
                        time.sleep(0.02)
                    self.assertEqual(info.call_count, 1)
                self.assertEqual((app.config.server, app.config.repos), ("svn://shared", ["x", "y"]))
                self.assertFalse(app.config.filter.skip_junk)
                self.assertEqual(len(app.tree.get_children()), 2)

                values = {"config_path": str(shared), "username": "ivanov", "password": "secret",
                          "remember": True, "server": "svn://mine", "repos": "x, y",
                          "last_dir": "D:\Work", "svn_path": "", "skip_junk": True, "show_report": True}
                app._apply_settings(values)
                st = store.settings
                self.assertEqual((st.server, st.repos, st.skip_junk, st.show_report), ("svn://mine", [], True, None))
                self.assertTrue(st.password_enc)
                self.assertNotIn("secret", Path(store.path).read_text(encoding="utf-8"))
                self.assertEqual(app.client.credentials.password, "secret")
                self.assertEqual(app.config.server, "svn://mine")
            finally:
                root.destroy()
                if appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = appdata


@unittest.skipUnless(HAS_DISPLAY and shutil.which("svn") and shutil.which("svnadmin"), "нет экрана или svn")
class BranchesGuiTests(unittest.TestCase):
    """Ветки и версии: колонка «Линия», защита версии, «Что скачать», создание версии, переключение."""

    BRANCH = "задача 5"
    TAG = "2026-10-01"

    @classmethod
    def setUpClass(cls):
        from svnhelper import server
        from svnhelper.svn_core import SvnClient
        real.RealSvnTests.setUpClass.__func__(cls)
        src = Path(cls.tmp.name) / "br_src"
        src.mkdir()
        (src / "a.txt").write_text("a", encoding="utf-8")
        cls.project_segments = ["test", "projects", "br_project"]
        cls.project_url = server.build_url(cls.server, cls.project_segments)
        real.run("svn", "import", str(src), cls.project_url + "/trunk", "-m", "init")
        client = SvnClient("svn")
        client.copy_url(cls.project_url + "/trunk", server.build_url(cls.project_url, ["branches", cls.BRANCH]),
                        "Ветка для теста")
        client.copy_url(cls.project_url + "/trunk", server.build_url(cls.project_url, ["tags", cls.TAG]),
                        f"Версия {cls.TAG}")

    @classmethod
    def tearDownClass(cls):
        real.RealSvnTests.tearDownClass.__func__(cls)

    def pump(self, root, cond, timeout=20):
        end = time.monotonic() + timeout
        while not cond():
            root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), end, "таймаут ожидания")

    def line_url(self, *segments):
        from svnhelper import server
        return server.build_url(self.project_url, list(segments))

    def make_app(self, name, copies=()):
        """Окно с настоящим svn; copies — [(папка, [сегменты линии])]: копии проекта br_project в папке name."""
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        base = os.path.join(self.tmp.name, name)
        os.makedirs(base)
        for folder, segments in copies:
            real.run("svn", "checkout", self.line_url(*segments), os.path.join(base, folder))
        root = tk.Tk()
        root.withdraw()
        quiet_clipboard(root)
        store = SettingsStore(Path(self.tmp.name) / f"{name}.json")
        app = App(root, Config(server=self.server, repos=["test"], work_dir=base), store, SvnClient("svn"),
                  os.path.join(self.tmp.name, "log.txt"))
        app.tortoise_exe = "C:\\fake\\TortoiseProc.exe"
        app._apply_tortoise()
        app.dir_var.set(base)
        if copies:
            app._scan_projects()
            self.pump(root, lambda: len(app.mine) >= len(copies))
            self.pump(root, lambda: not app._checking and not any(i.checking for i in app.mine.values()))
        return root, app, base, store

    @staticmethod
    def combo_values(app):
        return list(app.line_combo.tk.splitlist(app.line_combo.cget("values")))

    def by_name(self, app):
        return {i.name: i for i in app.mine.values()}

    def select(self, root, app, item):
        """Выбирает папку в «Мои проекты» (строка таблицы)."""
        app.mine_tree.selection_set(app._iid_of(item))
        root.update()

    def open_lines(self, root, app, item, line=None):
        """Вкладка «Ветки и версии» на папке item: ждёт список линий; line (если задана) выбирается в таблице."""
        self.select(root, app, item)
        app.open_project_tab("lines")
        self.pump(root, lambda: app._line_rows and not app._lines_view_loading)
        if line is not None:
            self.pick_row(root, app, line)

    def pick_row(self, root, app, line):
        iid = next(i for i, (ln, _e) in app._line_rows.items() if ln == line)
        app.lines_tree.selection_set(iid)
        root.update()
        return iid

    def test_line_column_and_tag_protection(self):
        from svnhelper import server
        root, app, _base, _store = self.make_app("cols", [("t", ["trunk"]), ("b", ["branches", self.BRANCH]),
                                                          ("v", ["tags", self.TAG])])
        try:
            items = self.by_name(app)
            self.assertEqual(items["t"].line, server.TRUNK_LINE)
            self.assertEqual(items["b"].line, server.Line("branch", self.BRANCH))
            self.assertEqual(items["v"].line, server.Line("tag", self.TAG))
            self.assertEqual(items["b"].project_url, self.project_url)
            self.assertEqual(items["b"].line_url, self.line_url("branches", self.BRANCH))
            # колонка «Ветка» стоит сразу после «Проект»; колонки «№» в таблице нет
            self.assertEqual(list(app.mine_tree["columns"])[:2], ["name", "line"])
            self.assertEqual(app.mine_tree.heading("line", "text"), "Ветка")
            self.assertNotIn("rev", app.mine_tree["columns"])
            cells = {n: app.mine_tree.set(app._iid_of(i), "line") for n, i in items.items()}
            self.assertEqual(cells, {"t": "Основная", "b": self.BRANCH, "v": f"🔒 Версия {self.TAG}"})

            self.select(root, app, items["t"])
            self.assertEqual(str(app._commit_panel.save_button.cget("state")), "normal")
            self.assertEqual(str(app.project_panel.lock_btn.cget("state")), "normal")
            self.select(root, app, items["v"])
            self.assertEqual(str(app._commit_panel.save_button.cget("state")), "disabled")
            self.assertEqual(str(app.project_panel.lock_btn.cget("state")), "disabled")
            self.assertEqual(str(app.update_btn.cget("state")), "normal")

            # «Ещё ▾»: сохранение через TortoiseSVN и подменю «Для опытных»
            menu = app.more_menu
            labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) != "separator"]
            self.assertEqual(labels, ["Сохранить через TortoiseSVN…", "Отправить на сервер…", "Для опытных"])
            expert = app.expert_menu
            labels = [expert.entrycget(i, "label") for i in range(expert.index("end") + 1)]
            self.assertEqual(labels, ["История в TortoiseSVN", "Отменить мои правки (TortoiseSVN)…", "Разобрать конфликт в TortoiseSVN",
                                      "Починить папку"])

            # шапка версии: ветка и «только для просмотра»; истории (Создал…) в шапке нет
            text = app.project_panel.header_text()
            self.assertIn(f"Ветка: 🔒 Версия {self.TAG}", text)
            self.assertIn("Для правок переключитесь на Основную или ветку", text)
            self.assertNotIn("Адрес на сервере", text)
            self.assertNotIn("Создал", text)
            self.assertNotIn("Работали", text)
            self.assertIn("сохранение № ", text)

            # правка внутри версии: подсказка «создайте ветку», строка не зелёная
            Path(items["v"].path, "a.txt").write_text("правка", encoding="utf-8")
            app._check_status([items["v"]])
            self.pump(root, lambda: not items["v"].checking and not app._checking)
            values, tag = app._row_values(items["v"])
            self.assertEqual(tag, "new")
            self.assertIn("создайте ветку", values[3])
            self.assertIn("Правки в версию сохранить нельзя", app.project_panel.header_text())
            self.assertEqual(str(app._commit_panel.save_button.cget("state")), "disabled")   # в версию не сохраняем

            # вкладка «Ветки»: из версии версию не делаем и перенос правок недоступен, а ветку создать можно
            self.open_lines(root, app, items["v"])
            self.assertEqual(str(app.new_tag_btn.cget("state")), "disabled")
            self.assertEqual(str(app.merge_btn.cget("state")), "disabled")
            self.assertEqual(str(app.new_branch_btn.cget("state")), "normal")
        finally:
            root.destroy()

    def test_lines_tab_folders_rows_and_buttons(self):
        from svnhelper import server
        root, app, _base, _store = self.make_app("lines_tab", [
            ("br_project", ["trunk"]), (f"br_project__{self.BRANCH}", ["branches", self.BRANCH]),
            (f"br_project__{self.TAG}", ["tags", self.TAG])])
        try:
            items = self.by_name(app)
            branch = server.Line("branch", self.BRANCH)
            tag = server.Line("tag", self.TAG)
            self.open_lines(root, app, items[f"br_project__{self.BRANCH}"])
            # своего выбора папки нет: вкладка показывает проект, выделенный в колонке 2
            self.assertFalse(hasattr(app, "lines_folder_combo"))
            self.assertEqual(app._lines_folder_item(), items[f"br_project__{self.BRANCH}"])
            self.assertEqual([button.cget("text") for button in app.lines_buttons._items],
                             ["Перейти", "Отличия", "Перенести правки…", "Новая ветка…", "Скачать отдельно",
                              "Зафиксировать версию…"])
            # таблица: Основная, потом ветки, потом версии; в строке, где сейчас папка, «вы здесь»
            self.assertEqual([app.lines_tree.heading(c, "text") for c in app.lines_tree["columns"]],
                             ["Название", "Создал", "Создана", "Последнее изменение", "Описание", "", "Путь на сервере"])
            rows = [(line, app.lines_tree.item(iid, "values"), app.lines_tree.item(iid, "tags"))
                    for iid, (line, _e) in app._line_rows.items()]
            kinds = [line.kind for line, _v, _t in rows]
            self.assertEqual(kinds[0], "trunk")
            self.assertEqual(kinds, sorted(kinds, key=["trunk", "branch", "tag"].index))   # порядок групп
            by_line = {line: (values, tags) for line, values, tags in rows}
            self.assertEqual(by_line[server.TRUNK_LINE][0][0], "Основная")
            self.assertEqual(by_line[branch][0][0], f"Ветка «{self.BRANCH}»")
            self.assertIn("current", by_line[branch][1])
            self.assertEqual(by_line[tag][0][0], f"🔒 Версия «{self.TAG}»")
            self.assertIn("frozen", by_line[tag][1])
            self.assertEqual(sum(values[5] == "✔" for _l, values, _t in rows), 1)
            # по умолчанию выбрана строка «здесь»: переключаться некуда, кнопка неактивна
            self.assertEqual(app._selected_line()[0], branch)
            self.assertEqual(str(app.switch_here_btn.cget("state")), "disabled")
            self.assertIn("уже здесь", app.lines_hint.cget("text"))
            # выбрали другую линию: кнопка включается, подсказка про переключение
            self.pick_row(root, app, server.TRUNK_LINE)
            self.assertEqual(str(app.switch_here_btn.cget("state")), "normal")
            self.assertIn(f"Переключение заменит файлы в папке «{items[f'br_project__{self.BRANCH}'].name}» на Основную.",
                          app.lines_hint.cget("text"))
            self.pick_row(root, app, branch)
            self.assertEqual(str(app.switch_here_btn.cget("state")), "disabled")
            # «Создал / Создана» заполняются лениво: сначала «…», потом настоящие значения
            iid = next(i for i, (ln, _e) in app._line_rows.items() if ln == branch)
            self.pump(root, lambda: "…" not in app.lines_tree.item(iid, "values")[1:3])
            self.assertRegex(app.lines_tree.item(iid, "values")[2], r"^\d\d\.\d\d\.\d{4}$")
            # карточка: название и строки истории; у ветки «Заняты файлы» подгружаются по требованию
            self.assertTrue(app.line_title.cget("text").startswith(f"Ветка «{self.BRANCH}»"))
            self.pump(root, lambda: self.BRANCH in app.line_title.cget("text") and any(
                str(lbl.cget("text")).startswith("Создал ") for lbl in app.line_card_rows))
            self.pick_row(root, app, tag)
            shown = [str(lbl.cget("text")) for lbl in app.line_card_rows if lbl.winfo_manager()]
            self.assertIn("Только для просмотра.", shown)
            self.assertEqual(str(app.switch_here_btn.cget("state")), "normal")
            self.assertEqual(str(app.download_other_btn.cget("state")), "normal")
        finally:
            root.destroy()

    def test_lines_history_is_cached_and_loaded_lazily(self):
        root, app, _base, _store = self.make_app("lines_lazy", [("br_project", ["trunk"])])
        try:
            item = self.by_name(app)["br_project"]
            calls = []
            real_history = app.client.branch_history
            with mock.patch.object(app.client, "branch_history",
                                   side_effect=lambda *a, **k: calls.append(a[0]) or real_history(*a, **k)):
                self.open_lines(root, app, item)
                self.pump(root, lambda: app._line_history and not app._history_loading)
                first = len(calls)
                self.assertGreaterEqual(first, 1)
                self.assertEqual(len(set(calls)), len(calls))           # по каждому адресу один запрос
                app._request_line_histories()                           # повторно сведения не запрашиваются
                root.update()
                self.assertEqual(len(calls), first)
                app._on_lines_refresh()                                 # «⟳ Обновить» читает всё заново
                self.pump(root, lambda: app._line_rows and not app._lines_view_loading)
                self.pump(root, lambda: len(calls) > first and not app._history_loading)
        finally:
            root.destroy()

    def test_lines_tab_shows_diff_from_trunk(self):
        from svnhelper import server
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app, _base, _store = self.make_app("lines_diff", [(f"br_project__{self.BRANCH}", ["branches", self.BRANCH])])
        try:
            item = self.by_name(app)[f"br_project__{self.BRANCH}"]
            branch = server.Line("branch", self.BRANCH)
            self.open_lines(root, app, item, server.TRUNK_LINE)
            self.assertEqual(str(app.diff_btn.cget("state")), "disabled")       # Основную с самой собой не сравнивают
            self.assertEqual(app.diff_box.winfo_manager(), "")
            self.pick_row(root, app, branch)
            self.assertEqual(str(app.diff_btn.cget("state")), "normal")

            changes = [("б.txt", "added"), ("а.txt", "modified"), ("в.txt", "deleted")]
            with mock.patch.object(app.client, "diff_summary", return_value=changes) as fake:
                app._on_show_diff()
                self.pump(root, lambda: app.diff_tree.get_children())
            self.assertEqual(fake.call_args[0][0], item.line_url_of(server.TRUNK_LINE))
            self.assertEqual(fake.call_args[0][1], item.line_url_of(branch))
            self.assertEqual([app.diff_tree.item(i, "values") for i in app.diff_tree.get_children()],
                             [("изменён", "а.txt"), ("новый", "б.txt"), ("удалён", "в.txt")])
            self.assertIn("3 файла", app.diff_label.cget("text"))
            # другая строка списка: отличия прежней ветки прячутся
            self.pick_row(root, app, server.TRUNK_LINE)
            self.assertEqual(app.diff_tree.get_children(), ())
            self.assertEqual(app.diff_box.winfo_manager(), "")

            # нет связи: без окон с ошибкой, спокойная подпись
            self.pick_row(root, app, branch)
            with mock.patch.object(app.client, "diff_summary",
                                   side_effect=SvnError(ErrorKind.NO_CONNECTION, "нет связи")), \
                    mock.patch.object(app, "show_error") as show_error, \
                    mock.patch("tkinter.messagebox.showerror") as showerror:
                app._on_show_diff()
                self.pump(root, lambda: not app._diff_loading)
                show_error.assert_not_called()
                showerror.assert_not_called()
            self.assertIn("Нет связи", app.diff_label.cget("text"))
            self.assertEqual(app.diff_tree.get_children(), ())

            # отличий нет
            with mock.patch.object(app.client, "diff_summary", return_value=[]):
                app._on_show_diff()
                self.pump(root, lambda: not app._diff_loading)
            self.assertEqual(app.diff_label.cget("text"), "Отличий от Основной нет.")

            # настоящий svn: запрос проходит без ошибок (состав зависит от других тестов общего хранилища)
            app._on_show_diff()
            self.pump(root, lambda: not app._diff_loading)
            self.assertRegex(app.diff_label.cget("text"), "^Отличи")
        finally:
            root.destroy()

    def test_window_tabs_order_and_no_separate_lines_tab(self):
        """U4.3: вкладок окна нет; «Мои проекты | Скачать проект» - подвкладки колонки «Проекты», первая - свои."""
        root, app, _base, _store = self.make_app("tab_order")
        try:
            self.assertFalse(hasattr(app, "notebook"))
            self.assertEqual(app.mine_tab.winfo_parent(), str(root))             # рабочая область прямо в окне
            titles = [app.list_tabs.tab(t, "text") for t in app.list_tabs.tabs()]
            self.assertEqual(titles, ["Мои проекты", "Скачать проект"])      # вкладки «Ветки и версии» нет
            self.assertEqual(app.list_tabs.index(app.list_frames["mine"]), 0)
            self.assertEqual(app._list_mode(), "mine")
            self.assertFalse(hasattr(app, "lines_folder_combo"))
            self.assertFalse(hasattr(app, "_open_lines_for_selected"))
        finally:
            root.destroy()

    def test_first_run_without_projects_opens_download_tab(self):
        root, app, _base, _store = self.make_app("first_run")
        try:
            self.assertEqual(app._list_mode(), "mine")                        # приложение начинает с «Мои проекты»
            empty = os.path.join(self.tmp.name, "first_run_empty")
            os.makedirs(empty)
            app.dir_var.set(empty)
            app.mine_col2.collapse()                                          # «Проекты» свёрнуты: развернутся сами
            app._start_tab_chosen = False                                     # как будто это первый поиск после запуска
            app._scan_projects()
            self.pump(root, lambda: app._mine_scanned and not app.mine)       # проектов в папке нет
            self.assertEqual(app._list_mode(), "download")
            self.assertFalse(app.mine_col2.collapsed or app.mine_col3.collapsed)
            self.assertEqual(app.download_form.winfo_manager(), "grid")      # справа форма скачивания
            app.select_list_mode("mine")
            self.assertEqual(app.mine_empty.winfo_manager(), "grid")         # пустое состояние с путём к скачиванию
            app._scan_projects()                                              # повторный поиск подвкладку не переключает
            self.pump(root, lambda: app._mine_scanned and not app.mine)
            root.update()
            self.assertEqual(app._list_mode(), "mine")
        finally:
            root.destroy()

    def test_lines_tab_follows_project_selection_and_has_no_stub(self):
        from svnhelper import messages
        root, app, _base, _store = self.make_app("lines_empty")
        try:
            app.dir_var.set(os.path.join(self.tmp.name, "lines_empty"))
            app._scan_projects()
            self.pump(root, lambda: app._mine_scanned and not app.mine)
            app.open_project_tab("lines")
            root.update()
            self.assertEqual(app.lines_status.cget("text"), messages.LINES_PICK_PROJECT)    # проекта нет
            self.assertEqual(app.lines_body.winfo_manager(), "")
            self.assertEqual(app.lines_tree.get_children(), ())
        finally:
            root.destroy()

    def test_download_separately_opens_first_tab_with_chosen_branch(self):
        from svnhelper import server
        root, app, _base, _store = self.make_app("sep", [("br_project", ["trunk"])])
        try:
            item = self.by_name(app)["br_project"]
            branch = server.Line("branch", self.BRANCH)
            self.open_lines(root, app, item, branch)
            self.assertEqual(app.project_tabs.select(), str(app.project_tab_frames["lines"]))
            app._on_download_separately()
            root.update()
            self.assertEqual(app._list_mode(), "download")
            self.assertEqual(app.download_form.winfo_manager(), "grid")
            self.assertEqual(app.url_var.get(), f"{self.project_url}/branches/{self.BRANCH}")
            self.assertEqual(app.line, branch)
            self.assertTrue(app.line_combo.get().startswith(f"Ветка «{self.BRANCH}»"))
            self.assertEqual(app.line_row.winfo_manager(), "grid")      # «Что скачать» видно: у проекта есть ветки
            self.assertIn(f"br_project__{self.BRANCH}", app.dest_var.get())
        finally:
            root.destroy()

    def test_what_to_download_hidden_for_project_without_lines(self):
        root, app, _base, _store = self.make_app("nolines")
        try:
            app.url_var.set(self.server + "/test/projects/upd_project")
            self.assertEqual(app.line_row.winfo_manager(), "")
            self.pump(root, lambda: app.project.display in app._lines_cache)
            self.assertEqual(app.line_row.winfo_manager(), "")           # веток и версий нет: выбирать нечего
            self.assertEqual(self.combo_values(app), ["Основная"])
            self.assertEqual(app.line_info.winfo_manager(), "")
            app.url_var.set(self.project_url)                            # а у этого проекта они есть
            self.pump(root, lambda: app.project.display in app._lines_cache)
            self.assertEqual(app.line_row.winfo_manager(), "grid")
        finally:
            root.destroy()

    def test_buttons_fit_into_window_on_every_tab(self):
        root, app, _base, _store = self.make_app("fit_lines", [("br_project", ["trunk"])])
        try:
            root.deiconify()
            root.geometry(f"{app.px(780)}x{app.px(640)}")                # самое узкое окно
            app.select_list_mode("mine")
            item = self.by_name(app)["br_project"]
            self.select(root, app, item)
            root.update()
            right = root.winfo_rootx() + root.winfo_width()
            for widget in (app.update_btn, app.more_btn, app.new_project_btn):
                self.assertTrue(widget.winfo_ismapped(), widget)
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right, widget)
            app.open_project_tab("locks")                     # «Занять файл…» и «Освободить…» справа, на «Занятых»
            root.update()
            self.assertEqual(len(app.project_panel.side_buttons()), 2)
            for widget in app.project_panel.side_buttons():
                self.assertTrue(widget.winfo_ismapped(), widget)
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right, widget)
            self.assertTrue(app.mine_tree.winfo_ismapped())     # столбцы шире окна листаются полосой прокрутки
            self.open_lines(root, app, item)
            root.update()
            for widget in (app.switch_here_btn, app.download_other_btn, app.new_branch_btn, app.new_tag_btn,
                           app.merge_btn):
                self.assertTrue(widget.winfo_ismapped(), widget)
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right, widget)
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                     root.winfo_rooty() + root.winfo_height(), widget)
            # «Что скачать» помещается в форму скачивания: шаги 2 и 3 не обрезаны
            app.select_list_mode("download")
            app.url_var.set(self.project_url)
            self.pump(root, lambda: app.line_combo.winfo_ismapped())
            self.assertLessEqual(app.download_btn.winfo_rooty() + app.download_btn.winfo_height(),
                                 root.winfo_rooty() + root.winfo_height())
        finally:
            root.destroy()

    def test_project_column_hides_branch_suffix(self):
        root, app, _base, _store = self.make_app("pname", [
            ("br_project", ["trunk"]), (f"br_project__{self.BRANCH}", ["branches", self.BRANCH]),
            (f"br_project__{self.TAG}", ["tags", self.TAG]), ("br_project__чужая", ["trunk"]),
            ("мой чертёж", ["branches", self.BRANCH])])
        try:
            shown = sorted(app.mine_tree.set(iid, "name") for iid in app.mine)
            self.assertEqual(shown, sorted(["br_project", "br_project", "br_project", "br_project__чужая",
                                            "мой чертёж"]))
            width = lambda col: int(app.mine_tree.column(col, "width"))  # noqa: E731
            self.assertGreaterEqual(width("name"), app.px(85))
            self.assertGreaterEqual(width("line"), app.px(85))
            self.assertGreater(width("todo"), width("status"))
        finally:
            root.destroy()

    def test_switch_refused_with_unsaved_changes(self):
        from svnhelper import server
        root, app, _base, _store = self.make_app("sw_refuse", [("t", ["trunk"])])
        try:
            item = self.by_name(app)["t"]
            Path(item.path, "a.txt").write_text("не сохранено", encoding="utf-8")
            self.open_lines(root, app, item, server.Line("branch", self.BRANCH))
            self.assertEqual(str(app.switch_here_btn.cget("state")), "normal")
            with mock.patch.object(app.client, "switch") as switch, \
                    mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask, \
                    mock.patch("tkinter.messagebox.showwarning") as warn:
                app._on_switch_here()
                self.pump(root, lambda: ask.call_count >= 1 and not app._locking)
                switch.assert_not_called()
                self.assertIsNone(app.upd)
            self.assertIn("Сначала сохраните правки — их 1. Потом переключайтесь.", ask.call_args[0][1])
            self.assertIn("Перейти к сохранению сейчас?", ask.call_args[0][1])
            warn.assert_not_called()
        finally:
            root.destroy()

    def test_switch_to_branch_and_back(self):
        from svnhelper import server
        root, app, _base, _store = self.make_app("sw_ok", [("t", ["trunk"])])
        try:
            item = self.by_name(app)["t"]
            branch = server.Line("branch", self.BRANCH)
            self.open_lines(root, app, item, branch)
            with mock.patch("tkinter.messagebox.askyesno", return_value=True), \
                    mock.patch("tkinter.messagebox.showinfo") as info, \
                    mock.patch("tkinter.messagebox.showwarning") as warn:
                app._on_switch_here()
                self.assertEqual(str(app.switch_here_btn.cget("state")), "disabled")   # идёт подготовка
                self.pump(root, lambda: info.call_count >= 1 and app.upd is None)
                self.assertEqual(info.call_args[0][1].splitlines()[0], f"Папка переключена: Ветка «{self.BRANCH}».")
                self.assertEqual(info.call_args[0][0], "Переключение завершено")
                self.pump(root, lambda: not app._checking and not item.checking and item.status is not None)
                warn.assert_not_called()
            self.assertEqual(item.line, branch)
            self.assertEqual(app.mine_tree.set(app._iid_of(item), "line"), self.BRANCH)
            self.assertEqual(app.client.info(item.path).url, self.line_url("branches", self.BRANCH))
            # на вкладке «Ветки» «вы здесь» переехало на ветку, список перечитан
            self.pump(root, lambda: app._line_rows and not app._lines_view_loading and any(
                app.lines_tree.item(i, "values")[5] == "✔" and ln == branch
                for i, (ln, _e) in app._line_rows.items()))
            # другая ветка не из этого проекта: понятный текст, копия остаётся на месте
            from svnhelper.svn_core import ErrorKind, SvnError
            with mock.patch.object(app.client, "switch", side_effect=SvnError(ErrorKind.UNRELATED, "E195012")), \
                    mock.patch("svnhelper.gui.App.show_error") as err, \
                    mock.patch("tkinter.messagebox.askyesno", return_value=True):
                app._begin_switch(item, server.TRUNK_LINE, app.client.status(item.path))
                self.pump(root, lambda: err.call_count >= 1)
            self.assertEqual(err.call_args[0][1], "Эта ветка не из этого проекта, переключиться на неё нельзя.")
            # на версию переключаемся без лишних вопросов: подсказка есть в самой вкладке
            self.pump(root, lambda: not app._checking and not item.checking)
            with mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask, \
                    mock.patch("tkinter.messagebox.showinfo") as info:
                app._begin_switch(item, server.Line("tag", self.TAG), app.client.status(item.path))
                self.pump(root, lambda: info.call_count >= 1 and app.upd is None)
                ask.assert_not_called()
            self.assertIn("только для просмотра", info.call_args[0][1])
        finally:
            root.destroy()

    def test_switch_unfinished_is_shown_and_current_line_selectable(self):
        from svnhelper import server
        root, app, _base, _store = self.make_app("sw_half", [("t", ["trunk"])])
        try:
            item = self.by_name(app)["t"]
            part = os.path.join(item.path, "a.txt")
            real.run("svn", "switch", self.line_url("branches", self.BRANCH, "a.txt"), part)   # переключён один файл
            app._check_status([item])
            self.pump(root, lambda: item.status is not None and not item.checking and not app._checking)
            self.assertEqual(item.status.switched, 1)
            text, tag, hint = app._status_for(item)
            self.assertEqual((text, tag), ("Переключение не закончено", "bad"))
            self.assertEqual(hint, "Вкладка «Ветки» → «Перейти»")
            self.select(root, app, item)
            self.assertIn("Переключение не закончено", app.project_panel.header_text())
            self.assertIn("Вкладка «Ветки» → «Перейти»", app.project_panel.header_text())

            # текущую линию выбрать можно: «Переключиться сюда» дочитывает переключение
            self.open_lines(root, app, item, server.TRUNK_LINE)
            self.assertEqual(str(app.switch_here_btn.cget("state")), "normal")
            self.assertIn("Переключение не закончено", app.lines_hint.cget("text"))
            with mock.patch("tkinter.messagebox.showinfo") as info, \
                    mock.patch("tkinter.messagebox.showwarning"):
                app._on_switch_here()
                self.pump(root, lambda: info.call_count >= 1 and app.upd is None)
            self.assertEqual(app.client.status(item.path).switched, 0)
            self.assertEqual(app.client.info(part).url, self.line_url("trunk", "a.txt"))
            self.assertEqual(item.line, server.TRUNK_LINE)
        finally:
            root.destroy()

    def test_switch_releases_own_locks_first(self):
        from svnhelper import server
        root, app, _base, _store = self.make_app("sw_unlock", [("t", ["trunk"])])
        try:
            item = self.by_name(app)["t"]
            app.client.lock(os.path.join(item.path, "a.txt"))
            self.assertEqual(len(app.client.locks_under(self.line_url("trunk"))), 1)
            self.open_lines(root, app, item, server.Line("branch", self.BRANCH))
            statuses = []
            original = app.mine_status.set
            with mock.patch.object(app.mine_status, "set", side_effect=lambda v: (statuses.append(v), original(v))[1]), \
                    mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask, \
                    mock.patch("tkinter.messagebox.showinfo") as info:
                app._on_switch_here()
                self.pump(root, lambda: info.call_count >= 1 and app.upd is None)
                ask.assert_not_called()                       # про занятые файлы не спрашиваем
            self.assertIn("Освобождаю занятые файлы (1) и переключаю…", statuses)
            self.assertEqual(app.client.locks_under(self.line_url("trunk")), [])
            self.assertEqual(app.client.info(item.path).url, self.line_url("branches", self.BRANCH))
        finally:
            root.destroy()

    def fake_new_line(self, result, seen):
        class FakeNewLine:
            def __init__(self, parent, kind, revision, included, default, taken, check_name=None, can_switch=True):
                seen.update(kind=kind, revision=revision, included=included, default=default, taken=list(taken),
                            can_switch=can_switch, check_name=check_name)
                self.result = result
        return FakeNewLine

    def test_make_tag_calls_copy_url_with_revision(self):
        from datetime import date
        root, app, _base, _store = self.make_app("mk_tag", [("t", ["trunk"])])
        try:
            item = self.by_name(app)["t"]
            self.open_lines(root, app, item)
            seen = {}
            loaded = app._lines_gen
            result = {"name": "2026-11-01", "comment": "Версия 2026-11-01", "switch": False}
            with mock.patch("svnhelper.gui.lines_tab.NewLineDialog", self.fake_new_line(result, seen)), \
                    mock.patch.object(root, "wait_window"), \
                    mock.patch.object(app.client, "copy_url") as copy, \
                    mock.patch("tkinter.messagebox.showinfo") as info:
                app._on_make_tag()
                self.pump(root, lambda: info.call_count >= 1)
            copy.assert_called_once_with(self.line_url("trunk"), self.line_url("tags", "2026-11-01"),
                                         "Версия 2026-11-01", item.revision)
            self.assertEqual(info.call_args[0][1], "Версия «2026-11-01» создана.")
            self.assertEqual(seen["kind"], "tag")
            self.assertEqual(seen["default"], date.today().isoformat())
            self.assertIn(self.TAG, seen["taken"])
            self.assertEqual(seen["included"], f"Войдёт то, что сохранено на сервере (сохранение № {item.revision}).")
            self.assertTrue(seen["can_switch"])
            self.assertGreater(app._lines_gen, loaded)                    # после создания список линий читается заново
            self.pump(root, lambda: app._line_rows and not app._lines_view_loading)

            # несохранённые правки: в окне сказано, что они не войдут; вопроса нет, версия создаётся
            Path(item.path, "a.txt").write_text("не сохранено", encoding="utf-8")
            with mock.patch("svnhelper.gui.lines_tab.NewLineDialog", self.fake_new_line(result, seen)), \
                    mock.patch.object(root, "wait_window"), \
                    mock.patch.object(app.client, "copy_url") as copy, \
                    mock.patch("tkinter.messagebox.askyesno") as ask, \
                    mock.patch("tkinter.messagebox.showinfo") as info:
                app._on_make_tag()
                self.pump(root, lambda: info.call_count >= 1)
                copy.assert_called_once()
                ask.assert_not_called()
            self.assertIn("Несохранённые правки (1 файл) не войдут.", seen["included"])
            self.assertFalse(seen["can_switch"])
        finally:
            root.destroy()

    def test_copy_uses_newest_revision_after_commit(self):
        """После Commit одного файла корень копии остаётся на старой ревизии: копировать надо с max_revision."""
        root, app, _base, _store = self.make_app("mk_max", [("t", ["trunk"])])
        try:
            item = self.by_name(app)["t"]
            path = Path(item.path, "a.txt")
            path.write_text("после коммита", encoding="utf-8")
            real.run("svn", "commit", str(path), "-m", "правка")
            newest = app.client.status(item.path, verbose=True).max_revision
            self.assertGreater(newest, item.revision)          # item.revision - старая
            self.open_lines(root, app, item)
            seen = {}
            result = {"name": "после-коммита", "comment": "Версия после коммита", "switch": False}
            with mock.patch("svnhelper.gui.lines_tab.NewLineDialog", self.fake_new_line(result, seen)), \
                    mock.patch.object(root, "wait_window"), \
                    mock.patch("tkinter.messagebox.showinfo") as info:
                app._on_make_tag()
                self.pump(root, lambda: info.call_count >= 1)
            self.assertEqual(seen["revision"], newest)
            self.assertIn(f"сохранение № {newest}", seen["included"])
            tagged = subprocess.run(["svn", "cat", self.line_url("tags", "после-коммита", "a.txt")],
                                    stdout=subprocess.PIPE, check=True).stdout.decode("utf-8")
            self.assertEqual(tagged, "после коммита")           # в версии новый файл, а не прежний
        finally:
            root.destroy()

    def test_make_branch_with_switch(self):
        from svnhelper import server
        root, app, _base, _store = self.make_app("mk_branch", [("t", ["trunk"])])
        try:
            item = self.by_name(app)["t"]
            self.open_lines(root, app, item)
            result = {"name": "эксперимент", "comment": "Ветка эксперимент", "switch": True}
            with mock.patch("svnhelper.gui.lines_tab.NewLineDialog", self.fake_new_line(result, {})), \
                    mock.patch.object(root, "wait_window"), \
                    mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask, \
                    mock.patch("tkinter.messagebox.showinfo") as info:
                app._on_make_branch()
                self.pump(root, lambda: info.call_count >= 1 and app.upd is None)
                self.pump(root, lambda: item.line == server.Line("branch", "эксперимент"))
                ask.assert_not_called()
            self.assertEqual(info.call_count, 1)                 # одно короткое окно
            self.assertEqual(info.call_args[0][0], "Ветка создана")
            self.assertEqual(info.call_args[0][1], "Ветка «эксперимент» создана. Папка переключена на неё.")
            self.assertEqual(app.client.info(item.path).url, self.line_url("branches", "эксперимент"))
            self.assertTrue(app.client.url_exists(self.line_url("branches", "эксперимент")))
        finally:
            root.destroy()

    def test_new_line_name_check_runs_in_background(self):
        root, app, _base, _store = self.make_app("mk_check", [("t", ["trunk"])])
        try:
            item = self.by_name(app)["t"]
            self.open_lines(root, app, item)
            seen = {}
            with mock.patch("svnhelper.gui.lines_tab.NewLineDialog", self.fake_new_line(None, seen)), \
                    mock.patch.object(root, "wait_window"):
                app._on_make_branch()
                self.pump(root, lambda: seen and not app._locking)
            answers = []
            seen["check_name"](self.BRANCH, answers.append)               # ветка уже есть на сервере
            self.pump(root, lambda: answers)
            seen["check_name"]("совсем-новая", answers.append)
            self.pump(root, lambda: len(answers) == 2)
            self.assertEqual(answers, [True, False])
        finally:
            root.destroy()

    def test_download_combo_selects_branch_and_tag(self):
        from svnhelper import server
        root, app, base, store = self.make_app("dl")
        try:
            app.url_var.set(self.project_url)
            self.assertEqual(self.combo_values(app), ["Основная"])   # список ещё не загружен
            self.assertEqual(app.line_row.winfo_manager(), "")       # «Что скачать» не показываем, пока выбирать не из чего
            self.pump(root, lambda: app.project.display in app._lines_cache)
            values = self.combo_values(app)
            self.assertEqual(values[0], "Основная")
            # в общем репозитории есть и ветки/версии других тестов: ищем свои по названию
            branch_at = next(i for i, v in enumerate(values) if f"«{self.BRANCH}»" in v)
            tag_at = next(i for i, v in enumerate(values) if f"«{self.TAG}»" in v)
            self.assertRegex(values[branch_at], rf"^Ветка «{self.BRANCH}» — \d\d\.\d\d(, .+)?$")
            self.assertRegex(values[tag_at], rf"^🔒 Версия «{self.TAG}» — \d\d\.\d\d$")

            app.line_combo.current(branch_at)
            app._on_line_selected()
            self.assertEqual(app.line, server.Line("branch", self.BRANCH))
            self.assertIn(f"br_project__{self.BRANCH}", app.dest_var.get())
            # сведения: кто создал и кто работал
            self.pump(root, lambda: "Создал " in app.line_info.cget("text"))
            info_text = app.line_info.cget("text")
            self.assertRegex(info_text, r"^Создал \S+ \d\d\.\d\d( · последнее изменение \S+ \d\d\.\d\d)?$")
            self.assertEqual(info_text.count("\n"), 0)               # одна короткая строка, без списка авторов
            self.assertNotIn("Работали", info_text)
            self.assertNotIn("Заняты", info_text)
            asked = []
            with mock.patch("tkinter.messagebox.askyesno",
                            side_effect=lambda t, m, **k: asked.append(m) or False), \
                    mock.patch("tkinter.messagebox.showerror") as err:
                app._on_download()
                self.pump(root, lambda: app.job is None)
                err.assert_not_called()
            dest = os.path.join(base, f"br_project__{self.BRANCH}")
            self.assertTrue(os.path.isfile(os.path.join(dest, "a.txt")))
            self.assertEqual(app.client.info(dest).url, self.line_url("branches", self.BRANCH))
            self.assertEqual(asked, [])                               # окна «Готово» нет, результат в карточке
            self.assertIn("«Занять файл…»", app._action_notes[os.path.normcase(dest)])
            self.assertTrue(store.settings.recent[0]["url"].endswith(f"/branches/{self.BRANCH}"))

            # индикатор «уже скачано» учитывает выбранную ветку
            app._update_dest_label()
            self.assertEqual(app.dest_var.get().split(":")[0], "Уже скачано")
            self.assertIn("Свежее — на вкладке «Мои проекты».", app.dest_var.get())
            app.line_combo.current(0)
            app._on_line_selected()
            self.assertIn("будет сохранён", app.dest_var.get())

            # версия: одна подсказка вместо вопроса, скачивание без подтверждения
            app.line_combo.current(tag_at)
            app._on_line_selected()
            self.assertEqual(app.line_info.cget("text"), "Только для просмотра: правки в неё сохранить нельзя.")
            asked.clear()
            with mock.patch("tkinter.messagebox.askyesno",
                            side_effect=lambda t, m, **k: asked.append(m) or True), \
                    mock.patch("os.startfile", create=True):
                app._on_download()
                self.pump(root, lambda: app.job is None)
            self.assertEqual(asked, [])                               # ни вопроса, ни окна «Готово»
            tag_note = app._action_notes[os.path.normcase(os.path.join(base, f"br_project__{self.TAG}"))]
            self.assertNotIn("«Занять файл…»", tag_note)
            self.assertIn("сохранить нельзя", tag_note)
            self.assertTrue(os.path.isdir(os.path.join(base, f"br_project__{self.TAG}", ".svn")))

            # ссылка на ветку выбирает её сразу
            app.url_var.set(f"{self.project_url}/branches/{self.BRANCH}")
            self.assertEqual(app.line, server.Line("branch", self.BRANCH))
            self.assertTrue(app.line_combo.get().startswith(f"Ветка «{self.BRANCH}»"))
        finally:
            root.destroy()

    def test_download_never_loads_locks(self):
        root, app, _base, _store = self.make_app("dl_locks")
        try:
            app.url_var.set(self.project_url)
            self.pump(root, lambda: app.project.display in app._lines_cache)
            calls, histories = [], []
            real_history = app.client.branch_history
            with mock.patch.object(app.client, "locks_under", side_effect=lambda *a, **k: calls.append(a)), \
                    mock.patch.object(app.client, "branch_history",
                                      side_effect=lambda *a, **k: histories.append(a) or real_history(*a, **k)):
                values = self.combo_values(app)
                tag_at = next(i for i, v in enumerate(values) if v.startswith("🔒"))
                branch_at = next(i for i, v in enumerate(values) if v.startswith("Ветка"))
                for index in (0, tag_at):                               # основная, версия: сервер не спрашиваем
                    app.line_combo.current(index)
                    app._on_line_selected()
                    root.update()
                self.assertEqual(histories, [])
                app.line_combo.current(branch_at)                       # ветка: только «кто создал и менял»
                app._on_line_selected()
                self.pump(root, lambda: "Загружаю" not in app.line_info.cget("text"))
                self.assertEqual(len(histories), 1)
                self.assertEqual(calls, [])                             # занятые файлы здесь больше не запрашиваются
        finally:
            root.destroy()

    def test_mine_selection_does_not_ask_server_for_history_or_locks(self):
        root, app, _base, _store = self.make_app("mine_locks", [("b", ["branches", self.BRANCH])])
        try:
            item = self.by_name(app)["b"]
            with mock.patch.object(app.client, "locks_under") as locks, \
                    mock.patch.object(app.client, "branch_history") as history:
                self.select(root, app, item)
                for _ in range(20):
                    root.update()
                    time.sleep(0.02)
                locks.assert_not_called()
                history.assert_not_called()
        finally:
            root.destroy()

    def test_stale_line_info_is_dropped(self):
        from svnhelper import server
        from svnhelper.svn_core import BranchHistory
        root, app, _base, _store = self.make_app("stale_info")
        try:
            app.url_var.set(self.project_url)
            app._load_line_info()
            stale = app._info_gen
            app.url_var.set("")                 # выбор сменился: прежний запрос недействителен
            app._on_line_info(stale, server.TRUNK_LINE, BranchHistory(created_by="petrov"))
            self.assertEqual(app.line_info.cget("text"), "")
        finally:
            root.destroy()


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class BranchTextGuiTests(unittest.TestCase):
    """Ветки и версии без настоящего сервера: подробности, дерево, занятые файлы, занятый файл (BUSY)."""

    make_app = Stage3GuiTests.make_app
    pump = Stage3GuiTests.pump

    def make_item(self, app, tmp, line, **kwargs):
        from svnhelper import server
        from svnhelper.gui.models import MineItem
        folder = os.path.join(tmp, "work")
        os.makedirs(folder, exist_ok=True)
        item = MineItem(folder, "proj", "svn", url="svn://srv/test/proj/trunk", revision=5, checked=True,
                        project_prefix="svn://srv", project_segments=("test", "proj"), line=line,
                        line_url="svn://srv/test/proj/trunk", **kwargs)
        item.line_url = item.line_url_of(line or server.TRUNK_LINE)
        app.mine[app._insert_mine_row(item)] = item
        return item

    def test_header_is_short_and_without_repeats(self):
        from svnhelper import server
        from svnhelper.svn_core import WcStatus
        root, app, tmp = self.make_app()
        item = self.make_item(app, tmp, server.Line("branch", "задача"),
                              status=WcStatus(modified=2, incoming=1, unversioned=3))
        title, status, tag, branch, todo, note = app._header_fields(item)
        self.assertEqual((title, status, branch), ("proj", "Есть новое на сервере и ваши правки",
                                                   "задача · сохранение № 5"))
        self.assertTrue(todo.startswith("Сначала «Получить новое»"))
        self.assertEqual(note, "")                                            # истории ветки в шапке нет

        # шапка в окне: название, ● состояние, «Ветка: …», что сделать - по одной строке, без повторов
        app.mine_tree.selection_set(app._iid_of(item))
        root.update()
        lines = app.project_panel.header_text().split("\n")
        self.assertEqual(lines[:3], ["proj", "● Есть новое на сервере и ваши правки", "Ветка: задача · сохранение № 5"])
        self.assertTrue(lines[3].startswith("Что сделать: "))
        self.assertEqual(len(set(lines)), len(lines))

        item.status = WcStatus()
        item.line = server.TRUNK_LINE
        self.assertEqual(app._header_fields(item)[1:4], ("Актуален", "ok", "Основная · сохранение № 5"))
        item.line = server.Line("tag", "2026-10-01")                           # версия
        self.assertEqual(app._header_fields(item)[3], "🔒 Версия 2026-10-01 · сохранение № 5")
        item.line = None                                                      # ветку определить не удалось
        self.assertEqual(app._header_fields(item)[3], "не определена · сохранение № 5")
        app._action_notes[os.path.normcase(item.path)] = "Сохранено на сервер: 3 файла"
        self.assertEqual(app._header_fields(item)[5], "Сохранено на сервер: 3 файла")

    def test_project_name_column(self):
        from svnhelper import server
        from svnhelper.gui.models import MineItem
        branch, tag = server.Line("branch", "задача"), server.Line("tag", "v1")
        make = lambda name, line: MineItem("p", name, "svn", line=line, project_segments=("a", "proj"))  # noqa: E731
        self.assertEqual(make("proj", server.TRUNK_LINE).display_name, "proj")
        self.assertEqual(make("proj__задача", branch).display_name, "proj")
        self.assertEqual(make("proj__v1", tag).display_name, "proj")
        self.assertEqual(make("proj__другая", branch).display_name, "proj__другая")
        self.assertEqual(make("proj__v1", server.TRUNK_LINE).display_name, "proj__v1")   # суффикс не от ветки
        self.assertEqual(make("мои чертежи", branch).display_name, "мои чертежи")
        self.assertEqual(MineItem("p", "x", "plain").display_name, "x")

    def test_project_from_tree_in_folder_named_tags(self):
        from svnhelper.gui.models import Node
        root, app, _tmp = self.make_app()
        segments = ["test", "tags", "proj"]
        iid = app.tree.insert("", "end", text="proj")
        app.nodes[iid] = Node(segments, state="loaded", is_project=True)
        app.tree.selection_set(iid)
        root.update()
        self.assertEqual(app.project.segments, tuple(segments))
        self.assertEqual(app.project.line.kind, "trunk")
        self.assertEqual(app.project.trunk_url, "svn://srv/test/tags/proj/trunk")
        self.assertEqual(app.line.kind, "trunk")
        # а ссылка, набранная руками, читается как версия: так устроен разбор строки
        app.url_var.set("svn://srv/test/tags/proj/")
        self.assertEqual(app.project.line.kind, "tag")

    def test_busy_file_runs_cleanup_then_explains(self):
        from svnhelper import server
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app, tmp = self.make_app()
        item = self.make_item(app, tmp, server.TRUNK_LINE)
        for switching in (False, True):
            with mock.patch.object(app.client, "update", side_effect=SvnError(ErrorKind.BUSY, "E720")), \
                    mock.patch.object(app.client, "switch", side_effect=SvnError(ErrorKind.BUSY, "E720")), \
                    mock.patch.object(app.client, "cleanup") as cleanup, \
                    mock.patch.object(app, "_reprobe"), \
                    mock.patch("svnhelper.gui.App.show_error") as err:
                if switching:
                    from svnhelper.gui.models import UpdateJob
                    app.upd = UpdateJob(item, client=app.client, url=item.line_url_of(server.Line("branch", "b")),
                                        target=server.Line("branch", "b"))
                    app._start_update()
                else:
                    app._begin_update(item)
                self.pump(root, lambda: err.call_count >= 1)
                cleanup.assert_called_once_with(item.path)
            self.assertEqual(err.call_args[0][1], "Файл открыт в другой программе. Закройте Компас, Word и Excel "
                                                  "с файлами этого проекта и повторите.")
            self.assertIsNone(app.upd)

    def switch_setup(self, app, tmp, locks):
        from svnhelper import server
        from svnhelper.svn_core import LockInfo, UpdateResult, WcStatus
        item = self.make_item(app, tmp, server.TRUNK_LINE)
        status = WcStatus(my_locks=[LockInfo(os.path.join(item.path, name), "me") for name in locks])
        return item, status, UpdateResult(revision=7)

    def test_switch_unlocks_before_switching(self):
        from svnhelper import server
        root, app, tmp = self.make_app()
        item, status, result = self.switch_setup(app, tmp, ["a.txt", "b.txt"])
        calls = mock.Mock()
        calls.unlock.side_effect = lambda path: None
        calls.switch.return_value = result
        with mock.patch.object(app.client, "unlock", calls.unlock), mock.patch.object(app.client, "switch", calls.switch), \
                mock.patch.object(app, "_reprobe"), \
                mock.patch("tkinter.messagebox.askyesno") as ask, \
                mock.patch("tkinter.messagebox.showinfo") as info:
            app._begin_switch(item, server.Line("branch", "b"), status)
            self.assertEqual(app.mine_status.get(), "Освобождаю занятые файлы (2) и переключаю…")
            self.pump(root, lambda: info.call_count >= 1 and app.upd is None)
            ask.assert_not_called()
        names = [c[0] for c in calls.mock_calls if c[0] in ("unlock", "switch")]
        self.assertEqual(names, ["unlock", "unlock", "switch"])
        self.assertEqual([c.args[0] for c in calls.unlock.call_args_list],
                         [os.path.join(item.path, "a.txt"), os.path.join(item.path, "b.txt")])

    def test_switch_not_started_when_unlock_fails(self):
        from svnhelper import server
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app, tmp = self.make_app()
        item, status, _result = self.switch_setup(app, tmp, ["a.txt"])
        with mock.patch.object(app.client, "unlock", side_effect=SvnError(ErrorKind.OTHER, "boom")), \
                mock.patch.object(app.client, "switch") as switch, \
                mock.patch.object(app, "_reprobe"), \
                mock.patch("svnhelper.gui.App.show_error") as err:
            app._begin_switch(item, server.Line("branch", "b"), status)
            self.pump(root, lambda: err.call_count >= 1)
            switch.assert_not_called()
        self.assertEqual(err.call_args[0][0], "Не удалось освободить занятые файлы")

    def test_unfinished_switch_allows_current_line(self):
        from svnhelper import server
        from svnhelper.svn_core import UpdateResult, WcStatus
        root, app, tmp = self.make_app()
        item = self.make_item(app, tmp, server.TRUNK_LINE, status=WcStatus(switched=3))
        text, tag, hint = app._status_for(item)
        self.assertEqual((text, tag), ("Переключение не закончено", "bad"))
        self.assertIn("«Перейти»", hint)
        self.assertEqual(app._row_values(item)[0][2], "● Переключение не закончено")
        for switched, expect_switch in ((3, True), (0, False)):
            status = WcStatus(switched=switched)
            with mock.patch.object(app.client, "switch", return_value=UpdateResult(revision=5)) as switch, \
                    mock.patch.object(app, "_reprobe"), \
                    mock.patch("tkinter.messagebox.showinfo") as info:
                app._begin_switch(item, server.TRUNK_LINE, status)     # текущая линия
                if expect_switch:
                    self.pump(root, lambda: info.call_count >= 1 and app.upd is None)
                    switch.assert_called_once()
                else:
                    switch.assert_not_called()

    def test_unsaved_changes_refuse_switch_and_offer_saving_in_program(self):
        from svnhelper import server
        from svnhelper.svn_core import WcStatus
        root, app, tmp = self.make_app()
        item = self.make_item(app, tmp, server.TRUNK_LINE)
        app.tortoise_exe = None                                  # TortoiseSVN не нужен: сохранение в самой программе
        app._mine_dirty = False
        app.mine_tree.selection_set(app._iid_of(item))
        root.update()
        for agree in (False, True):
            with mock.patch("tkinter.messagebox.askyesno", return_value=agree) as ask, \
                    mock.patch.object(app, "_on_commit") as commit, \
                    mock.patch.object(app.client, "switch") as switch:
                app._begin_switch(item, server.Line("branch", "b"), WcStatus(modified=3))
            switch.assert_not_called()
            self.assertEqual(ask.call_args[0][1], "Сначала сохраните правки — их 3. Потом переключайтесь.\n\n"
                                                  "Перейти к сохранению сейчас?")
            self.assertEqual(commit.call_count, int(agree))      # «да» - вкладка «Изменения» и поиск правок
            if agree:
                self.assertEqual(app.project_tabs.select(), str(app.project_tab_frames["changes"]))


class LineTextTests(unittest.TestCase):
    """Тексты про ветки и версии без окна."""

    def test_history_lines(self):
        from datetime import datetime, timezone
        from svnhelper import messages
        from svnhelper.svn_core import AuthorStats, BranchHistory, LockInfo
        day = lambda d, m: datetime(2026, m, d, 12, 0, tzinfo=timezone.utc)  # noqa: E731
        history = BranchHistory(
            created_by="Иванов", created=day(12, 10), created_revision=346, copied_from="/p/trunk",
            copied_from_revision=345, comment="Ветка для\nэксперимента", authors=[
                AuthorStats("Иванов", 5, day(18, 10)), AuthorStats("Петров", 2, day(15, 10)),
                AuthorStats("А", 1, day(14, 10)), AuthorStats("Б", 1, day(13, 10)), AuthorStats("В", 1, day(12, 10)),
                AuthorStats("Г", 1, day(11, 10))])
        lines = messages.history_lines(history, [LockInfo("a", "Петров"), LockInfo("b", "Петров")])
        self.assertEqual(lines[0], "Создал Иванов 12.10.2026 из Основной (№ 345): «Ветка для эксперимента»")
        self.assertEqual(lines[1], "Работали: Иванов — 5, Петров — 2, А — 1, Б — 1 и ещё 2. Последнее: Иванов, 18.10")
        self.assertEqual(lines[2], "Заняты файлы: Петров — 2")
        self.assertEqual(len(messages.history_lines(history, [])), 2)          # никто не занимал: строки нет
        self.assertEqual(len(messages.history_lines(history, None)), 2)
        # длинный комментарий обрезается до 60 знаков
        history.comment = "я" * 100
        self.assertTrue(messages.history_lines(history, None)[0].endswith("я" * 60 + "…»"))
        # журнал обрезан: «Создал» остаётся (оно определяется всегда), у авторов - пометка
        history.comment = "x"
        history.complete = False
        cut = messages.history_lines(history, None)
        self.assertTrue(cut[0].startswith("Создал Иванов"))
        self.assertTrue(cut[1].startswith("Работали (последние 200 сохранений): Иванов — 5"))
        # основная: без «Создал»
        history.complete = True
        self.assertTrue(messages.history_lines(history, None, show_created=False)[0].startswith("Работали:"))
        history.copied_from = "/p/branches/ветка"
        self.assertIn("из ветки «ветка» (№ 345)", messages.history_lines(history, None)[0])

    def test_included_texts(self):
        from svnhelper import messages
        self.assertEqual(messages.included_text(345, 0), "Войдёт то, что сохранено на сервере (сохранение № 345).")
        self.assertEqual(messages.included_text(345, 2),
                         "Войдёт то, что сохранено на сервере (сохранение № 345). "
                         "Несохранённые правки (2 файла) не войдут.")

    def test_choice_labels_and_cells(self):
        from datetime import datetime, timezone
        from svnhelper import server
        from svnhelper.gui.models import line_cell, line_choice_label
        from svnhelper.svn_core import LineEntry
        when = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(line_choice_label("branch", LineEntry("x", "ivanov", when)), "Ветка «x» — 18.10, ivanov")
        self.assertEqual(line_choice_label("tag", LineEntry("y", "ivanov", when)), "🔒 Версия «y» — 18.10")
        self.assertEqual(line_choice_label("branch", LineEntry("z")), "Ветка «z»")
        self.assertEqual((line_cell(None), line_cell(server.TRUNK_LINE), line_cell(server.Line("branch", "доработка")),
                          line_cell(server.Line("tag", "2026-10-01"))),
                         ("", "Основная", "доработка", "🔒 Версия 2026-10-01"))

    def test_dialogs_validate_names(self):
        root = tk.Tk()
        root.withdraw()
        try:
            from svnhelper.dialogs import NewLineDialog
            dlg = NewLineDialog(root, "tag", 345, "Войдёт…", "2026-10-19", ["old"])
            self.assertEqual(dlg._comment.get("1.0", "end").strip(), "Версия 2026-10-19")
            dlg._name_var.set("2026-10-20")                                  # комментарий следует за названием
            self.assertEqual(dlg._comment.get("1.0", "end").strip(), "Версия 2026-10-20")
            dlg._name_var.set("a/b")
            dlg._ok()
            self.assertIsNone(dlg.result)
            self.assertIn("нельзя использовать символ", dlg._error.cget("text"))
            dlg._name_var.set("OLD")
            dlg._ok()
            self.assertEqual(dlg._error.cget("text"), "Такое имя уже есть — выберите другое")
            dlg._name_var.set("новая")
            dlg._ok()
            self.assertEqual(dlg.result["name"], "новая")

            branch = NewLineDialog(root, "branch", 345, "Войдёт…", "", [])
            branch._name_var.set("exp")
            branch._comment.delete("1.0", "end")
            branch._comment.insert("1.0", "Ветка")                           # осталось только слово «Ветка»
            branch._ok()
            self.assertIsNone(branch.result)
            self.assertIn("для чего нужна ветка", branch._error.cget("text"))
            branch._comment.insert("end", " для проверки")
            branch._ok()
            self.assertEqual((branch.result["name"], branch.result["switch"]), ("exp", True))

        finally:
            root.destroy()

    def test_dialog_texts_and_name_check(self):
        root = tk.Tk()
        root.withdraw()
        try:
            from svnhelper.dialogs import NewLineDialog
            texts = lambda dlg: [w.cget("text") for w in dlg.winfo_children()[0].winfo_children()  # noqa: E731
                                 if w.winfo_class() in ("TLabel", "TCheckbutton")]
            tag = NewLineDialog(root, "tag", 345, "Войдёт…", "x", [])
            self.assertIn("Версия — снимок проекта, как он сохранён на сервере (сохранение № 345). "
                          "Изменить его потом нельзя.", texts(tag))
            self.assertIn("Комментарий (что в ней):", texts(tag))
            self.assertIn("Название:", texts(tag))
            tag.destroy()

            # занятое имя: проверка идёт, окно не закрывается, внутри показана ошибка
            pending = []
            dlg = NewLineDialog(root, "branch", 345, "Войдёт…", "", [], check_name=lambda n, done: pending.append(done))
            self.assertIn("Ветка — отдельная копия проекта для пробных изменений. Основная при этом не меняется.",
                          texts(dlg))
            dlg._name_var.set("занято")
            dlg._ok()
            self.assertEqual(dlg._error.cget("text"), "Проверяю имя…")
            self.assertEqual(str(dlg.ok_btn.cget("state")), "disabled")
            pending[0](True)
            self.assertIsNone(dlg.result)
            self.assertTrue(dlg.winfo_exists())                              # диалог не закрылся
            self.assertEqual(dlg._error.cget("text"), "Такое имя уже есть — выберите другое")
            self.assertEqual(str(dlg.ok_btn.cget("state")), "normal")
            dlg._name_var.set("свободно")
            dlg._ok()
            pending[1](False)
            self.assertEqual(dlg.result["name"], "свободно")

            # окно закрыли, пока шла проверка: ответ ничего не ломает
            late = []
            gone = NewLineDialog(root, "branch", 1, "Войдёт…", "", [], check_name=lambda n, done: late.append(done))
            gone._name_var.set("имя")
            gone._ok()
            gone.destroy()
            late[0](True)

            # несохранённые правки: «Сразу переключиться» выключена, неактивна, подпись объясняет почему
            blocked = NewLineDialog(root, "branch", 345, "Войдёт…", "", [], can_switch=False)
            self.assertEqual(str(blocked.switch_box.cget("state")), "disabled")
            self.assertIn("Сначала сохраните правки, потом можно переключиться", texts(blocked))
            blocked._name_var.set("exp")
            blocked._comment.insert("end", " для проверки")
            blocked._ok()
            self.assertFalse(blocked.result["switch"])
        finally:
            root.destroy()


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class CommitFlowFakeTests(unittest.TestCase):
    """Сохранение на сервер в программе, на фейковом svn (tests/fake_svn.py): режимы ok, commit_ood, commit_tag."""

    COMMENT = "Правка чертежа"

    def setUp(self):
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.gui.models import MineItem
        from svnhelper.svn_core import SvnClient
        # фоновая проверка проектов может ещё держать calls.jsonl (фейковый svn дописывает журнал вызовов):
        # на Windows удаление папки тогда падает с WinError 32, а к проверяемому поведению это не относится
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_SVN_MODE", "FAKE_SVN_LOG")])
        self.calls_log = os.path.join(self.tmp.name, "calls.jsonl")
        os.environ["FAKE_SVN_LOG"] = self.calls_log
        fake = [sys.executable, str(Path(__file__).resolve().parent / "fake_svn.py")]
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        quiet_clipboard(self.root)
        self.app = App(self.root, Config(server="svn://x", repos=["a"]), SettingsStore(Path(self.tmp.name) / "s.json"),
                       SvnClient(fake), os.path.join(self.tmp.name, "log.txt"))
        self.app.tortoise_exe = None            # результат не должен зависеть от установленного TortoiseSVN
        self.app._apply_tortoise()
        self.wc = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.wc)
        self.item = MineItem(self.wc, "proj", "svn")
        self.iid = self.app._insert_mine_row(self.item)
        self.app.mine[self.iid] = self.item
        self.app._mine_dirty = False                # список проектов собран руками: вкладка не должна искать заново
        self.app.select_list_mode("mine")
        self.app.mine_tree.selection_set(self.iid)
        self.root.update()
        self.pump(lambda: not self.app._checking)   # переход на вкладку сам проверяет проекты: ждём, пока закончит
        App.show_error.reset_mock()

    def pump(self, cond, timeout=20):
        end = time.monotonic() + timeout
        while not cond():
            self.root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), end, "таймаут ожидания")

    def commits(self):
        import json
        if not os.path.exists(self.calls_log):
            return []
        with open(self.calls_log, encoding="utf-8") as fh:
            return [e for e in map(json.loads, fh) if e["args"][0] == "commit"]

    def file(self, name):
        return os.path.join(self.wc, name)

    def open_panel(self):
        """«Сохранить на сервер»: правки появляются в панели «Изменения» колонки 3 (окна больше нет)."""
        from svnhelper import messages
        self.app._on_commit()
        panel = self.app._commit_panel
        self.pump(lambda: self.app.mine_status.get() != messages.COMMIT_LOADING)     # правки найдены и показаны
        self.assertTrue(panel.tree.get_children())
        return panel

    def save(self, panel, only):
        """Оставляет отмеченным один файл, пишет комментарий и нажимает «Сохранить отмеченное»."""
        for path in panel.tree.get_children():
            if path != only:
                panel.toggle(path)
        panel.set_comment(self.COMMENT)
        panel.save_button.invoke()

    def finish(self):
        self.pump(lambda: not self.app._committing)
        self.pump(lambda: not self.app._mine_spinning)

    def test_commit_success(self):
        os.environ["FAKE_SVN_MODE"] = "ok"
        panel = self.open_panel()
        paths = list(panel.tree.get_children())
        self.assertNotIn(self.file("Thumbs.db"), paths)                 # мусор в списке не показывается
        self.assertIn(self.file("новый файл.txt"), paths)
        with mock.patch("tkinter.messagebox.askyesno", return_value=False):
            self.save(panel, self.file("новый файл.txt"))
            self.assertTrue(self.app._committing)
            self.finish()
            self.pump(lambda: self.item.checked)                        # проект проверен заново
        commit = self.commits()[-1]
        self.assertEqual(commit["message"], self.COMMENT)
        self.assertIn("новый файл.txt", commit["targets"])
        self.assertNotIn("a.txt", commit["targets"])
        self.assertEqual(panel.comment(), "")                           # комментарий очищен, список ждёт новой проверки
        self.assertEqual(self.app.mine_status.get(), "Сохранено на сервер: 1 файл")
        self.assertIn("Сохранено на сервер: 1 файл", self.app.project_panel.header_text())    # результат - в шапке
        self.assertEqual(self.app.settings.recent_comments, [self.COMMENT])

    def test_commit_retries_after_out_of_date(self):
        from svnhelper.svn_core import UpdateResult
        os.environ["FAKE_SVN_MODE"] = "commit_ood"
        panel = self.open_panel()

        def fake_update(path, *_a, **_k):
            os.environ["FAKE_SVN_MODE"] = "ok"                         # получили новое: теперь сервер примет
            return UpdateResult(revision=44)

        with mock.patch("tkinter.messagebox.askyesno", side_effect=[True, False]) as ask, \
                mock.patch.object(self.app.client, "update", side_effect=fake_update) as update:
            self.save(panel, self.file("новый файл.txt"))
            self.finish()
            self.pump(lambda: self.item.checked)
        self.assertIn("На сервере есть новее. Получить новое и затем сохранить?", ask.call_args_list[0][0][1])
        update.assert_called_once_with(self.wc)
        commits = self.commits()
        self.assertEqual(len(commits), 2)                              # повтор тем же списком и комментарием
        self.assertEqual((commits[0]["message"], commits[0]["targets"]),
                         (commits[1]["message"], commits[1]["targets"]))
        self.assertEqual(self.app.mine_status.get(), "Сохранено на сервер: 1 файл")
        self.assertEqual(panel.comment(), "")

    def test_commit_stops_on_conflict_after_update(self):
        from svnhelper.svn_core import UpdateResult
        os.environ["FAKE_SVN_MODE"] = "commit_ood"
        panel = self.open_panel()
        with mock.patch("tkinter.messagebox.askyesno", side_effect=[True, False]) as ask, \
                mock.patch.object(self.app.client, "update", return_value=UpdateResult(44, conflicts=["x.txt"])):
            self.save(panel, self.file("новый файл.txt"))
            self.finish()
        self.assertEqual(len(self.commits()), 1)                       # после конфликта повторного сохранения нет
        self.assertEqual(ask.call_count, 2)
        self.assertIn("разберите конфликт", ask.call_args_list[1][0][1])   # и предлагаем разобрать его сразу
        self.assertEqual(self.app.settings.recent_comments, [])

    def fake_conflict_dialog(self, answers, shown):
        """Подмена окна выбора при конфликте: answers по очереди; shown получает (имя, номер, всего)."""
        def make(_parent, name, index=1, total=1):
            shown.append((name, index, total))
            return mock.Mock(result=answers[len(shown) - 1])
        return mock.patch("svnhelper.gui.projects_tab.ConflictDialog", side_effect=make)

    def test_conflict_resolved_in_program_without_tortoise(self):
        """Без TortoiseSVN конфликт решается выбором «моя версия / версия с сервера» в самой программе."""
        from svnhelper import messages
        self.assertIsNone(self.app.tortoise_exe)
        self.item.status = None
        shown = []
        with self.fake_conflict_dialog(["theirs-full"], shown), mock.patch.object(self.root, "wait_window"), \
                mock.patch.object(self.app.client, "resolve") as resolve:
            self.app._start_resolve(self.item)
            self.pump(lambda: shown)
            self.pump(lambda: not self.app._locking)
            resolve.assert_called_once_with(self.file("спор.txt"), "theirs-full")
        self.assertEqual(shown, [("спор.txt", 1, 1)])
        self.assertEqual(self.app._action_notes[os.path.normcase(self.wc)], messages.CONFLICT_DONE)

    def test_conflict_cancel_changes_nothing(self):
        shown = []
        with self.fake_conflict_dialog([None], shown), mock.patch.object(self.root, "wait_window"), \
                mock.patch.object(self.app.client, "resolve") as resolve:
            self.app._start_resolve(self.item)
            self.pump(lambda: shown)
            self.root.update()
        resolve.assert_not_called()
        self.assertFalse(self.app._locking)

    def test_conflict_after_commit_offers_resolve_and_resolves(self):
        from svnhelper.svn_core import UpdateResult
        os.environ["FAKE_SVN_MODE"] = "commit_ood"
        panel = self.open_panel()
        shown = []
        with mock.patch("tkinter.messagebox.askyesno", side_effect=[True, True]), \
                self.fake_conflict_dialog(["mine-full"], shown), mock.patch.object(self.root, "wait_window"), \
                mock.patch.object(self.app.client, "resolve") as resolve, \
                mock.patch.object(self.app.client, "update", return_value=UpdateResult(44, conflicts=["x.txt"])):
            self.save(panel, self.file("новый файл.txt"))
            self.finish()
            self.pump(lambda: shown)
            self.pump(lambda: not self.app._locking)
        resolve.assert_called_once_with(self.file("спор.txt"), "mine-full")

    def test_resolve_failed_for_tree_conflict_explains(self):
        from svnhelper import messages
        from svnhelper.gui import App
        from svnhelper.svn_core import ErrorKind, SvnError
        shown = []
        failure = SvnError(ErrorKind.OTHER, "svn: warning: W195024: Inapplicable\nsvn: E155027: Failure")
        with self.fake_conflict_dialog(["mine-full"], shown), mock.patch.object(self.root, "wait_window"), \
                mock.patch.object(self.app.client, "resolve", side_effect=failure):
            self.app._start_resolve(self.item)
            self.pump(lambda: shown)
            self.pump(lambda: App.show_error.called)
        self.assertEqual(App.show_error.call_args[0][1], messages.CONFLICT_UNSUPPORTED_ADMIN)

    def test_still_out_of_date_after_update_has_own_text(self):
        from svnhelper import messages
        from svnhelper.gui import App
        from svnhelper.svn_core import UpdateResult
        os.environ["FAKE_SVN_MODE"] = "commit_ood"                    # сервер новее и после получения нового
        panel = self.open_panel()
        with mock.patch("tkinter.messagebox.askyesno", return_value=True), \
                mock.patch.object(self.app.client, "update", return_value=UpdateResult(44)):
            self.save(panel, self.file("новый файл.txt"))
            self.finish()
            self.pump(lambda: App.show_error.called)
        self.assertEqual(App.show_error.call_args[0][1], messages.COMMIT_STILL_OUT_OF_DATE)
        self.assertNotIn("занимайте", App.show_error.call_args[0][1])

    def test_saving_one_project_does_not_clear_panel_of_another(self):
        """Выбрали другой проект, пока первый сохранялся: его комментарий и список не сбрасываются."""
        from svnhelper.gui.models import MineItem
        from svnhelper.svn_core import LocalChange, WcStatus
        other = MineItem(os.path.join(self.tmp.name, "other"), "other", "svn",
                         status=WcStatus(local_changes=[LocalChange("x.txt", "modified")]))
        other_iid = self.app._insert_mine_row(other)
        self.app.mine[other_iid] = other
        panel = self.app._commit_panel
        self.app.mine_tree.selection_set(other_iid)
        self.root.update()
        panel.set_comment("для второго проекта")
        self.app._reset_changes_panel(self.item)                       # закончилось сохранение первого проекта
        self.assertEqual(panel.comment(), "для второго проекта")
        self.assertEqual(list(panel.tree.get_children()), ["x.txt"])
        self.app._reset_changes_panel(other)                           # а свой проект сбрасывается
        self.assertEqual((panel.comment(), panel.tree.get_children()), ("", ()))

    def test_recent_comments_list_refreshed_after_save(self):
        os.environ["FAKE_SVN_MODE"] = "ok"
        panel = self.open_panel()
        with mock.patch("tkinter.messagebox.askyesno", return_value=False):
            self.save(panel, self.file("новый файл.txt"))
            self.finish()
        self.assertEqual(panel.recent_comments(), [self.COMMENT])
        menu = panel._recent_menu                                      # и пункты настоящего меню «Последние ▾»
        self.assertEqual([menu.entrycget(i, "label") for i in range(menu.index("end") + 1)], [self.COMMENT])
        self.assertEqual(panel._recent_btn.cget("text"), "Последние ▾")

    def test_checks_wait_for_commit_and_locking(self):
        item = self.item
        for flag in ("_committing", "_locking"):
            with self.subTest(flag=flag):
                self.app._check_queue = []
                setattr(self.app, flag, True)
                try:
                    self.app._check_status([item])
                    self.assertFalse(item.checking)
                    self.assertEqual(self.app._check_queue, [item])
                finally:
                    setattr(self.app, flag, False)
        self.app._drain_check_queue()
        self.pump(lambda: not self.app._checking)

    def test_auto_refresh_does_nothing_when_hidden_in_tray(self):
        self.root.withdraw()
        self.assertEqual(self.root.state(), "withdrawn")
        with mock.patch.object(self.app, "_check_status") as check:
            self.app._auto_refresh_tick()
        check.assert_not_called()

    def test_rebuild_config_updates_commit_templates(self):
        self.app.shared_data = {"commit_templates": ["Один", "Два"]}
        with mock.patch.object(self.app, "_on_refresh"), mock.patch.object(self.app, "_on_url_changed"),                 mock.patch.object(self.app, "_rescan_docs"):
            self.app._rebuild_config()
        self.assertEqual(self.app._commit_panel.template_texts(), ["Один", "Два"])
        # и настоящие кнопки справа: подписи шаблонов на них, а не только внутренний список
        from svnhelper.svn_core import LocalChange
        self.app._commit_panel.set_changes([LocalChange("x.txt", "modified")])
        self.root.update()
        self.assertEqual(self.app.project_panel.current_tab(), "changes")
        self.assertEqual([str(b.cget("text")) for b in self.app.project_panel.side_buttons()],
                         ["Сохранить отмеченное", "Один", "Два"])

    def test_commit_lock_required_offers_lock_and_saves(self):
        from svnhelper.svn_core import CommitResult
        os.environ["FAKE_SVN_MODE"] = "ok"
        panel = self.open_panel()
        target = self.file("a.txt")                                     # на нём svn:needs-lock, не занят
        locked = []

        def fake_lock(path, *_a, **_k):
            locked.append(path)
            self.app.client.commit = lambda *a, **k: CommitResult(45, [path])   # с замком сервер примет

        with mock.patch("tkinter.messagebox.askyesno", side_effect=[True, False]) as ask, \
                mock.patch.object(self.app.client, "lock", side_effect=fake_lock):
            self.save(panel, target)
            self.finish()
        self.assertEqual(self.commits(), [])                           # без замка svn commit не запускался
        self.assertIn("Файл a.txt нужно сначала занять", ask.call_args_list[0][0][1])
        self.assertEqual(locked, [target])
        self.assertEqual(self.app.mine_status.get(), "Сохранено на сервер: 1 файл")

    def test_commit_lock_declined_keeps_panel(self):
        os.environ["FAKE_SVN_MODE"] = "ok"
        panel = self.open_panel()
        with mock.patch("tkinter.messagebox.askyesno", return_value=False), \
                mock.patch.object(self.app.client, "lock") as lock:
            self.save(panel, self.file("a.txt"))
            self.finish()
        lock.assert_not_called()
        self.assertTrue(panel.tree.get_children())                     # список на месте: можно выбрать другие файлы
        self.assertEqual(panel.comment(), self.COMMENT)
        self.assertEqual(str(panel.save_button.cget("state")), "normal")

    def test_commit_into_version_is_refused(self):
        from svnhelper import messages, server
        os.environ["FAKE_SVN_MODE"] = "ok"
        self.item.line = server.Line("tag", "v1")
        self.app._refresh_row(self.item)
        with mock.patch("tkinter.messagebox.showinfo") as info:
            self.app._on_commit()
        info.assert_called_once()
        self.assertEqual(info.call_args[0][1], messages.COMMIT_TAG_REFUSED)
        self.assertIn("создайте ветку", messages.COMMIT_TAG_REFUSED)
        self.assertEqual([w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)], [])
        self.assertEqual(str(self.app._commit_panel.save_button.cget("state")), "disabled")   # в версию не сохраняем
        self.assertEqual(self.commits(), [])

    def test_commit_rejected_by_server_hook_for_version(self):
        """Копия не распознана как версия, но сервер (хук) её отклонил: понятное сообщение, панель остаётся."""
        from svnhelper import messages
        from svnhelper.gui import App
        from svnhelper.svn_core import ErrorKind
        os.environ["FAKE_SVN_MODE"] = "commit_tag"
        panel = self.open_panel()
        self.save(panel, self.file("новый файл.txt"))
        self.finish()
        App.show_error.assert_called_once()
        self.assertEqual(App.show_error.call_args[0][1], messages.short_message(ErrorKind.READ_ONLY_LINE))
        self.assertEqual(App.show_error.call_args[1]["kind"], ErrorKind.READ_ONLY_LINE)
        self.assertEqual(self.app.settings.recent_comments, [])
        self.assertTrue(panel.tree.get_children())
        self.assertEqual(str(panel.save_button.cget("state")), "normal")

    def test_commit_panel_lives_in_column_three_without_modal_window(self):
        os.environ["FAKE_SVN_MODE"] = "ok"
        self.app.mine_col3.collapse()
        panel = self.open_panel()
        self.assertFalse(self.app.mine_col3.collapsed)                  # колонка развернулась
        self.assertEqual(self.app.project_tabs.select(), str(self.app.project_tab_frames["changes"]))
        self.assertTrue(str(panel).startswith(str(self.app.project_tab_frames["changes"])))
        self.assertEqual([w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)], [])
        count = len(panel.tree.get_children())
        self.assertEqual(self.app.project_tabs.tab(self.app.project_tab_frames["changes"], "text"),
                         f"Изменения {count}")                          # счётчик на ярлыке
        self.assertFalse(hasattr(self.app, "_commit_window"))

    def test_changes_follow_project_status_and_keep_marks(self):
        from svnhelper.svn_core import LocalChange, WcStatus
        panel = self.app._commit_panel
        tab = lambda: self.app.project_tabs.tab(self.app.project_tab_frames["changes"], "text")  # noqa: E731
        self.item.status = WcStatus()
        self.app._refresh_row(self.item)
        self.assertEqual(tab(), "Изменения")
        changes = [LocalChange(self.file("a.txt"), "modified"), LocalChange(self.file("b.txt"), "unversioned")]
        self.item.status = WcStatus(local_changes=changes)
        self.app._refresh_row(self.item)
        self.assertEqual(list(panel.tree.get_children()), [self.file("a.txt"), self.file("b.txt")])
        self.assertEqual(tab(), "Изменения 2")
        panel.toggle(self.file("a.txt"))
        panel.set_comment("пишу")
        self.item.status = WcStatus(local_changes=list(changes))        # очередная проверка: те же правки
        self.app._refresh_row(self.item)
        self.assertEqual(panel.checked_paths(), [self.file("b.txt")])   # галочки и комментарий не сбросились
        self.assertEqual(panel.comment(), "пишу")
        self.item.status = WcStatus()
        self.app._refresh_row(self.item)
        self.assertEqual((panel.tree.get_children(), tab()), ((), "Изменения"))

    def test_other_project_clears_comment_and_shows_its_own_changes(self):
        from svnhelper.gui.models import MineItem
        from svnhelper.svn_core import LocalChange, WcStatus
        other = MineItem(os.path.join(self.tmp.name, "other"), "other", "svn",
                         status=WcStatus(local_changes=[LocalChange("x.txt", "modified")]))
        other_iid = self.app._insert_mine_row(other)
        self.app.mine[other_iid] = other
        panel = self.app._commit_panel
        panel.set_comment("для первого проекта")
        self.app.mine_tree.selection_set(other_iid)
        self.root.update()
        self.assertEqual(panel.comment(), "")
        self.assertEqual(list(panel.tree.get_children()), ["x.txt"])
        self.assertIs(self.app._commit_item, other)

    def test_commit_refused_while_folder_busy(self):
        from svnhelper import messages
        self.app._tortoise_open.add(self.wc)
        self.app._update_mine_buttons()
        with mock.patch("tkinter.messagebox.showinfo") as info:
            self.app._on_commit()
        self.assertEqual(info.call_args[0][1], messages.COMMIT_BUSY)
        self.assertEqual(str(self.app._commit_panel.save_button.cget("state")), "disabled")

    def test_close_asks_about_unsaved_changes(self):
        from svnhelper.svn_core import LocalChange, WcStatus
        self.item.status = WcStatus()                                  # правок нет: и спрашивать не о чем
        self.assertEqual(self.app._unsaved_project_names(), [])
        # только новый файл (счётчик modified его не видит): это тоже несохранённая правка
        self.item.status = WcStatus(unversioned=1, local_changes=[LocalChange(self.file("новый.txt"), "unversioned")])
        with mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            self.app._on_close()
        self.assertIn("Есть несохранённые правки в: proj", ask.call_args[0][1])
        self.assertFalse(self.app._closing)                            # отказались закрывать


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class CommitPanelTests(unittest.TestCase):
    def setUp(self):
        from svnhelper import theme
        from svnhelper.gui.commit_panel import CommitPanel
        from svnhelper.svn_core import LocalChange
        self.LocalChange = LocalChange
        self.root = tk.Tk()
        self.root.withdraw()
        theme.apply(self.root)
        self.saved = []
        self.panel = CommitPanel(self.root, recent_comments=["раз", "два"],
                                 on_save=lambda paths, text: self.saved.append((paths, text)))
        self.panel.pack()
        self.panel.set_changes([
            self.LocalChange("чертёж.cdw", "modified", locked_by_me=True),
            self.LocalChange("новый.txt", "unversioned"),
            self.LocalChange("старый.txt", "missing"),
            self.LocalChange("спор.txt", "conflicted"),
        ])

    def tearDown(self):
        self.root.destroy()

    def test_defaults_checked_except_conflict(self):
        self.assertEqual(self.panel.checked_paths(), ["чертёж.cdw", "новый.txt", "старый.txt"])
        self.assertIn("сначала разберите конфликт", self.panel.tree.set("спор.txt", "kind"))
        self.panel.toggle("спор.txt")
        self.assertNotIn("спор.txt", self.panel.checked_paths())
        self.assertEqual(self.panel.tree.set("чертёж.cdw", "lock"), "🔒")

    def test_empty_comment_blocks_save(self):
        self.panel._save()
        self.assertEqual(self.panel.error_text(), "Напишите, что вы сделали")
        self.assertEqual(self.saved, [])
        self.panel.set_comment("правка")           # ввод убирает ошибку
        self.assertEqual(self.panel.error_text(), "")

    def test_nothing_checked_blocks_save(self):
        for path in list(self.panel.checked_paths()):
            self.panel.toggle(path)
        self.panel.set_comment("правка")
        self.panel._save()
        self.assertEqual(self.panel.error_text(), "Отметьте хотя бы один файл")
        self.assertEqual(self.saved, [])
        self.panel.toggle("новый.txt")
        self.assertEqual(self.panel.error_text(), "")

    def test_save_passes_checked_paths_and_comment(self):
        self.panel.toggle("старый.txt")
        self.panel.set_comment("  Правка чертежа ")
        self.panel._save()
        self.assertEqual(self.saved, [(["чертёж.cdw", "новый.txt"], "Правка чертежа")])

    def test_templates_recent_and_disable(self):
        buttons = [w for w in self.panel._templates_row.winfo_children()]
        self.assertEqual([str(b.cget("text")) for b in buttons],
                         ["Правка чертежа", "Новая редакция КП", "Исправил по замечаниям"])
        buttons[1].invoke()
        self.assertEqual(self.panel.comment(), "Новая редакция КП")
        self.assertEqual(list(self.panel._recent.cget("values")), ["раз", "два"])
        self.panel.set_enabled(False, "Версию менять нельзя")
        self.assertEqual(str(self.panel.save_button.cget("state")), "disabled")
        self.panel._save()
        self.assertEqual(self.saved, [])
        self.panel.toggle("новый.txt")
        self.assertIn("новый.txt", self.panel.checked_paths())

    def test_empty_state(self):
        self.panel.set_changes([])
        self.root.update_idletasks()
        self.assertTrue(self.panel._empty.winfo_ismapped() or self.panel._empty.winfo_manager())
        self.assertFalse(self.panel._body.winfo_manager())
        self.assertIn("Правок нет", str(self.panel._empty.cget("text")))

    def test_dark_theme_builds(self):
        from svnhelper import theme
        theme.apply(self.root, mode="dark")
        self.panel.set_changes([self.LocalChange("a.txt", "modified")])
        theme.configure_tree_tags(self.panel.tree)
        theme.apply(self.root, mode="light")


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class SideColumnButtonsTests(unittest.TestCase):
    """U4.1: колонка кнопок справа на «Изменениях» (шаблоны, «Последние ▾») и на «Занятых» - без окна с проектами."""

    LONG = "Исправил размеры на сборочном чертеже по замечаниям главного конструктора и технолога"

    def make_panel(self, scale=1.0, templates=None, recent=("раз", "два")):
        from svnhelper import theme
        from svnhelper.gui.project_panel import ProjectPanel
        root = tk.Tk()
        self.addCleanup(self.destroy_root, root)
        root.tk.call("tk", "scaling", 96 * scale / 72)
        theme.apply(root)
        px = lambda value: int(value * scale)                                      # noqa: E731
        self.saved, self.released, self.locked = [], [], []
        panel = ProjectPanel(root, px, templates=templates, recent_comments=list(recent),
                             on_save=lambda paths, text: self.saved.append((paths, text)),
                             on_release=self.released.append, on_lock=lambda: self.locked.append(True))
        panel.pack(fill="both", expand=True)
        root.geometry("1000x600")
        root.update()
        self.root, self.panel, self.px = root, panel, px
        return panel

    @staticmethod
    def destroy_root(root):
        try:
            root.destroy()
        except tk.TclError:                     # уже закрыто самим тестом
            pass

    def show_changes(self, panel):
        from svnhelper.svn_core import LocalChange
        panel.tabs.select(panel.frames["changes"])
        panel.commit_panel.set_changes([LocalChange("a.txt", "modified")])
        self.root.update()

    @staticmethod
    def texts(panel):
        return [str(b.cget("text")) for b in panel.side_buttons()]

    def test_changes_tab_has_save_templates_and_recent_in_order(self):
        panel = self.make_panel()
        self.assertEqual(panel.side_buttons(), [])                         # правок нет: нет и колонки
        self.show_changes(panel)
        self.assertEqual(self.texts(panel), ["Сохранить отмеченное", "Правка чертежа", "Новая редакция КП",
                                             "Исправил по замечаниям", "Последние ▾"])
        ys = [b.winfo_y() for b in panel.side_buttons()]
        self.assertEqual(ys, sorted(ys))
        # шаблоны отделены от кнопки сохранения: зазор больше обычного
        buttons = panel.side_buttons()
        gap = lambda a, b: b.winfo_y() - (a.winfo_y() + a.winfo_height())      # noqa: E731
        self.assertGreater(gap(buttons[0], buttons[1]), gap(buttons[1], buttons[2]))
        # под списком правок кнопок нет: только поле и ошибка
        body = panel.commit_panel
        outside = [w for w in SideColumnButtonsTests.all_widgets(body, "TButton") if w.master is not panel.side
                   and not str(w.master).startswith(str(panel.side))]
        self.assertEqual(outside, [])
        self.assertEqual(str(body.entry.winfo_manager()), "grid")
        panel.side_buttons()[2].invoke()
        self.assertEqual(body.comment(), "Новая редакция КП")

    @staticmethod
    def all_widgets(widget, klass):
        found = []
        for child in widget.winfo_children():
            if child.winfo_class() == klass:
                found.append(child)
            found.extend(SideColumnButtonsTests.all_widgets(child, klass))
        return found

    def test_recent_menu_lists_comments_and_fills_the_field(self):
        panel = self.make_panel(recent=["раз", "два", " ", "три", "4", "5", "6"])
        self.show_changes(panel)
        body = panel.commit_panel
        self.assertEqual(body.recent_comments(), ["раз", "два", "три", "4", "5"])      # пять последних, пустые не берём
        menu = body._recent_menu
        self.assertEqual([menu.entrycget(i, "label") for i in range(menu.index("end") + 1)],
                         ["раз", "два", "три", "4", "5"])
        menu.invoke(1)
        self.assertEqual(body.comment(), "два")
        body.set_recent_comments([])                                            # нечего выбирать: кнопки нет
        self.root.update()
        self.assertNotIn("Последние ▾", self.texts(panel))
        body.set_recent_comments(["x" * 200])                                   # длинный комментарий в меню коротким
        label = body._recent_menu.entrycget(0, "label")
        self.assertTrue(label.endswith("…") and len(label) <= body.RECENT_LABEL_MAX)
        body._recent_menu.invoke(0)
        self.assertEqual(body.comment(), "x" * 200)                             # а в поле - целиком

    def test_long_template_does_not_widen_column_and_has_full_tooltip(self):
        self.assertGreater(len(self.LONG), 60)
        panel = self.make_panel(templates=[self.LONG, "Короткий"])
        self.show_changes(panel)
        limit = self.px(panel.SIDE_MAX_WIDTH)
        buttons = panel.side_buttons()
        self.assertLessEqual(panel.side.winfo_reqwidth(), limit)
        self.assertLessEqual(max(b.winfo_width() for b in buttons), limit)
        long_button = buttons[1]
        shown = str(long_button.cget("text"))
        self.assertTrue(shown.endswith("…") and len(shown) < len(self.LONG))
        self.assertEqual(long_button._full_tip.text_fn(), self.LONG)            # полный текст - в подсказке
        self.assertEqual(buttons[2].cget("text"), "Короткий")
        self.assertEqual(buttons[2]._full_tip.text_fn(), "")                    # у короткого подсказки нет
        long_button.invoke()
        self.assertEqual(panel.commit_panel.comment(), self.LONG)               # в поле попадает целиком
        self.assertEqual(panel.commit_panel.template_texts(), [self.LONG, "Короткий"])

    def test_long_template_fits_at_every_scale(self):
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale):
                panel = self.make_panel(scale=scale, templates=[self.LONG, self.LONG, self.LONG])
                self.show_changes(panel)
                limit = self.px(panel.SIDE_MAX_WIDTH)
                for button in panel.side_buttons():
                    self.assertLessEqual(button.winfo_width(), limit, button.cget("text"))
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth(), button.cget("text"))
                    self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                         self.root.winfo_rootx() + self.root.winfo_width())
                self.root.destroy()

    def test_templates_are_rebuilt_in_place_before_recent(self):
        panel = self.make_panel()
        self.show_changes(panel)
        panel.commit_panel.set_templates(["Один", "Два"])                       # общие настройки перечитаны
        self.root.update()
        self.assertEqual(self.texts(panel), ["Сохранить отмеченное", "Один", "Два", "Последние ▾"])
        panel.commit_panel.set_templates([])
        self.root.update()
        self.assertEqual(self.texts(panel), ["Сохранить отмеченное", "Последние ▾"])

    def test_blocked_panel_blocks_templates_too(self):
        panel = self.make_panel()
        self.show_changes(panel)
        panel.commit_panel.set_enabled(False, "В версию сохранять нельзя")
        self.assertEqual({str(b.cget("state")) for b in panel.side_buttons()}, {"disabled"})
        panel.commit_panel.set_templates(["Новый"])                             # и новые шаблоны заблокированы
        self.assertEqual({str(b.cget("state")) for b in panel.side_buttons()}, {"disabled"})
        panel.commit_panel.set_enabled(True)
        self.assertEqual({str(b.cget("state")) for b in panel.side_buttons()}, {"normal"})

    def test_locks_tab_buttons_and_release_without_selection(self):
        from svnhelper.svn_core import LockInfo
        panel = self.make_panel()
        panel.tabs.select(panel.frames["locks"])
        panel.set_locks_enabled(True, True)
        base = os.path.join(os.sep, "proj")
        mine, other = os.path.join(base, "a.bin"), os.path.join(base, "b.bin")
        panel.set_locks(base, [LockInfo(mine, "me")], [LockInfo(other, "petrov")])
        self.root.update()
        self.assertEqual(self.texts(panel), ["Занять файл…", "Освободить…"])
        tree = panel.locks_tree
        self.assertEqual(str(tree.cget("selectmode")), "extended")
        own_row, other_row = tree.get_children()
        self.assertEqual(tree.selection(), ())
        self.assertEqual(str(panel.release_btn.cget("state")), "normal")
        panel.release_btn.invoke()
        self.assertEqual(self.released, [[]])                                   # без выбора: окно выбора спросит само
        tree.selection_set(own_row, other_row)                                  # выбраны свой и чужой: освобождает свой
        self.root.update()
        panel.release_btn.invoke()
        self.assertEqual(self.released[-1], [mine])
        tree.selection_set(other_row)                                           # только чужой: освобождать нечего
        self.root.update()
        self.assertEqual(str(panel.release_btn.cget("state")), "disabled")
        panel.lock_btn.invoke()
        self.assertEqual(self.locked, [True])
        panel.set_locks_enabled(False, False)
        tree.selection_clear()
        self.assertEqual((str(panel.lock_btn.cget("state")), str(panel.release_btn.cget("state"))),
                         ("disabled", "disabled"))

    def test_incoming_tab_counter(self):
        panel = self.make_panel()
        tab = lambda: panel.tabs.tab(panel.frames["incoming"], "text")          # noqa: E731
        self.assertEqual(tab(), "Новое")
        panel.set_incoming_count(4)
        self.assertEqual(tab(), "Новое 4")
        panel.set_incoming_count(0)
        self.assertEqual(tab(), "Новое")

    def test_dark_theme_builds(self):
        from svnhelper import theme
        panel = self.make_panel(templates=[self.LONG])
        theme.apply(self.root, mode="dark")
        self.show_changes(panel)
        panel.commit_panel.set_templates(["Один", self.LONG])
        panel.commit_panel.set_recent_comments(["раз"])
        self.root.update()
        self.assertEqual(len(panel.side_buttons()), 4)
        theme.apply(self.root, mode="light")

    def test_fit_text(self):
        from svnhelper.gui.widgets import fit_text, text_width
        root = tk.Tk()
        self.addCleanup(root.destroy)
        self.assertEqual(fit_text(root, "Коротко", 500), "Коротко")
        for room in (40, 120, 200):
            shown = fit_text(root, self.LONG, room)
            self.assertTrue(shown.endswith("…"))
            self.assertLessEqual(text_width(root, shown), room)
        self.assertEqual(fit_text(root, self.LONG, 1), "…")


class CommitTemplatesConfigTests(unittest.TestCase):
    def test_defaults_and_override(self):
        from svnhelper.config import Config, apply_config_data
        cfg = Config()
        self.assertEqual(cfg.commit_templates, ["Правка чертежа", "Новая редакция КП", "Исправил по замечаниям"])
        apply_config_data(cfg, {"commit_templates": ["Один", " ", "Два"]})
        self.assertEqual(cfg.commit_templates, ["Один", "Два"])
        apply_config_data(cfg, {"commit_templates": "x"})
        self.assertEqual(cfg.commit_templates, ["Один", "Два"])
        self.assertTrue(cfg.warnings)


class MineColumn2ViewTests(unittest.TestCase):
    """Колонка 2: поиск проекта, узкий и широкий вид, сортировка по заголовку (Task C.2)."""

    setUpClass = MineTabTests.__dict__["setUpClass"]
    tearDownClass = MineTabTests.__dict__["tearDownClass"]
    make_app = MineTabTests.make_app
    checkout = MineTabTests.checkout
    pump = MineTabTests.pump

    def _fill(self, app, names):
        from svnhelper.gui.models import MineItem
        for name in names:
            item = MineItem(f"p_{name}", name, "svn")
            app.mine[app._insert_mine_row(item)] = item
        app._apply_mine_sort()

    def _names(self, app):
        return [app.mine_tree.set(i, "name") for i in app.mine_tree.get_children()]

    def test_filter_is_case_insensitive_and_restores(self):
        root, app, _base = self.make_app("flt", scan=False)
        try:
            self._fill(app, ["Сушилки", "Отбраковка", "сушка2"])
            app.mine_filter_var.set("СУШ")
            self.assertEqual(self._names(app), ["Сушилки", "сушка2"])
            app.mine_filter_var.set("нет такого")
            self.assertEqual(self._names(app), [])
            app.mine_filter_var.set("")
            self.assertEqual(self._names(app), ["Отбраковка", "Сушилки", "сушка2"])
            self.assertFalse(hasattr(app, "mine_sort_combo"))
        finally:
            root.destroy()

    def test_view_switch_changes_columns_and_keeps_selection(self):
        root, app, _base = self.make_app("view", scan=False)
        try:
            self._fill(app, [f"proj{i:02d}" for i in range(30)])
            iid = app.mine_tree.get_children()[12]
            app.mine_tree.selection_set(iid)
            app.mine_tree.see(iid)
            root.update()
            top = app.mine_tree.yview()[0]
            self.assertEqual(list(app.mine_tree["displaycolumns"]), ["brief"])
            self.assertEqual(app.mine_tree.set(iid, "brief").split(chr(10))[0], "proj12")
            app.mine_col3.collapse()
            root.update()
            self.assertEqual(list(app.mine_tree["displaycolumns"]),
                             ["name", "status", "line", "changed", "todo"])
            self.assertEqual(app.mine_tree.selection(), (iid,))
            self.assertAlmostEqual(app.mine_tree.yview()[0], top, places=3)
            app.mine_col3.expand()
            root.update()
            self.assertEqual(list(app.mine_tree["displaycolumns"]), ["brief"])
            self.assertEqual(app.mine_tree.selection(), (iid,))
            self.assertAlmostEqual(app.mine_tree.yview()[0], top, places=3)
        finally:
            root.destroy()

    def test_heading_click_sorts_in_wide_view(self):
        root, app, _base = self.make_app("hsort", scan=False)
        try:
            self._fill(app, ["a", "b", "c"])
            app.settings.mine_sort = "opened"      # старое значение settings - начальная сортировка
            app.settings.opened = {os.path.normcase("p_c"): 300.0, os.path.normcase("p_b"): 100.0}
            app._apply_mine_sort()
            self.assertEqual(self._names(app), ["c", "b", "a"])
            app.mine_col3.collapse()
            app._on_heading_sort("name")
            self.assertEqual(self._names(app), ["a", "b", "c"])
            self.assertEqual(app.settings.mine_sort, "name")
            self.assertEqual(app.mine_tree.heading("name", "text"), "Проект ▲")
        finally:
            root.destroy()


class MainActionTests(unittest.TestCase):
    """C.4: выбор главного действия колонки 1 (чистая функция) по каждому состоянию проекта."""

    def test_main_action_for_every_state(self):
        from svnhelper.gui.projects_tab import main_action
        from svnhelper.svn_core import WcStatus
        changes = [mock.Mock()]
        cases = [
            (None, None, False, None),                                    # проект не выбран
            ("plain", None, False, "publish"),                            # папка только на компьютере: отправить на сервер
            ("broken", None, False, "repair"),                            # повреждена
            ("svn", None, False, "update"),                               # проверка ещё не пришла
            ("svn", WcStatus(), False, "update"),                         # актуален
            ("svn", WcStatus(incoming=3), False, "update"),               # есть новое
            ("svn", WcStatus(local_changes=changes), False, "commit"),    # есть правки
            ("svn", WcStatus(local_changes=changes, incoming=1), False, "commit"),
            ("svn", WcStatus(conflicts=1), False, "resolve"),             # конфликт
            ("svn", WcStatus(conflicts=1, local_changes=changes, incoming=1), False, "resolve"),   # конфликт важнее
            ("svn", WcStatus(local_changes=changes, incoming=1), True, "update"),   # версия: правки не сохраняются
        ]
        for state, status, read_only, expected in cases:
            with self.subTest(state=state, conflicts=getattr(status, "conflicts", None), read_only=read_only):
                self.assertEqual(main_action(state, status, read_only), expected)

    def test_main_action_for_local_history(self):
        """Ревью 2026-11-02, п.5: история потеряна - «Восстановить», если нашлась, иначе «Отправить на сервер»;
        «Продолжить без истории» главным не бывает."""
        from svnhelper.gui.projects_tab import main_action
        from svnhelper.svn_core import WcStatus
        changes = [mock.Mock()]
        cases = [(None, False, False, "commit"), (WcStatus(), False, False, "publish"),
                 (WcStatus(local_changes=changes), False, False, "commit"),
                 (None, True, True, "restore"), (WcStatus(), True, True, "restore"),
                 (None, True, False, "publish"), (WcStatus(local_changes=changes), True, False, "publish")]
        for status, missing, found, expected in cases:
            with self.subTest(missing=missing, found=found, changes=bool(getattr(status, "local_changes", None))):
                self.assertEqual(main_action("svn", status, False, True, missing, found), expected)


class ColumnOneButtonsTests(unittest.TestCase):
    """C.4: кнопки колонки 1 - порядок, подсветка главного действия, переходы во вкладки."""

    setUpClass = MineTabTests.__dict__["setUpClass"]
    tearDownClass = MineTabTests.__dict__["tearDownClass"]
    pump = MineTabTests.pump
    checkout = MineTabTests.checkout
    make_app = MineTabTests.make_app
    select_first = MineTabTests.select_first

    @staticmethod
    def accents(app):
        """Все подсвеченные кнопки окна (обе колонки развёрнуты): на экране она одна, главное действие."""
        return shown_accents(app)

    def show_state(self, app, item, state, status):
        """Проект в состоянии state/status: панель и кнопки перерисованы."""
        item.state, item.status = state, status
        app._refresh_project_panel()
        app._update_mine_buttons()

    def test_exactly_one_accent_button_per_state(self):
        """U4.2: для каждого главного действия на экране (обе колонки развёрнуты) ровно одна подсвеченная кнопка:
        слева - разобрать, починить, получить, отправить; справа - сохранение на «Изменениях»."""
        from svnhelper.svn_core import LocalChange, WcStatus
        root, app, _base = self.make_app("c4_accent")
        try:
            iid, item = self.select_first(root, app)
            self.pump(root, lambda: not app._checking and not item.checking)
            app.tortoise_exe = "C:\\fake\\TortoiseProc.exe"
            app._apply_tortoise()
            self.assertFalse(app.mine_col2.collapsed or app.mine_col3.collapsed)
            panel = app.project_panel
            panel.tabs.select(panel.frames["changes"])
            change = LocalChange(os.path.join(item.path, "a.txt"), "modified")
            cases = [("svn", WcStatus(), "update", app.update_btn),
                     ("svn", WcStatus(incoming=2), "update", app.update_btn),
                     ("svn", WcStatus(local_changes=[change]), "commit", app._commit_panel.save_button),
                     ("svn", WcStatus(incoming=2, local_changes=[change]), "commit", app._commit_panel.save_button),
                     ("svn", WcStatus(conflicts=1), "resolve", app.special_btn),
                     ("broken", None, "repair", app.special_btn),
                     ("plain", None, "publish", app.publish_btn)]
            for state, status, main, expected in cases:
                with self.subTest(state=state, status=status):
                    self.show_state(app, item, state, status)
                    self.assertEqual(app._main_action_of(item), main)
                    self.assertEqual(self.accents(app), [expected])
            # сохранение подсвечено на любой подвкладке, где оно видно, и только у главного действия
            self.show_state(app, item, "svn", WcStatus(local_changes=[change]))
            self.assertEqual(str(app._commit_panel.save_button.cget("style")), "Accent.TButton")
            self.show_state(app, item, "svn", WcStatus(conflicts=1, local_changes=[change]))
            self.assertEqual(str(app._commit_panel.save_button.cget("style")), "TButton")   # главное - разобрать конфликт
            self.assertEqual(self.accents(app), [app.special_btn])
            self.show_state(app, item, "broken", None)
            self.assertEqual(app.special_btn.cget("text"), "Починить папку")
            self.show_state(app, item, "svn", WcStatus(conflicts=1))
            self.assertEqual(app.special_btn.cget("text"), "Разобрать конфликт")
            # без выбранного проекта ни одной подсвеченной кнопки
            app.mine_tree.selection_set(())
            root.update()
            app._update_mine_buttons()
            self.assertEqual(self.accents(app), [])
            self.assertEqual(app.mine_name_label.cget("text"), "")
        finally:
            root.destroy()

    def test_resolve_button_works_without_tortoise_and_notice_leads_to_it(self):
        from svnhelper import notifier
        from svnhelper.svn_core import WcStatus
        root, app, _base = self.make_app("c4_resolve")
        try:
            iid, item = self.select_first(root, app)
            self.pump(root, lambda: not app._checking and not item.checking)
            self.assertIsNone(app.tortoise_exe)
            item.state, item.status = "svn", WcStatus(conflicts=1)
            app._update_mine_buttons()
            self.assertEqual(app.special_btn.cget("text"), "Разобрать конфликт")
            self.assertEqual(str(app.special_btn.cget("state")), "normal")      # TortoiseSVN для этого не нужен
            self.assertEqual(self.accents(app), [app.special_btn])
            app.mine_tree.selection_set(())
            notice = notifier.Notice("conflict:x", "conflict", "p", "t", item.path, "changes")
            with mock.patch.object(app, "show_window"):
                app._notice_open(notice)
            self.assertEqual(app.mine_tree.selection(), (iid,))
            self.assertEqual(str(app.special_btn.cget("state")), "normal")
            self.assertEqual(app.project_tabs.select(), str(app.project_tab_frames["changes"]))
        finally:
            root.destroy()

    def test_lamp_texts_use_one_term(self):
        from svnhelper.svn_core import WcStatus
        root, app, _base = self.make_app("c4_lamp")
        try:
            iid, item = self.select_first(root, app)
            self.pump(root, lambda: not app._checking and not item.checking)
            item.status, item.checked = WcStatus(incoming=1), True
            app._mine_spinning = False
            self.assertIn("Нужно получить новое или сохранить", app._lamp_state()[2])
            app._mine_spinning = True
            self.assertTrue(app._lamp_state()[2].startswith("Идёт работа"))
            self.assertFalse(hasattr(type(app), "_show_commit_window"))
            self.assertEqual(app._diff_loading, False)       # состояние «Отличий» задано в __init__
        finally:
            root.destroy()

    def test_order_name_and_incoming_counter(self):
        from svnhelper.svn_core import WcStatus
        root, app, _base = self.make_app("c4_order")
        try:
            iid, item = self.select_first(root, app)
            self.pump(root, lambda: not app._checking and not item.checking)
            self.show_state(app, item, "svn", WcStatus(incoming=4))
            self.assertEqual(app.mine_name_label.cget("text"), item.display_name)
            self.assertEqual(app.update_btn.cget("text"), "Получить новое (4)")
            panel = app.project_panel                       # то же число - на ярлыке вкладки «Новое»
            self.assertEqual(panel.tabs.tab(panel.frames["incoming"], "text"), "Новое 4")
            self.show_state(app, item, "svn", WcStatus())
            self.assertEqual(app.update_btn.cget("text"), "Получить новое")
            self.assertEqual(panel.tabs.tab(panel.frames["incoming"], "text"), "Новое")
            row = app.actions_row
            shown = [w for w in row._items if w not in row._hidden]
            self.assertEqual(shown, [app.mine_name_label, app.check_btn, app.update_btn, app.open_btn, app.server_btn,
                                     app.more_btn, app.new_project_btn])
            self.assertEqual([w.cget("text") for w in shown[2:]],
                             ["Получить новое", "Открыть папку", "Открыть на сервере", "Ещё ▾", "＋ Новый проект…"])
            for gone in ("commit_btn", "lock_btn", "unlock_btn", "lines_nav_btn", "goto_download_btn"):
                self.assertFalse(hasattr(app, gone), gone)             # сохранение, занятые, ветки - справа
        finally:
            root.destroy()

    def test_lock_reminder_points_to_locks_tab(self):
        from svnhelper import messages
        self.assertIn("вкладка «Занятые» → «Занять файл…»", messages.LOCK_REMINDER)
        self.assertNotIn("Мои проекты", messages.LOCK_REMINDER)

    def test_lock_buttons_live_on_locks_tab_and_release_without_choice_opens_picker(self):
        root, app, _base = self.make_app("c4_tabs")
        try:
            iid, item = self.select_first(root, app)
            self.pump(root, lambda: not app._checking and not item.checking)
            panel = app.project_panel
            app.open_project_tab("locks")
            self.assertEqual([b.cget("text") for b in panel.side_buttons()], ["Занять файл…", "Освободить…"])
            with mock.patch.object(app, "_on_get_lock") as get_lock, \
                    mock.patch.object(app, "_on_release_lock") as release:
                panel.lock_btn.invoke()
                get_lock.assert_called_once_with()
                panel.release_btn.invoke()                         # ничего не выбрано: окно выбора своих файлов
                release.assert_called_once_with()
            app.select_list_mode("download")
            self.assertEqual(app._list_mode(), "download")
            # настоящая вкладка колонки 3 разворачивается и выбирается
            app.mine_col3.collapse()
            app.open_project_tab("locks")
            self.assertFalse(app.mine_col3.collapsed)
            self.assertEqual(app.project_tabs.select(), str(app.project_tab_frames["locks"]))
        finally:
            root.destroy()

    def deselect_and_select(self, root, app, iid):
        """Выбор проекта заново: для панели это «другой проект» (автопроверка раз в несколько секунд им не является)."""
        app.mine_tree.selection_set(())
        root.update()
        app.mine_tree.selection_set(iid)
        root.update()

    def test_selecting_project_with_changes_opens_changes_tab(self):
        from svnhelper.svn_core import LocalChange, WcStatus
        root, app, _base = self.make_app("c4_auto")
        try:
            iid, item = self.select_first(root, app)
            self.pump(root, lambda: not app._checking and not item.checking and item.status is not None)
            panel = app.project_panel
            change = LocalChange(os.path.join(item.path, "a.txt"), "modified")
            for status, opens in ((WcStatus(), False), (WcStatus(local_changes=[change]), True),
                                  (WcStatus(conflicts=1, local_changes=[change]), True), (WcStatus(incoming=3), False)):
                with self.subTest(status=status):
                    item.status = status
                    panel.tabs.select(panel.frames["history"])
                    self.deselect_and_select(root, app, iid)
                    self.assertEqual(panel.current_tab(), "changes" if opens else "history")
            # автопроверка того же проекта вкладку не трогает
            item.status = WcStatus(local_changes=[change])
            panel.tabs.select(panel.frames["history"])
            app._on_item_status(item, WcStatus(local_changes=[change]), "")
            self.assertEqual(panel.current_tab(), "history")
        finally:
            root.destroy()

    def test_auto_changes_tab_waits_for_first_check_and_yields_to_user(self):
        from svnhelper.svn_core import LocalChange, WcStatus
        root, app, _base = self.make_app("c4_auto2")
        try:
            iid, item = self.select_first(root, app)
            self.pump(root, lambda: not app._checking and not item.checking and item.status is not None)
            panel = app.project_panel
            change = LocalChange(os.path.join(item.path, "a.txt"), "modified")
            # проверки ещё не было: ждём её и открываем «Изменения» по её результату
            item.status = None
            panel.tabs.select(panel.frames["locks"])
            self.deselect_and_select(root, app, iid)
            self.assertEqual(panel.current_tab(), "locks")
            app._on_item_status(item, WcStatus(local_changes=[change]), "")
            self.assertEqual(panel.current_tab(), "changes")
            # человек сам выбрал вкладку до результата проверки: не мешаем
            item.status = None
            panel.tabs.select(panel.frames["locks"])
            self.deselect_and_select(root, app, iid)
            panel.tabs.event_generate("<ButtonPress-1>", x=5, y=5)
            app._on_item_status(item, WcStatus(local_changes=[change]), "")
            self.assertEqual(panel.current_tab(), "locks")
            # курсор в поле комментария: вкладку не трогаем
            item.status = WcStatus(local_changes=[change])
            with mock.patch.object(app, "_typing_comment", return_value=True):
                self.deselect_and_select(root, app, iid)
            self.assertEqual(panel.current_tab(), "locks")
        finally:
            root.destroy()

    def test_double_click_opens_tab_of_main_action(self):
        from types import SimpleNamespace
        from svnhelper.svn_core import LocalChange, WcStatus
        root, app, _base = self.make_app("c4_dbl")
        try:
            iid, item = self.select_first(root, app)
            self.pump(root, lambda: not app._checking and not item.checking)
            change = LocalChange(os.path.join(item.path, "a.txt"), "modified")
            event = SimpleNamespace(x=5, y=5)
            cases = [("svn", WcStatus(local_changes=[change]), "changes"), ("svn", WcStatus(conflicts=1), "changes"),
                     ("svn", WcStatus(incoming=2), "incoming"), ("svn", WcStatus(), "incoming"),
                     ("broken", None, None)]
            with mock.patch.object(app.mine_tree, "identify_row", return_value=iid), \
                    mock.patch.object(app.mine_tree, "identify_region", return_value="cell"), \
                    mock.patch.object(app, "open_project_tab") as opened:
                for state, status, tab in cases:
                    with self.subTest(state=state, status=status):
                        self.show_state(app, item, state, status)
                        opened.reset_mock()
                        app.mine_col3.collapse()
                        app._on_mine_double_click(event)
                        if tab is None:                              # у починки вкладки нет: «Состояние» просто разворачивается
                            opened.assert_not_called()
                            self.assertFalse(app.mine_col3.collapsed)
                        else:
                            opened.assert_called_once_with(tab)
                app.mine_col3.collapse()
                opened.reset_mock()
                with mock.patch.object(app.mine_tree, "identify_region", return_value="heading"):
                    app._on_mine_double_click(event)                 # двойной щелчок по заголовку таблицы - не проект
                opened.assert_not_called()
                self.assertTrue(app.mine_col3.collapsed)
        finally:
            root.destroy()


class ProjectPanelTabsTests(unittest.TestCase):
    """C.3: колонка 3 - шапка и вкладки Новое / История / Занятые; смена проекта; «Нет связи» вместо ошибки."""

    setUpClass = MineTabTests.__dict__["setUpClass"]
    tearDownClass = MineTabTests.__dict__["tearDownClass"]
    pump = MineTabTests.pump
    checkout = MineTabTests.checkout
    make_app = MineTabTests.make_app
    select_first = MineTabTests.select_first
    FILE = MineTabTests.FILE

    def ready(self, name):
        """Окно на «Мои проекты», проект выбран и проверен; возвращает (root, app, item)."""
        root, app, _base = self.make_app(name)
        app.select_list_mode("mine")
        root.update()
        iid, item = self.select_first(root, app)
        self.pump(root, lambda: not app._checking and not item.checking and item.status is not None)
        return root, app, item

    def rows(self, view):
        return [view.tree.item(i, "values") for i in view.tree.get_children()]

    def test_tab_titles_header_and_no_stub_hints(self):
        root, app, item = self.ready("p_titles")
        try:
            self.assertEqual([app.project_tabs.tab(f, "text") for f in app.project_tabs.tabs()],
                             ["Изменения", "Новое", "История", "Занятые", "Ветки"])
            self.assertFalse(hasattr(app, "mine_detail") or hasattr(app, "mine_card") or hasattr(app, "detail_rows"))
            for key in ("incoming", "history", "locks"):          # в пустых вкладках нет подсказок-заглушек
                texts = [str(w.cget("text")) for w in self.all_labels(app.project_tab_frames[key])]
                self.assertFalse([t for t in texts if "по кнопке" in t or "через «Ещё" in t or "в подробностях" in t], texts)
            panel = app.project_panel
            self.assertEqual(panel.title_label.cget("text"), item.display_name)
            self.assertTrue(str(panel.status_label.cget("text")).startswith("● "))
            self.assertEqual(panel.branch_label.cget("text"), f"Ветка: Основная · сохранение № {item.revision}")
        finally:
            root.destroy()

    @staticmethod
    def all_labels(widget):
        from tkinter import ttk
        found = []
        for child in widget.winfo_children():
            if isinstance(child, (tk.Label, ttk.Label)):
                found.append(child)
            found += ProjectPanelTabsTests.all_labels(child)
        return found

    def test_incoming_tab_lists_what_others_saved(self):
        root, app, item = self.ready("p_incoming")
        _other_base, other = self.checkout("p_incoming_other")
        Path(other, self.FILE).write_text("от коллеги", encoding="utf-8")
        real.run("svn", "commit", "-m", "Правка коллеги", other, "--username", "petrov")
        try:
            app._check_status([item])
            self.pump(root, lambda: not app._checking and not item.checking and item.status.incoming)
            app.open_project_tab("incoming")
            view = app.project_panel.incoming_view
            self.pump(root, lambda: self.rows(view))
            who, when, what, files = self.rows(view)[0]
            self.assertEqual((who, what, files), ("petrov", "Правка коллеги", self.FILE))
            self.assertRegex(when, r"^\d\d\.\d\d\.\d{4} \d\d:\d\d$")
            self.assertEqual(view.message.cget("text"), "")
            self.assertIn("Ваши файлы не пострадают", view.note.cget("text"))
            with mock.patch.object(app.client, "incoming_log") as again:      # та же версия заново не читается
                app._load_project_tab()
                app._refresh_row(item)
            again.assert_not_called()
        finally:
            root.destroy()

    def test_incoming_empty_and_history_limit(self):
        root, app, item = self.ready("p_history")
        try:
            app.open_project_tab("incoming")
            view = app.project_panel.incoming_view
            self.pump(root, lambda: view.message.cget("text") == "На сервере нового нет.")
            self.assertEqual(self.rows(view), [])
            limits = []
            real_log = app.client.recent_log
            with mock.patch.object(app.client, "recent_log",
                                   side_effect=lambda path, limit, *a, **k: limits.append(limit) or real_log(path, limit, *a, **k)):
                app.open_project_tab("history")
                history = app.project_panel.history_view
                self.pump(root, lambda: self.rows(history))
            self.assertEqual(limits, [50])
            who, when, what, files = self.rows(history)[0]
            self.assertRegex(files, r"^\d+ файл")
        finally:
            root.destroy()

    def test_no_connection_is_a_message_not_an_error(self):
        from svnhelper import messages
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app, item = self.ready("p_offline")
        try:
            app.show_error.reset_mock()
            err = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013: Unable to connect")
            with mock.patch.object(app.client, "incoming_log", side_effect=err), \
                    mock.patch.object(app.client, "recent_log", side_effect=err), \
                    mock.patch.object(app.client, "list_lines", side_effect=err):
                for tab, view in (("incoming", app.project_panel.incoming_view),
                                  ("history", app.project_panel.history_view)):
                    app.open_project_tab(tab)
                    self.pump(root, lambda: view.message.cget("text") == messages.PANEL_OFFLINE)
                app.open_project_tab("lines")
                self.pump(root, lambda: app.lines_status.cget("text") == messages.PANEL_OFFLINE)
                self.assertEqual(app.lines_body.winfo_manager(), "")
            app.show_error.assert_not_called()
            # сбой запомнен: сам не повторяется, а когда связь вернулась - читается заново
            app.open_project_tab("incoming")
            with mock.patch.object(app.client, "incoming_log", return_value=[]) as retry:
                app._load_project_tab()
                retry.assert_not_called()
                app._set_offline(True)
                app._set_offline(False)
                app._load_project_tab()
                self.pump(root, lambda: app.project_panel.incoming_view.message.cget("text") == "На сервере нового нет.")
            # полоса «Нет связи» уже есть: вкладки сразу говорят «Нет связи», svn не запускается
            app._set_offline(True)
            with mock.patch.object(app.client, "recent_log") as never:
                app.open_project_tab("history")
                root.update()
                self.assertEqual(app.project_panel.history_view.message.cget("text"), messages.PANEL_OFFLINE)
            never.assert_not_called()
        finally:
            root.destroy()

    def test_switching_project_cancels_running_loads(self):
        from svnhelper.gui.models import MineItem
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app, item = self.ready("p_switch")
        try:
            seen = {}

            def slow_log(path, cancel=None):
                seen["cancel"] = cancel
                deadline = time.monotonic() + 10
                while not cancel.cancelled and time.monotonic() < deadline:
                    time.sleep(0.02)
                raise SvnError(ErrorKind.CANCELLED)

            view = app.project_panel.incoming_view
            with mock.patch.object(app.client, "incoming_log", side_effect=slow_log):
                app.open_project_tab("incoming")
                self.pump(root, lambda: "cancel" in seen)
                self.assertFalse(seen["cancel"].cancelled)
                self.assertEqual(view.message.cget("text"), "Ищу, что нового на сервере…")
                other = MineItem(os.path.join(self.tmp.name, "nowhere"), "nowhere", "plain")
                other_iid = app._insert_mine_row(other)
                app.mine[other_iid] = other
                app.mine_tree.selection_set(other_iid)
                root.update()
                self.assertTrue(seen["cancel"].cancelled)                   # загрузка прежнего проекта оборвана
                self.assertEqual(view.message.cget("text"), "Эта папка не подключена к серверу.")
                self.assertEqual(app.project_panel.title_label.cget("text"), "nowhere")
                time.sleep(0.2)
                root.update()                                                  # поздний ответ ничего не меняет
                self.assertEqual(view.message.cget("text"), "Эта папка не подключена к серверу.")
                self.assertEqual(self.rows(view), [])
        finally:
            root.destroy()

    def test_release_picker_does_not_preselect_files_with_unsaved_changes(self):
        """«Освободить…» без выбора: файлы с несохранёнными правками в окне заранее не отмечены и подписаны, иначе двумя
        щелчками освобождаются файлы, которые коллега тут же займёт, и правки разойдутся с его."""
        from svnhelper.svn_core import LocalChange, LockInfo, WcStatus
        root, app, item = self.ready("p_unlock_pick")
        try:
            saved, edited = (os.path.join(item.path, name) for name in ("сохранён.cdw", "правлю.cdw"))
            status = WcStatus(my_locks=[LockInfo(saved, "me"), LockInfo(edited, "me")],
                              local_changes=[LocalChange(edited, "modified", locked_by_me=True)])
            seen = {}

            class FakePick:
                def __init__(self, parent, title, prompt, items, preselect=None):
                    seen.update(items=list(items), preselect=preselect, prompt=prompt)
                    self.result = []
            with mock.patch("svnhelper.gui.projects_tab.PickDialog", FakePick), mock.patch.object(root, "wait_window"):
                app._choose_unlock(item, status)
            by_name = {name: label for label in seen["items"] for name in ("сохранён.cdw", "правлю.cdw") if name in label}
            self.assertNotIn("несохранённые правки", by_name["сохранён.cdw"])
            self.assertIn("(есть несохранённые правки)", by_name["правлю.cdw"])
            self.assertEqual([seen["items"][i] for i in seen["preselect"]], [by_name["сохранён.cdw"]])
            self.assertIn("не отмечены", seen["prompt"])
        finally:
            root.destroy()

    def test_pick_dialog_preselect(self):
        from svnhelper.gui.widgets import PickDialog
        root, app, item = self.ready("p_pick_dialog")
        try:
            with mock.patch.object(tk.Toplevel, "grab_set"):
                for preselect, expected in ((None, (0, 1, 2)), ([1], (1,)), ([], ())):
                    dialog = PickDialog(root, "t", "p", ["а", "б", "в"], preselect=preselect)
                    self.assertEqual(tuple(dialog._box.curselection()), expected, preselect)
                    dialog._ok()
                    self.assertEqual(dialog.result, [["а", "б", "в"][i] for i in expected])
        finally:
            root.destroy()

    def test_locks_tab_shows_locks_and_releases_own(self):
        root, app, item = self.ready("p_locks")
        _other_base, other = self.checkout("p_locks_other")
        other_file = os.path.join(other, self.FILE)
        try:
            panel = app.project_panel
            app.open_project_tab("locks")
            self.assertEqual(panel.locks_message.cget("text"), "Занятых файлов нет.")
            self.assertEqual(str(panel.lock_btn.cget("state")), "normal")
            real.run("svn", "lock", other_file, "-m", "editing", "--username", "petrov")
            app._check_status([item])
            self.pump(root, lambda: not app._checking and not item.checking and item.status.other_locks)
            tree = panel.locks_tree
            self.assertEqual([(tree.set(i, "file"), tree.set(i, "who")) for i in tree.get_children()],
                             [(self.FILE, "petrov")])
            tree.selection_set(tree.get_children()[0])
            root.update()
            self.assertEqual(str(panel.release_btn.cget("state")), "disabled")      # чужой файл освободить нельзя
            # свой занятый файл: фейковая проверка проекта, чтобы не зависеть от второго файла в тестовом хранилище
            from svnhelper.svn_core import LockInfo, WcStatus
            mine_file = os.path.join(item.path, "docs sub", "b.bin")
            item.status = WcStatus(my_locks=[LockInfo(mine_file, "me")])
            app._refresh_row(item)
            row = tree.get_children()[0]
            self.assertEqual((tree.set(row, "file"), tree.set(row, "who")), (os.path.join("docs sub", "b.bin"), "вы"))
            # ничего не выбрано: «Освободить…» доступна и открывает окно выбора своих файлов
            self.assertEqual(tree.selection(), ())
            self.assertEqual(str(panel.release_btn.cget("state")), "normal")
            self.assertEqual(panel.release_btn.cget("text"), "Освободить…")          # «…»: откроется окно выбора
            with mock.patch.object(app, "_on_release_lock") as choose, mock.patch.object(app, "_run_locks") as run:
                panel.release_btn.invoke()
            choose.assert_called_once_with()
            run.assert_not_called()
            tree.selection_set(row)
            root.update()
            self.assertEqual(str(panel.release_btn.cget("state")), "normal")
            self.assertEqual(panel.release_btn.cget("text"), "Освободить")           # без «…»: окна не будет, файл освобождается сразу
            with mock.patch.object(app, "_on_release_lock") as choose, mock.patch.object(app, "_run_locks") as run:
                panel.release_btn.invoke()
            run.assert_called_once_with(item, [mine_file], lock=False)           # выбрана своя строка: освобождает её
            choose.assert_not_called()
            with mock.patch.object(app, "_on_get_lock") as get_lock:                # «Занять файл…» в колонке кнопок
                panel.lock_btn.invoke()
            get_lock.assert_called_once_with()
        finally:
            real.run("svn", "unlock", other_file, "--username", "petrov")
            root.destroy()

    # колонка кнопок справа в панели «Состояние проекта»

    def side_texts(self, app):
        return [str(b.cget("text")) for b in app.project_panel.side_buttons()]

    def test_side_column_is_rebuilt_per_tab_and_has_documented_order(self):
        from svnhelper.gui.widgets import SideButtons
        root, app, item = self.ready("p_side_tabs")
        try:
            root.deiconify()                   # колонки лежат внутри вкладок: раскладка считается, только когда окно показано
            panel = app.project_panel
            root.update()
            panel.commit_panel.set_recent_comments(["Раз", "Два"])
            expected = {
                "changes": ["Сохранить отмеченное", "Правка чертежа", "Новая редакция КП", "Исправил по замечаниям",
                            "Последние ▾"],
                "incoming": [],
                "history": [],
                "locks": ["Занять файл…", "Освободить…"],
                "lines": ["Перейти", "Отличия", "Перенести правки…", "Новая ветка…", "Скачать отдельно",
                          "Зафиксировать версию…"],
            }
            for key, texts in expected.items():
                with self.subTest(tab=key):
                    app.open_project_tab(key)
                    root.update()
                    if key == "changes":      # у проекта без правок кнопки сохранения нет (как раньше)
                        self.assertEqual(self.side_texts(app), [])
                        panel.commit_panel.set_changes([self.make_change()])
                        root.update()
                    self.assertEqual(self.side_texts(app), texts)
                    if texts:
                        self.assertEqual(panel.side.winfo_manager(), "grid")
                        ys = [b.winfo_y() for b in panel.side_buttons()]
                        self.assertEqual(ys, sorted(ys))                  # кнопки друг под другом
                        self.assertEqual(len({b.winfo_x() for b in panel.side_buttons()}), 1)
                    else:                                                  # нет кнопок - нет колонки
                        self.assertEqual(panel.side.winfo_manager(), "")
            # подсвечена только «Сохранить отмеченное» (когда сохранение - главное действие); «Перейти» и остальные
            # обычного стиля
            self.assertEqual(str(app.switch_here_btn.cget("style")), "")
            self.assertEqual(str(panel.commit_panel.save_button.cget("style")), "TButton")   # у проекта без правок
            panel.set_save_accent(True)
            right = [b for key in ("changes", "locks", "lines") for b in panel._side_bars[key]._items
                     if b not in panel._side_bars[key]._hidden]
            self.assertEqual([str(b.cget("text")) for b in right if str(b.cget("style")) == "Accent.TButton"],
                             ["Сохранить отмеченное"])
            # под таблицами вкладок кнопок не осталось: все только в колонке справа, под списком правок
            # только поле комментария и ошибка
            for key in ("changes", "locks", "lines"):
                outside = [str(w.cget("text")) for w in self.all_buttons(app.project_tab_frames[key])
                           if not isinstance(w.master, SideButtons)]
                self.assertEqual(outside, [], (key, outside))
        finally:
            root.destroy()

    @staticmethod
    def make_change():
        from svnhelper.svn_core import LocalChange
        return LocalChange(path="a.txt", kind="modified")

    @staticmethod
    def all_buttons(widget):
        from tkinter import ttk
        found = []
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                found.append(child)
            found.extend(ProjectPanelTabsTests.all_buttons(child))
        return found

    def test_side_columns_hide_together_with_their_panels(self):
        root, app, item = self.ready("p_side_collapse")
        try:
            root.deiconify()
            root.geometry("1300x800")
            app.open_project_tab("lines")
            root.update()
            panel, left = app.project_panel, app.actions_row
            self.assertTrue(left.winfo_ismapped() and panel.side.winfo_ismapped())
            self.assertTrue(app.switch_here_btn.winfo_ismapped() and app.update_btn.winfo_ismapped())
            app.mine_col3.collapse()                       # «Состояние проекта» ›: уходит и правая колонка
            root.update()
            self.assertFalse(panel.side.winfo_ismapped() or app.switch_here_btn.winfo_ismapped())
            self.assertTrue(left.winfo_ismapped())         # а левая остаётся с «Проектами»
            app.mine_col2.collapse()                       # «Проекты» ‹: уходит и левая колонка
            root.update()
            self.assertFalse(left.winfo_ismapped() or app.update_btn.winfo_ismapped())
            app.mine_col2.expand()
            app.mine_col3.expand()
            root.update()
            self.assertTrue(left.winfo_ismapped() and panel.side.winfo_ismapped())
            self.assertEqual(panel.side.master, panel.frames["lines"])     # правая колонка лежит внутри своей вкладки
            self.assertTrue(str(panel.side).startswith(str(panel)))
        finally:
            root.destroy()

    def test_side_column_width_does_not_depend_on_window_width(self):
        root, app, item = self.ready("p_side_width")
        try:
            root.deiconify()
            panel = app.project_panel
            widths = {}
            for window in ("1000x700", "1500x800"):
                root.geometry(window)
                for key in ("locks", "lines"):
                    app.open_project_tab(key)
                    root.update()
                    widths[(window, key)] = [b.winfo_width() for b in panel.side_buttons()]
            self.assertEqual(len({w for ws in widths.values() for w in ws}), 1)    # везде одна ширина
            longest = max(b.winfo_reqwidth() for b in panel.side_buttons())
            self.assertGreaterEqual(panel.side_buttons()[0].winfo_width(), longest)  # самый длинный текст не обрезан
        finally:
            root.destroy()

    @staticmethod
    def tab_widths(tabs, px):
        """Ширина каждого ярлыка блокнота, как она нарисована (по identify вдоль строки ярлыков)."""
        spans = {}
        for x in range(tabs.winfo_width()):
            if tabs.identify(x, px(10)):
                index = tabs.index(f"@{x},{px(10)}")
                spans[index] = spans.get(index, 0) + 1
        return [spans[i] for i in sorted(spans)]

    def test_tab_labels_with_counters_are_not_clipped_at_every_scale(self):
        """U4.2: ярлыки «Изменения 12», «Новое 14» не обрезаны при 100/125/150% на каждой вкладке, в том числе там, где
        справа колонка кнопок (она лежит внутри вкладки и ширину у ярлыков не отнимает). Окно px(1000): ноутбук 1366 px
        при 125%. «Естественная» ширина ярлыка - в широком окне, где места с запасом."""
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale):
                root, app, _base = self.make_app(f"p_tabs_scale_{scale}", visible=True, scale=scale)
                try:
                    app.select_list_mode("mine")
                    iid, item = self.select_first(root, app)
                    self.pump(root, lambda: not app._checking and not item.checking and item.status is not None)
                    panel = app.project_panel
                    panel.set_changes_count(12)
                    panel.set_incoming_count(14)
                    widths = {}
                    for size in (1700, 1000):      # широкое окно: естественная ширина; px(1000): проверяемая
                        root.geometry(f"{app.px(size)}x{app.px(700)}")
                        for key in ("changes", "incoming", "history", "locks", "lines"):
                            app.open_project_tab(key)        # выбранный ярлык шире остальных: сравниваем при том же выборе
                            root.update()
                            widths[size, key] = self.tab_widths(panel.tabs, app.px)
                    for key in ("changes", "incoming", "history", "locks", "lines"):
                        self.assertEqual(len(widths[1700, key]), 5)
                        self.assertEqual(widths[1000, key], widths[1700, key], key)
                finally:
                    root.destroy()

    def test_side_buttons_fit_at_every_scale(self):
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale):
                root, app, _base = self.make_app(f"p_side_scale_{scale}", visible=True, scale=scale)
                try:
                    root.geometry("1410x950")
                    app.select_list_mode("mine")
                    iid, item = self.select_first(root, app)
                    self.pump(root, lambda: not app._checking and not item.checking and item.status is not None)
                    for key in ("locks", "lines"):
                        app.open_project_tab(key)
                        root.update()
                        buttons = app.project_panel.side_buttons()
                        self.assertTrue(buttons)
                        for button in buttons:
                            self.assertTrue(button.winfo_ismapped(), button.cget("text"))
                            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth(), button.cget("text"))
                            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                                 root.winfo_rootx() + root.winfo_width(), button.cget("text"))
                finally:
                    root.destroy()

    def test_broken_hint_matches_button(self):
        from svnhelper import messages
        from svnhelper.gui.models import MineItem
        root, app, item = self.ready("p_broken")
        try:
            broken = MineItem(os.path.join(self.tmp.name, "broken"), "broken", "broken")
            iid = app._insert_mine_row(broken)
            app.mine[iid] = broken
            app.mine_tree.selection_set(iid)
            root.update()
            hint = messages.status_line("broken", None, False)[2]
            self.assertIn(app.special_btn.cget("text"), hint)                       # «Починить папку» в подсказке и на кнопке
            self.assertIn(hint, app.project_panel.header_text())
        finally:
            root.destroy()


class TabLoadsTests(unittest.TestCase):
    """Загрузки вкладок колонки 3: повтор той же версии не нужен, смена проекта отменяет, сбой не зацикливается."""

    def test_markers_cancel_and_failures(self):
        from svnhelper.gui.project_panel import TabLoads
        loads = TabLoads()
        marker = ("p", 5, 7)
        self.assertTrue(loads.needed("incoming", marker))
        first = loads.start("incoming", marker)
        self.assertFalse(loads.needed("incoming", marker))                 # уже читается
        self.assertTrue(loads.needed("incoming", ("p", 5, 8)))             # на сервере появилось ещё - читаем заново
        second = loads.start("incoming", ("p", 5, 8))                      # новая загрузка обрывает прежнюю
        self.assertTrue(first.cancel.cancelled)
        self.assertFalse(loads.finish(first, True))                        # её ответ устарел
        self.assertTrue(loads.finish(second, True))
        self.assertFalse(loads.needed("incoming", ("p", 5, 8)))
        third = loads.start("history", marker)
        loads.reset()                                                      # другой проект
        self.assertTrue(third.cancel.cancelled)
        self.assertFalse(loads.finish(third, True))
        self.assertTrue(loads.needed("incoming", ("p", 5, 8)))
        failed = loads.start("history", marker)
        self.assertTrue(loads.finish(failed, False))
        self.assertFalse(loads.needed("history", marker))                  # сбой сам не повторяется
        loads.retry_failed()                                               # связь вернулась
        self.assertTrue(loads.needed("history", marker))


class CreateFolderTests(unittest.TestCase):
    """T.3: сборка проекта из шаблона в `.<имя>.creating`, остановка, откат и разбор остатков при запуске (без окна)."""

    def setUp(self):
        import test_templates as tt
        from svnhelper import projectmeta, templates
        from svnhelper.gui import new_project_dialog as npd
        self.tt, self.npd, self.projectmeta, self.templates = tt, npd, projectmeta, templates
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "проекты")
        os.makedirs(self.base)
        self.jobs = os.path.join(self.tmp.name, "jobs")
        self.template = tt.make_template(Path(self.tmp.name) / "tpl")
        manifest = templates.load_manifest(self.template)
        self.plan = templates.build_plan(manifest, {"date": "261028", "name": "вафли"},
                                         template_paths=templates.list_template_paths(self.template),
                                         base_dir=self.base)
        self.assertTrue(self.plan.ok, self.plan.errors)
        self.meta = projectmeta.new_meta("вафли", self.plan.folder)

    def create(self, cancel=None):
        from svnhelper.svn_core import CancelToken
        return self.npd.create_project_folder(self.base, self.plan, self.template, self.meta,
                                              cancel or CancelToken(), self.jobs)

    def leftovers(self):
        journal = sorted(Path(self.jobs).glob("*.json")) if os.path.isdir(self.jobs) else []
        return sorted(os.listdir(self.base)), journal

    def test_success_renames_and_cleans_journal(self):
        dest = self.create()
        self.assertEqual(os.path.basename(dest), "261028_вафли")
        self.assertTrue(os.path.isfile(os.path.join(dest, "README_261028_вафли.txt")))
        self.assertFalse(os.path.exists(os.path.join(dest, "template.json")))
        self.assertEqual(self.projectmeta.read(dest).folder, "261028_вафли")
        self.assertEqual(self.leftovers(), (["261028_вафли"], []))

    def test_cancel_before_start_and_in_middle_leave_nothing(self):
        from svnhelper.svn_core import CancelToken
        token = CancelToken()
        token.cancel()
        with self.assertRaises(self.npd.CreateCancelled):
            self.create(token)
        self.assertEqual(self.leftovers(), ([], []))
        token = CancelToken()
        real_apply = self.templates.apply_plan

        def apply_then_cancel(*args, **kwargs):
            real_apply(*args, **kwargs)
            token.cancel()
        with mock.patch.object(self.templates, "apply_plan", apply_then_cancel):
            with self.assertRaises(self.npd.CreateCancelled):
                self.create(token)
        self.assertEqual(self.leftovers(), ([], []))

    def test_failure_removes_only_ours_and_keeps_existing_folder(self):
        with mock.patch.object(self.projectmeta, "write", side_effect=OSError("диск полон")):
            with self.assertRaises(OSError):
                self.create()
        self.assertEqual(self.leftovers(), ([], []))
        os.makedirs(os.path.join(self.base, "261028_вафли"))
        Path(self.base, "261028_вафли", "моё.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertTrue(os.path.isfile(os.path.join(self.base, "261028_вафли", "моё.txt")))
        self.assertEqual(self.leftovers()[1], [])

    def test_killed_process_leftovers_removed_on_start(self):
        """Процесс убили посреди сборки: журнал и `.creating` остались, итоговой папки нет."""
        creating = self.npd.creating_path(self.base, "261028_вафли")
        job = self.npd.CreateJob("abc", self.base, creating, os.path.join(self.base, "261028_вафли"),
                                 path=os.path.join(self.jobs, "abc.json"))
        os.makedirs(os.path.join(creating, "01"))
        Path(creating, "ro.txt").write_text("x", encoding="utf-8")
        os.chmod(os.path.join(creating, "ro.txt"), 0o444)
        self.npd._save_job(job)
        self.assertEqual(self.npd.recover_create_jobs(self.jobs), 1)
        self.assertEqual(self.leftovers(), ([], []))

    def test_recover_during_build_skips_live_job(self):
        """Ревью п.2: разбор журналов посреди сборки (окно запустило его, пока идёт «Создать») не удаляет `.creating`
        живой задачи своего процесса: сборка доходит до конца."""
        real_apply, seen = self.templates.apply_plan, []

        def apply_and_recover(*args, **kwargs):
            real_apply(*args, **kwargs)
            seen.append(self.npd.recover_create_jobs(self.jobs))
            seen.append(os.path.isdir(self.npd.creating_path(self.base, "261028_вафли")))
        with mock.patch.object(self.templates, "apply_plan", apply_and_recover):
            dest = self.create()
        self.assertEqual(seen, [0, True])
        self.assertTrue(os.path.isfile(os.path.join(dest, "README_261028_вафли.txt")))
        self.assertEqual(self.leftovers(), (["261028_вафли"], []))

    def test_recovery_ignores_foreign_paths_and_finished_projects(self):
        ready = os.path.join(self.base, "готов")
        os.makedirs(ready)
        foreign = os.path.join(self.base, "обычная папка")
        os.makedirs(foreign)
        done = self.npd.CreateJob("d1", self.base, self.npd.creating_path(self.base, "готов"), ready,
                                  stage=self.npd.STAGE_DONE, path=os.path.join(self.jobs, "d1.json"))
        bad = self.npd.CreateJob("b1", self.base, foreign, foreign, path=os.path.join(self.jobs, "b1.json"))
        self.npd._save_job(done)
        self.npd._save_job(bad)
        Path(self.jobs, "other.json").write_text('{"kind": "publish"}', encoding="utf-8")
        self.npd.recover_create_jobs(self.jobs)
        self.assertEqual(sorted(os.listdir(self.base)), ["готов", "обычная папка"])
        self.assertEqual([p.name for p in Path(self.jobs).glob("*.json")], ["other.json"])

    def test_base_dir_checks(self):
        self.assertIsNone(self.npd.check_base_dir(self.base))
        self.assertIn("сетевом", self.npd.check_base_dir("\\\\server\\share\\x"))
        self.assertIsNotNone(self.npd.check_base_dir(""))
        self.assertIsNotNone(self.npd.check_base_dir(os.path.join(self.base, "нет такой")))


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class NewProjectDialogTests(unittest.TestCase):
    """T.3: окно «Новый проект» на заглушке клиента (шаблоны в памяти), без настоящего svn."""

    def setUp(self):
        import test_templates as tt
        self.tt = tt
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": os.path.join(self.tmp.name, "data")})
        env.start()
        self.addCleanup(env.stop)
        self.base = os.path.join(self.tmp.name, "проекты")
        os.makedirs(self.base)
        self.cache = Path(self.tmp.name) / "cache"
        self.jobs = Path(self.tmp.name) / "data" / "create_jobs"
        self.client = tt.FakeClient()
        self.client.cred_generation = 0        # нужно окну входа (JobStamp)
        manifest = tt.manifest_json(title="Электроника: проект", renames={"a.txt": "{{folder}}.txt"})
        self.client.add("elec", title="Электроника: проект", manifest=manifest.encode("utf-8"))

    def pump(self, root, cond, timeout=20):
        end = time.monotonic() + timeout
        while not cond():
            root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), end, "таймаут ожидания")

    def make_app(self, templates_root=None, support="", recover=False, scale=None, visible=False):
        """recover: разбор журналов и подготовка шаблонов при запуске, как у настоящей программы. По умолчанию выключены:
        иначе разбор шёл бы одновременно с задачами самого теста (тот же data_dir()/create_jobs).
        scale - масштаб экрана Windows (1.25 = 125%), visible - показать окно на экране (для проверки раскладки)."""
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        root = tk.Tk()
        if scale is not None:
            root.tk.call("tk", "scaling", 96 * scale / 72)
        if not visible:
            root.withdraw()
        quiet_clipboard(root)
        self.addCleanup(lambda: root.winfo_exists() and root.destroy())
        cfg = Config(server="svn://srv", repos=["konstruktor"], work_dir=self.base, support=support,
                     templates_root=self.tt.ROOT_URL if templates_root is None else templates_root)
        store = SettingsStore(Path(self.tmp.name) / "settings.json")
        app = App(root, cfg, store, SvnClient("svn"), os.path.join(self.tmp.name, "log.txt"), startup_jobs=recover)
        app.client = self.client
        app._mine_dirty = False
        return root, app

    def open_dialog(self, app, storage="local"):
        """Окно «Новый проект». Тесты T.3 идут по пути «на компьютере»; storage=None оставляет выбор по умолчанию."""
        from svnhelper.gui.new_project_dialog import NewProjectDialog
        dialog = NewProjectDialog(app, self.base, cache_dir=self.cache, jobs_dir=self.jobs)
        self.pump(app.root, lambda: dialog.loaded is not None or dialog.state == "ready")
        if storage is not None:
            dialog.storage_var.set(storage)
        return dialog

    def test_button_is_last_in_column_one_and_not_highlighted(self):
        """U4.2: «＋ Новый проект…» внизу колонки кнопок, полной ширины, обычного стиля (подсвечена одна кнопка:
        главное действие); отдельной кнопки «Скачать проект» в колонке нет, скачивание - подвкладка «Проекты»."""
        root, app = self.make_app()
        self.assertEqual(app.new_project_btn.cget("text"), "＋ Новый проект…")
        self.assertEqual(str(app.new_project_btn.nametowidget(app.new_project_btn.winfo_parent())), str(app.actions_row))
        items = app.actions_row._items
        self.assertIs(items[-1], app.new_project_btn)
        self.assertFalse(hasattr(app, "goto_download_btn"))
        self.assertEqual(str(app.new_project_btn.cget("style")), "TButton")
        self.assertEqual(app.new_project_btn.pack_info()["fill"], "x")
        self.assertNotIn(str(app.new_project_btn), [str(w) for w in app.rescan_btn.master.winfo_children()])

    def assert_fits(self, widget, container, name):
        """Кнопка показана и целиком внутри своего ряда (и внутри колонки, в которой лежит ряд)."""
        self.assertTrue(widget.winfo_ismapped(), f"{name}: не показана")
        parent = widget.nametowidget(widget.winfo_parent())
        self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), parent.winfo_width(), f"{name}: шире ряда")
        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), container.winfo_rootx() + container.winfo_width(),
                             f"{name}: выходит за колонку")
        self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), f"{name}: подпись обрезана")

    def test_buttons_are_visible_at_every_screen_scale(self):
        """Ревью 2026-11-02, п.1 (blocker): окно 1410x950, масштабы 100/125/150%: все кнопки колонок 1 и 2 и ряды кнопок
        окон «Новый проект» и «Отправить на сервер» показаны целиком (раньше «＋ Новый проект…» не помещалась и пропадала)."""
        from svnhelper.gui.new_project_dialog import PublishDialog
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale):
                root, app = self.make_app(scale=scale, visible=True)
                os.makedirs(os.path.join(self.base, "обычная"), exist_ok=True)
                root.geometry("1410x950")
                app.select_list_mode("mine")
                root.update()
                col1 = [w for w in app.actions_row._items if w not in app.actions_row._hidden]
                self.assertIn(app.new_project_btn, col1)
                for widget in col1:
                    self.assert_fits(widget, app.actions_row, f"колонка 1: {widget.cget('text') or widget}")
                for widget in (app.mine_browse_btn, app.rescan_btn):
                    self.assert_fits(widget, app.mine_col2, f"колонка 2: {widget.cget('text')}")
                dialog = self.open_dialog(app)
                root.update()
                for widget in (dialog.create_btn, dialog.close_btn):
                    self.assert_fits(widget, dialog, f"Новый проект: {widget.cget('text')}")
                dialog.destroy()
                publish = PublishDialog(app, os.path.join(self.base, "обычная"), jobs_dir=self.jobs)
                root.update()
                for widget in (publish.send_btn, publish.close_btn):
                    self.assert_fits(widget, publish, f"Отправить на сервер: {widget.cget('text')}")
                publish.destroy()
                root.withdraw()           # окно закроет addCleanup; следующий масштаб - в новом окне

    def test_preview_follows_input_and_create_is_blocked_on_errors(self):
        root, app = self.make_app()
        dialog = self.open_dialog(app)
        self.assertIsNotNone(dialog.loaded)
        self.assertEqual(str(dialog.create_btn.cget("state")), "disabled")          # название не заполнено
        dialog.field_vars["date"].set("261028")
        dialog.field_vars["name"].set("отбраковщик вафель")
        self.assertEqual(dialog.preview_var.get(),
                         f"Будет создано: {os.path.join(self.base, '261028_отбраковщик вафель')}")
        self.assertEqual(str(dialog.create_btn.cget("state")), "normal")
        dialog.field_vars["name"].set("сушилка")
        self.assertTrue(dialog.preview_var.get().endswith("261028_сушилка"))
        dialog.field_vars["name"].set("a:b")                                          # запрещённый символ
        self.assertEqual(str(dialog.create_btn.cget("state")), "disabled")
        self.assertIn(":", dialog.problem_label.cget("text"))
        dialog.field_vars["name"].set("x")
        self.assertEqual(str(dialog.create_btn.cget("state")), "normal")
        self.assertEqual(dialog.warn_label.cget("text"), "")                          # длина пути в норме: ничего не пишем
        dialog.base_var.set("\\\\server\\share")                                      # сетевой диск
        self.assertEqual(str(dialog.create_btn.cget("state")), "disabled")
        self.assertIn("сетевом", dialog.problem_label.cget("text"))
        dialog.destroy()

    def test_create_selects_new_project_in_list(self):
        root, app = self.make_app()
        dialog = self.open_dialog(app)
        dialog.field_vars["date"].set("261028")
        dialog.field_vars["name"].set("отбраковщик вафель")
        dialog.create_btn.invoke()
        self.pump(root, lambda: dialog.result is not None)
        dest = os.path.join(self.base, "261028_отбраковщик вафель")
        self.assertEqual(dialog.result, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "261028_отбраковщик вафель.txt")))
        self.assertTrue(os.path.isdir(os.path.join(dest, "пусто")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "project.json")))
        self.assertEqual(sorted(os.listdir(self.base)), ["261028_отбраковщик вафель"])
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        # проект в списке и выделен, хотя выше по алфавиту есть другая папка
        Path(self.base, "aaa").mkdir()
        app.dir_var.set(self.base)
        app._show_new_project(dest)
        self.pump(root, lambda: len(app.mine) == 2 and app.mine_tree.selection())
        selected = app.mine[app.mine_tree.selection()[0]]
        self.assertEqual(os.path.normcase(selected.path), os.path.normcase(dest))

    def test_scan_skips_dot_folders(self):
        root, app = self.make_app()
        for name in (".x.creating", "обычный"):
            Path(self.base, name).mkdir()
        app.dir_var.set(self.base)
        app._scan_projects()
        self.pump(root, lambda: app._mine_scanned and len(app.mine) >= 1)
        self.assertEqual([i.name for i in app.mine.values()], ["обычный"])

    def test_works_from_cache_without_connection(self):
        root, app = self.make_app()
        dialog = self.open_dialog(app)                     # со связью: шаблон ложится в кэш
        self.assertEqual(dialog.source_var.get(), "")
        dialog.destroy()
        self.client.online = False
        dialog = self.open_dialog(app)
        self.assertIsNotNone(dialog.loaded)
        self.assertRegex(dialog.source_var.get(),
                         r"^Шаблон от \d\d\.\d\d\.\d{4} \(нет связи: взят сохранённый на компьютере\)$")
        self.assertIn("Нет связи с сервером", dialog.local_hint.cget("text"))
        dialog.field_vars["name"].set("из кэша")
        dialog.create_btn.invoke()
        self.pump(root, lambda: dialog.result is not None)
        self.assertTrue(os.path.isdir(dialog.result))

    def test_no_cache_and_no_connection_says_so(self):
        root, app = self.make_app()
        self.client.online = False
        dialog = self.open_dialog(app)
        self.assertIn("Нет связи с сервером", dialog.status_var.get())
        self.assertEqual(str(dialog.create_btn.cget("state")), "disabled")
        dialog.destroy()

    def test_templates_not_configured_message(self):
        root, app = self.make_app(templates_root=" ", support="Иванов, доб. 123")
        dialog = self.open_dialog(app)
        self.assertTrue(dialog.status_var.get().startswith("Шаблоны не настроены, обратитесь к администратору"))
        self.assertIn("Иванов", dialog.status_var.get())
        self.assertEqual(str(dialog.create_btn.cget("state")), "disabled")
        dialog.destroy()

    def test_existing_project_offers_to_open_it(self):
        root, app = self.make_app()
        dialog = self.open_dialog(app)
        os.makedirs(os.path.join(self.base, "261028_вафли"))
        dialog.field_vars["date"].set("261028")
        dialog.field_vars["name"].set("вафли")
        self.assertEqual(dialog.problem_label.cget("text"),
                         "Проект с таким названием уже есть в этой папке. Открыть его?")
        self.assertEqual(str(dialog.create_btn.cget("state")), "disabled")
        dialog.open_existing_btn.invoke()
        self.assertEqual(dialog.result, os.path.join(self.base, "261028_вафли"))

    def test_server_storage_is_default_with_connection(self):
        root, app = self.make_app()
        dialog = self.open_dialog(app, storage=None)
        self.assertIn("На общем сервере", dialog.server_radio.cget("text"))
        self.assertIn("Пока только на этом компьютере", dialog.local_radio.cget("text"))
        self.assertEqual(str(dialog.server_radio.cget("state")), "normal")
        self.assertEqual(dialog.server_hint.cget("text"), "")
        self.assertEqual(dialog.storage_var.get(), "server")
        dialog.destroy()

    def test_server_storage_is_unavailable_without_connection(self):
        root, app = self.make_app()
        self.open_dialog(app).destroy()                    # шаблон ложится в кэш
        self.client.online = False
        dialog = self.open_dialog(app, storage=None)
        self.assertEqual(str(dialog.server_radio.cget("state")), "disabled")
        self.assertIn("Нет связи с сервером", dialog.server_hint.cget("text"))
        self.assertIn("Нет связи с сервером", dialog.local_hint.cget("text"))
        self.assertEqual(dialog.storage_var.get(), "local")
        dialog.destroy()

    def test_closing_in_the_middle_stops_and_leaves_nothing(self):
        from svnhelper import templates
        root, app = self.make_app()
        dialog = self.open_dialog(app)
        dialog.field_vars["name"].set("прервём")
        entered, release = threading.Event(), threading.Event()
        real_apply = templates.apply_plan

        def slow_apply(*args, **kwargs):
            real_apply(*args, **kwargs)
            entered.set()
            release.wait(10)
        with mock.patch.object(templates, "apply_plan", slow_apply), \
                mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask:
            dialog.create_btn.invoke()
            self.pump(root, lambda: entered.is_set())
            self.assertEqual(dialog.state, "running")
            self.assertTrue(app._new_project_running() is False)     # окно ещё не зарегистрировано в App
            app._new_project_dialog = dialog
            self.assertTrue(app._new_project_running())
            dialog._on_close()                              # крестик посреди создания: вопрос, остановка
            ask.assert_called_once()
            self.assertEqual(ask.call_args.args[0], "Остановить создание?")
            release.set()
            self.pump(root, lambda: dialog.state == "ready")
        self.assertIsNone(dialog.result)
        self.assertEqual(os.listdir(self.base), [])
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        dialog.destroy()

    def test_startup_removes_leftover_creating_folder(self):
        from svnhelper.gui import new_project_dialog as npd
        creating = npd.creating_path(self.base, "261028_убитый")
        os.makedirs(os.path.join(creating, "01"))
        npd._save_job(npd.CreateJob("k1", self.base, creating, os.path.join(self.base, "261028_убитый"),
                                    path=str(self.jobs / "k1.json")))
        root, app = self.make_app(recover=True)
        self.pump(root, lambda: not os.path.exists(creating))
        self.pump(root, lambda: not list(self.jobs.glob("*.json")))
        self.assertEqual(os.listdir(self.base), [])



def _make_fake_server_client():
    """Заглушка клиента с сервером в памяти: шаблоны (из test_templates) и проекты (папки, файлы, журнал правок)."""
    import test_templates as tt
    from urllib.parse import unquote
    from svnhelper import server
    from svnhelper.svn_core import ErrorKind, ListEntry, LogEntry, SvnError

    class FakeServerClient(tt.FakeClient):
        def __init__(self):
            super().__init__()
            self.cred_generation = 0
            self.credentials = None
            self.dirs: set = set()
            self.files: dict = {}
            self.revision = 1
            self.log: list = []
            self.deny_write = False
            self.fail_import = None
            self.fail_checkout = None
            self.import_gate = None          # Event: загрузка ждёт его (проверка остановки посреди загрузки)
            self.checkout_gate = None        # Event: скачивание ждёт его
            self.list_gate = None            # Event: чтение списка папок ждёт его
            self.fail_remove = None          # откат (удаление папок на сервере) не удаётся
            self.fail_mkdir = None
            self.on_checkout = None          # вызывается в начале скачивания
            self.after_import = None         # вызывается после загрузки, до возврата
            self.exists_error = None         # url_exists падает с этой ошибкой (проверка имени в окне)
            self.add_dirs(server.build_url("svn://srv", ["konstruktor", "projects"]))

        def add_dirs(self, url):
            prefix = "svn://srv"
            parts = url[len(prefix):].strip("/").split("/")
            for i in range(1, len(parts) + 1):
                self.dirs.add(prefix + "/" + "/".join(parts[:i]))

        def url_exists(self, url):
            self._down()
            if self.exists_error is not None:
                raise self.exists_error
            return url.rstrip("/@") in self.dirs

        def list_entries(self, url, recursive=False, cancel=None, timeout=None):
            if url.endswith("@") and url[:-1] in self.dirs:
                prefix = url[:-1] + "/"
                return [ListEntry("file", f[len(prefix):]) for f in self.files if f.startswith(prefix)]
            return super().list_entries(url, recursive, cancel, timeout)

        def list_dirs(self, url, cancel=None):
            self._down()
            if self.list_gate is not None:
                self.list_gate.wait(10)
            prefix = url.rstrip("/") + "/"
            return sorted({unquote(d[len(prefix):].split("/")[0]) for d in self.dirs if d.startswith(prefix)})

        def _log(self, project_url, message):
            self.log.append((project_url, LogEntry(self.revision, "ivanov", None, message)))

        def mkdir_urls(self, urls, message):
            self._down()
            if self.deny_write:
                raise SvnError(ErrorKind.AUTHZ, "svn: E220004: Access denied")
            if self.fail_mkdir is not None:
                raise self.fail_mkdir
            self.revision += 1
            for url in urls:
                self.add_dirs(url)
            self._log(urls[0].rsplit("/", 1)[0], message)

        def last_log_entry(self, url, limit=1):
            return [e for u, e in reversed(self.log) if u == url.rstrip("/")][:limit]

        def import_dir(self, src, url, message, ignores, progress=None, cancel=None):
            self._down()
            if self.import_gate is not None:
                self.import_gate.wait(10)
            if cancel is not None and cancel.cancelled:
                raise SvnError(ErrorKind.CANCELLED)
            if self.fail_import is not None:
                raise self.fail_import
            count = 0
            for dirpath, dirnames, filenames in os.walk(src):
                rel = [] if os.path.relpath(dirpath, src) == "." else os.path.relpath(dirpath, src).split(os.sep)
                for name in dirnames:
                    self.add_dirs(server.build_url(url, [*rel, name]))
                    count += 1
                for name in filenames:
                    self.files[server.build_url(url, [*rel, name])] = Path(dirpath, name).read_bytes()
                    count += 1
            if progress is not None:
                progress.count = count
            self.revision += 1
            self._log(url.rsplit("/", 1)[0], message)
            if self.after_import is not None:
                self.after_import()
            return count

        def remove_url(self, url, message):
            if self.fail_remove is not None:
                raise self.fail_remove
            url = url.rstrip("/@")
            self.dirs = {d for d in self.dirs if d != url and not d.startswith(url + "/")}
            self.files = {f: data for f, data in self.files.items() if not f.startswith(url + "/")}
            self.log = [(u, e) for u, e in self.log if u != url]
            self.revision += 1

        def checkout(self, url, dest, progress=None, cancel=None, force=False, timeout=None):
            self._down()
            if self.on_checkout is not None:
                self.on_checkout()
            if self.checkout_gate is not None:
                self.checkout_gate.wait(10)
                if cancel is not None and cancel.cancelled:
                    raise SvnError(ErrorKind.CANCELLED)
            if self.fail_checkout is not None:
                raise self.fail_checkout
            os.makedirs(dest, exist_ok=True)
            prefix = url.rstrip("/") + "/"
            for d in sorted(self.dirs):
                if d.startswith(prefix):
                    os.makedirs(os.path.join(dest, *unquote(d[len(prefix):]).split("/")), exist_ok=True)
            for f, data in self.files.items():
                if f.startswith(prefix):
                    Path(dest, *unquote(f[len(prefix):]).split("/")).write_bytes(data)

    return FakeServerClient()


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class NewProjectServerTests(unittest.TestCase):
    """T.5: «Сразу на сервер» на заглушке клиента: сборка, загрузка, обычное скачивание, откат, нет прав."""

    setUp_base = NewProjectDialogTests.setUp
    pump = NewProjectDialogTests.pump
    make_app = NewProjectDialogTests.make_app
    open_dialog = NewProjectDialogTests.open_dialog

    def setUp(self):
        self.setUp_base()
        from svnhelper import server
        self.server = server
        self.client = _make_fake_server_client()
        self.client.add("elec", title="Электроника: проект",
                        manifest=self.tt.manifest_json(title="Электроника: проект",
                                                       renames={"a.txt": "{{folder}}.txt"}).encode("utf-8"))
        self.project_url = server.build_url("svn://srv", ["konstruktor", "projects", "261028_вафли"])
        self.dest = os.path.join(self.base, "261028_вафли")

    def ready_dialog(self, app, name="вафли"):
        dialog = self.open_dialog(app, storage=None)
        dialog.field_vars["date"].set("261028")
        dialog.field_vars["name"].set(name)
        self.pump(app.root, lambda: self.project_url in dialog.server_state)
        return dialog

    def trunk(self, name):
        return self.server.build_url(self.project_url, ["trunk", name])

    def test_default_is_server_with_place_from_template(self):
        root, app = self.make_app()
        dialog = self.open_dialog(app, storage=None)
        self.assertEqual(dialog.storage_var.get(), "server")
        self.assertEqual(dialog.server_segments, ["konstruktor", "projects"])
        dialog.field_vars["date"].set("261028")
        dialog.field_vars["name"].set("вафли")
        self.assertIn("Будет на сервере: konstruktor/projects/261028_вафли", dialog.place_var.get())
        self.pump(root, lambda: self.project_url in dialog.server_state)
        self.assertEqual(dialog.place_var.get(), "Будет на сервере: konstruktor/projects/261028_вафли")
        self.assertEqual(str(dialog.create_btn.cget("state")), "normal")
        dialog.storage_var.set("local")
        self.assertEqual(dialog.place_var.get(), "")
        dialog.destroy()

    def test_create_uploads_then_downloads_normally(self):
        root, app = self.make_app()
        dialog = self.ready_dialog(app)
        dialog.create_btn.invoke()
        self.pump(root, lambda: dialog.result is not None)
        self.assertEqual(dialog.result, self.dest)
        # на сервере: три папки, файлы шаблона с проектными именами, project.json с адресом на сервере
        for part in ("trunk", "branches", "tags"):
            self.assertIn(f"{self.project_url}/{part}", self.client.dirs)
        self.assertEqual(self.client.files[self.trunk("261028_вафли.txt")], b"hello")
        import json
        on_server = json.loads(self.client.files[self.trunk("project.json")].decode("utf-8"))
        self.assertEqual(on_server["svn"]["url"], self.project_url)
        # у человека: обычное скачивание в итоговую папку, никаких временных папок и журналов
        self.assertEqual(Path(self.dest, "261028_вафли.txt").read_bytes(), b"hello")
        self.assertTrue(os.path.isdir(os.path.join(self.dest, "пусто")))
        self.assertEqual(json.loads(Path(self.dest, "project.json").read_text("utf-8"))["svn"]["url"], self.project_url)
        self.assertEqual(os.listdir(self.base), ["261028_вафли"])
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_no_rights_keeps_local_project(self):
        from svnhelper.gui.new_project_dialog import NO_RIGHTS_TEXT
        root, app = self.make_app()
        self.client.deny_write = True
        dialog = self.ready_dialog(app)
        with mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.create_btn.invoke()
            self.pump(root, lambda: dialog.result is not None)
        self.assertEqual(info.call_args.args[1], NO_RIGHTS_TEXT)
        self.assertEqual(NO_RIGHTS_TEXT, "Нет прав создавать проекты в этой папке сервера. Проект создан на "
                                         "компьютере: отправить можно позже или попросить администратора.")
        self.assertEqual(dialog.result, self.dest)
        import json
        self.assertIsNone(json.loads(Path(self.dest, "project.json").read_text("utf-8"))["svn"])
        self.assertEqual(Path(self.dest, "261028_вафли.txt").read_bytes(), b"hello")
        self.assertNotIn(self.project_url, self.client.dirs)
        self.assertEqual(os.listdir(self.base), ["261028_вафли"])
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_failed_upload_rolls_server_back_and_leaves_nothing(self):
        from svnhelper.gui import App
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        self.client.fail_import = SvnError(ErrorKind.OTHER, "svn: E165001: сбой на сервере")
        dialog = self.ready_dialog(app)
        App.show_error.reset_mock()
        dialog.create_btn.invoke()
        self.pump(root, lambda: dialog.state == "ready")
        self.assertIsNone(dialog.result)
        self.assertNotIn(self.project_url, self.client.dirs)           # папки созданы и убраны откатом
        self.assertEqual(os.listdir(self.base), [])
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        self.assertEqual(App.show_error.call_args.kwargs["kind"], ErrorKind.OTHER)
        dialog.destroy()

    # -- ревью 2026-11-02 п.6: связь пропала - собранный проект остаётся на компьютере ----------------------------

    def create_expecting_local(self, app, before_create=None):
        dialog = self.ready_dialog(app)
        if before_create is not None:
            before_create()
        with mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.create_btn.invoke()
            self.pump(app.root, lambda: dialog.result is not None)
        self.assertEqual(dialog.result, self.dest)
        self.assertEqual(info.call_args.args[0], "Проект создан на компьютере")
        import json
        self.assertIsNone(json.loads(Path(self.dest, "project.json").read_text("utf-8"))["svn"])  # без адреса сервера
        self.assertEqual(Path(self.dest, "261028_вафли.txt").read_bytes(), b"hello")
        self.assertEqual(os.listdir(self.base), ["261028_вафли"])
        return info.call_args.args[1]

    def test_no_connection_during_upload_keeps_project_on_computer(self):
        from svnhelper.gui.new_project_dialog import NO_LINK_KEEP_TEXT
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        self.client.fail_import = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013")
        text = self.create_expecting_local(app)
        self.assertEqual(text, NO_LINK_KEEP_TEXT)
        self.assertEqual(NO_LINK_KEEP_TEXT, "Нет связи с сервером: проект создан на компьютере. Отправьте его на "
                                            "сервер позже: «Отправить на сервер…».")
        self.assertNotIn(self.project_url, self.client.dirs)           # каркас откатан
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_no_connection_before_upload_keeps_project_on_computer(self):
        from svnhelper.gui.new_project_dialog import NO_LINK_KEEP_TEXT
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()

        def lose_connection():
            self.client.exists_error = SvnError(ErrorKind.TIMEOUT, "svn: E175012: Connection timed out")
        text = self.create_expecting_local(app, lose_connection)
        self.assertEqual(text, NO_LINK_KEEP_TEXT)
        self.assertNotIn(self.project_url, self.client.dirs)           # на сервер не заходили
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_no_connection_with_failed_rollback_keeps_journal_until_next_start(self):
        """Ревью п.3 и п.6: каркас убрать не вышло - проект на компьютере, журнал на стадии uploading; разбор при
        следующем запуске каркас убирает, а готовую папку на компьютере не трогает."""
        from svnhelper import publish
        from svnhelper.gui import new_project_dialog as npd
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        self.client.fail_import = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013")
        self.client.fail_remove = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013")
        text = self.create_expecting_local(app)
        self.assertIn(publish.ROLLBACK_LATER_TEXT, text)
        self.assertIn(self.project_url + "/trunk", self.client.dirs)    # каркас пока на сервере
        self.assertEqual(read_job_stage(self.jobs), "uploading")
        self.assertTrue(read_job(self.jobs)["layout_revision"])
        self.client.fail_remove = None
        npd.recover_create_jobs(self.jobs, client=self.client, landed=[])
        self.assertNotIn(self.project_url, self.client.dirs)
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        self.assertEqual(Path(self.dest, "261028_вафли.txt").read_bytes(), b"hello")   # проект на компьютере цел

    def test_stop_in_the_middle_of_upload_rolls_back(self):
        import threading
        root, app = self.make_app()
        self.client.import_gate = gate = threading.Event()
        dialog = self.ready_dialog(app)
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            dialog.create_btn.invoke()
            self.pump(root, lambda: self.project_url + "/trunk" in self.client.dirs)    # папки уже на сервере
            self.assertEqual(dialog.state, "running")
            dialog._on_close()
        gate.set()
        self.pump(root, lambda: dialog.state == "ready")
        self.assertIsNone(dialog.result)
        self.assertNotIn(self.project_url, self.client.dirs)
        self.assertEqual(os.listdir(self.base), [])
        dialog.destroy()

    def test_download_failure_keeps_project_on_server(self):
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        self.client.fail_checkout = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013")
        dialog = self.ready_dialog(app)
        with mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.create_btn.invoke()
            self.pump(root, lambda: dialog.state == "ready" and info.call_count)
        self.assertIn("создан на сервере", info.call_args.args[1])
        self.assertIn("«Скачать проект»", info.call_args.args[1])
        self.assertIn(self.project_url + "/trunk", self.client.dirs)    # готовый проект остаётся
        self.assertEqual(os.listdir(self.base), [])                     # частично скачанного нет
        self.assertEqual(dialog.problem_label.cget("text"), self.server_exists_text())
        dialog.destroy()

    def server_exists_text(self):
        from svnhelper import publish
        return publish.MESSAGES[publish.EXISTS]

    def test_preview_checks_server_name_and_place(self):
        from svnhelper import publish
        root, app = self.make_app()
        self.client.add_dirs(self.project_url)                           # имя на сервере уже занято
        dialog = self.ready_dialog(app)
        self.assertEqual(dialog.problem_label.cget("text"), publish.MESSAGES[publish.EXISTS])
        self.assertEqual(str(dialog.create_btn.cget("state")), "disabled")
        dialog.storage_var.set("local")                                  # на компьютере это не мешает
        self.assertEqual(str(dialog.create_btn.cget("state")), "normal")
        dialog.storage_var.set("server")
        dialog.server_segments = ["konstruktor", "нет такого"]
        dialog._refresh()
        missing = self.server.build_url("svn://srv", ["konstruktor", "нет такого", "261028_вафли"])
        self.pump(root, lambda: missing in dialog.server_state)
        self.assertEqual(dialog.problem_label.cget("text"), publish.MESSAGES[publish.NO_PARENT])
        self.assertEqual(str(dialog.create_btn.cget("state")), "disabled")
        dialog.destroy()

    def test_change_place_uses_picker_result(self):
        root, app = self.make_app()
        dialog = self.ready_dialog(app)

        class FakePlace:
            def __init__(self, app, current, parent=None):
                self.result = ["konstruktor", "other"]
        with mock.patch("svnhelper.gui.new_project_dialog.ServerPlaceDialog", FakePlace), \
                mock.patch.object(dialog, "wait_window"):
            dialog.place_btn.invoke()
        self.assertEqual(dialog.server_segments, ["konstruktor", "other"])
        self.assertIn("konstruktor/other/261028_вафли", dialog.place_var.get())
        dialog.destroy()

    def test_template_without_place_asks_to_choose(self):
        root, app = self.make_app()
        self.client.add("elec", title="Электроника: проект",
                        manifest=self.tt.manifest_json(title="Электроника: проект", parent="").encode("utf-8"))
        dialog = self.open_dialog(app, storage=None)
        dialog.field_vars["name"].set("вафли")
        self.assertEqual(dialog.server_segments, [])
        self.assertIn("Изменить", dialog.problem_label.cget("text"))
        self.assertEqual(str(dialog.create_btn.cget("state")), "disabled")
        dialog.destroy()

    def test_place_dialog_uses_shared_tree(self):
        from svnhelper.gui.server_tree import ServerPlaceDialog, PLACE_IS_PROJECT
        root, app = self.make_app()
        self.client.add_dirs(self.server.build_url("svn://srv", ["konstruktor", "projects", "старый", "trunk"]))
        dialog = ServerPlaceDialog(app, ["konstruktor", "projects"])
        tree = dialog.tree
        first = tree.tree.get_children()[0]
        tree.tree.item(first, open=True)
        tree.tree.focus(first)
        tree.tree.event_generate("<<TreeviewOpen>>")
        self.pump(root, lambda: tree.nodes[first].state == "loaded")
        projects = tree.tree.get_children(first)[0]
        tree.tree.item(projects, open=True)
        tree.tree.focus(projects)
        tree.tree.event_generate("<<TreeviewOpen>>")
        self.pump(root, lambda: tree.nodes[projects].state == "loaded")
        old = tree.tree.get_children(projects)[0]
        self.assertIn("✔", tree.tree.item(old, "text"))
        tree.tree.selection_set(old)
        root.update()
        self.assertEqual(dialog.place_var.get(), PLACE_IS_PROJECT)           # в проект класть нельзя
        self.assertEqual(str(dialog.ok_btn.cget("state")), "disabled")
        tree.tree.selection_set(projects)
        root.update()
        self.assertEqual(str(dialog.ok_btn.cget("state")), "normal")
        dialog.ok_btn.invoke()
        self.assertEqual(dialog.result, ["konstruktor", "projects"])


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class PublishGuiTests(unittest.TestCase):
    """T.5: «Ещё → Отправить на сервер…», окно отправки и разбор незаконченных отправок при запуске."""

    setUp_base = NewProjectDialogTests.setUp
    pump = NewProjectDialogTests.pump
    make_app = NewProjectDialogTests.make_app

    def setUp(self):
        self.setUp_base()
        from svnhelper import publish, server
        self.publish, self.server = publish, server
        self.client = _make_fake_server_client()
        self.folder = os.path.join(self.base, "мой проект")
        os.makedirs(self.folder)
        Path(self.folder, "a.txt").write_text("x", encoding="utf-8")

    def open_publish(self, app):
        from svnhelper.gui.new_project_dialog import PublishDialog
        dialog = PublishDialog(app, self.folder, jobs_dir=self.jobs)
        dialog.segments = ["konstruktor", "projects"]
        dialog._refresh()
        return dialog

    def result(self):
        return self.publish.PublishResult(self.server.build_url("svn://srv", ["konstruktor", "projects", "мой проект"]),
                                          7, 3)

    def select_folder(self, app):
        app.dir_var.set(self.base)
        app._scan_projects()
        self.pump(app.root, lambda: app._mine_scanned and len(app.mine) == 1)
        iid = next(iter(app.mine))
        app.mine_tree.selection_set(iid)
        app._update_mine_buttons()

    def test_menu_item_only_for_folder_without_server(self):
        root, app = self.make_app()
        self.select_folder(app)
        idx = app._more_idx["publish"]
        self.assertEqual(app.more_menu.entrycget(idx, "label"), "Отправить на сервер…")
        self.assertEqual(str(app.more_menu.entrycget(idx, "state")), "normal")
        item = app._selected_item()
        item.state = "svn"
        item.url = "svn://srv/konstruktor/projects/x/trunk"
        app._update_mine_buttons()
        self.assertEqual(str(app.more_menu.entrycget(idx, "state")), "disabled")      # уже на сервере
        item.url = "file:///C:/history/trunk"
        app._update_mine_buttons()
        self.assertEqual(str(app.more_menu.entrycget(idx, "state")), "normal")        # локальная история
        item.state = "plain"
        with mock.patch.object(app, "_path_busy", return_value=True):
            app._update_mine_buttons()
            self.assertEqual(str(app.more_menu.entrycget(idx, "state")), "disabled")

    def test_menu_refuses_when_folder_is_busy(self):
        root, app = self.make_app()
        self.select_folder(app)
        opened = []
        with mock.patch("svnhelper.gui.projects_tab.PublishDialog", lambda *a, **k: opened.append(a)), \
                mock.patch.object(app, "_path_busy", return_value=True), \
                mock.patch("tkinter.messagebox.showinfo") as info:
            app._on_publish_project()
        self.assertEqual(opened, [])
        self.assertEqual(info.call_args.args[1], self.publish.MESSAGES[self.publish.SVN_BUSY])

    def test_menu_opens_dialog_and_selects_project_after_send(self):
        root, app = self.make_app()
        self.select_folder(app)
        seen = []

        class FakeDialog:
            def __init__(self, app_, folder):
                seen.append(folder)
                self.result = object()
        with mock.patch("svnhelper.gui.projects_tab.PublishDialog", FakeDialog), mock.patch.object(root, "wait_window"):
            app._on_publish_project()
        self.assertEqual(seen, [self.folder])
        self.pump(root, lambda: app._mine_scanned and app.mine_tree.selection())

    def test_dialog_sends_with_name_place_and_comment(self):
        root, app = self.make_app()
        dialog = self.open_publish(app)
        self.assertEqual(dialog.place_var.get(), "Будет на сервере: konstruktor/projects/мой проект")
        self.assertIn("На сервер уйдёт последний сохранённый вариант. Прежние сохранения останутся только на этом "
                      "компьютере.", [w.cget("text") for w in dialog.winfo_children()[0].winfo_children()
                                      if w.winfo_class() == "TLabel"])
        dialog.name_var.set("мой проект 2")
        dialog.comment_var.set("первая отправка")
        done = self.result()
        done.changed = ["C:/x/a.txt"]
        with mock.patch.object(self.publish, "publish", return_value=done) as send, \
                mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.send_btn.invoke()
            self.pump(root, lambda: dialog.result is not None)
        args = send.call_args.args
        self.assertEqual(args[1], self.folder)
        self.assertEqual(args[2], self.server.build_url("svn://srv", ["konstruktor", "projects"]))
        self.assertEqual(args[3], "первая отправка")
        self.assertEqual(args[4], "мой проект 2")
        self.assertIn("Проект отправлен на сервер", info.call_args.args[1])
        self.assertIn("a.txt", info.call_args.args[1])                    # то, что отличается от сервера, показано

    def test_dialog_refuses_when_folder_is_busy(self):
        root, app = self.make_app()
        dialog = self.open_publish(app)
        with mock.patch.object(app, "_path_busy", return_value=True), \
                mock.patch.object(self.publish, "publish") as send, \
                mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.send_btn.invoke()
        send.assert_not_called()
        self.assertEqual(info.call_args.args[1], self.publish.MESSAGES[self.publish.SVN_BUSY])
        dialog.destroy()

    def test_no_rights_leaves_local_folder_and_says_so(self):
        from svnhelper.gui.new_project_dialog import NO_RIGHTS_TEXT
        root, app = self.make_app()
        dialog = self.open_publish(app)
        error = self.publish.PublishError(self.publish.NO_RIGHTS, "upload")
        with mock.patch.object(self.publish, "publish", side_effect=error), \
                mock.patch("tkinter.messagebox.showwarning") as warn:
            dialog.send_btn.invoke()
            self.pump(root, lambda: warn.call_count and dialog.state == "ready")
        self.assertEqual(warn.call_args.args[1], NO_RIGHTS_TEXT)
        self.assertIsNone(dialog.result)
        self.assertEqual(Path(self.folder, "a.txt").read_text("utf-8"), "x")
        self.assertEqual(os.listdir(self.folder), ["a.txt"])
        dialog.destroy()

    def test_resumable_offers_finish_button(self):
        root, app = self.make_app()
        dialog = self.open_publish(app)
        job = self.publish.PublishJob("j1", self.folder, self.result().project_url,
                                      svn_old=self.publish.svn_old_path(self.folder), stage="uploaded")
        error = self.publish.PublishError(self.publish.RESUMABLE, "check", job=job)
        with mock.patch.object(self.publish, "publish", side_effect=error), \
                mock.patch("tkinter.messagebox.showwarning") as warn:
            dialog.send_btn.invoke()
            self.pump(root, lambda: warn.call_count and dialog.state == "ready")
        self.assertIn("Завершить отправку", warn.call_args.args[1])
        self.assertTrue(dialog.resume_btn.winfo_manager())
        with mock.patch.object(self.publish, "resume_publish", return_value=self.result()) as resume, \
                mock.patch("tkinter.messagebox.showinfo"):
            dialog.resume_btn.invoke()
            self.pump(root, lambda: dialog.result is not None)
        self.assertIs(resume.call_args.args[1], job)

    def seed_job(self, stage):
        job = self.publish.PublishJob("j1", self.folder, self.result().project_url,
                                      svn_old=self.publish.svn_old_path(self.folder), stage=stage,
                                      path=str(self.jobs / "j1.json"))
        self.publish._save_job(job)
        return job

    def test_startup_recovers_job_before_upload_silently(self):
        self.seed_job("started")
        root, app = self.make_app(recover=True)
        self.pump(root, lambda: not list(self.jobs.glob("*.json")))
        self.assertEqual(app._publish_pending, [])
        self.assertEqual(app.publish_warn.winfo_manager(), "")

    def test_startup_offers_finish_and_finish_binds_folder(self):
        self.seed_job("uploaded")
        root, app = self.make_app(recover=True)
        self.pump(root, lambda: app._publish_pending)
        self.assertEqual(app.publish_warn.winfo_manager(), "grid")
        self.assertIn("«мой проект»", app.publish_warn_var.get())
        self.assertEqual(app.publish_finish_btn.cget("text"), "Завершить отправку")
        self.assertEqual(app.publish_discard_btn.cget("text"), "Убрать недоделанное")
        with mock.patch.object(self.publish, "resume_publish", return_value=self.result()) as resume, \
                mock.patch("tkinter.messagebox.showinfo") as info:
            app.publish_finish_btn.invoke()
            self.pump(root, lambda: not app._publish_pending)
        resume.assert_called_once()
        self.assertIn("Проект отправлен на сервер", info.call_args.args[1])
        self.assertEqual(app.publish_warn.winfo_manager(), "")

    def test_discard_cleans_journal_and_keeps_folder(self):
        self.seed_job("uploaded")
        root, app = self.make_app(recover=True)
        self.pump(root, lambda: app._publish_pending)
        with mock.patch("tkinter.messagebox.askyesno", return_value=True), \
                mock.patch("tkinter.messagebox.showinfo"):
            app.publish_discard_btn.invoke()
            self.pump(root, lambda: not app._publish_pending)
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        self.assertEqual(Path(self.folder, "a.txt").read_text("utf-8"), "x")
        self.assertEqual(sorted(os.listdir(self.base)), ["мой проект"])

    def test_busy_folder_blocks_finish(self):
        self.seed_job("uploaded")
        root, app = self.make_app(recover=True)
        self.pump(root, lambda: app._publish_pending)
        with mock.patch.object(app, "_path_busy", return_value=True), \
                mock.patch.object(self.publish, "resume_publish") as resume, \
                mock.patch("tkinter.messagebox.showinfo") as info:
            app.publish_finish_btn.invoke()
        resume.assert_not_called()
        self.assertEqual(info.call_args.args[1], self.publish.MESSAGES[self.publish.SVN_BUSY])


@unittest.skipUnless(HAS_DISPLAY and shutil.which("svn") and shutil.which("svnadmin"), "нет экрана или svn")
class PublishGuiRealSvnTests(unittest.TestCase):
    """T.5: «Отправить позже» из окна на настоящем svn (file:///): папка уходит на сервер и привязывается на месте."""

    pump = NewProjectDialogTests.pump

    def test_dialog_publishes_plain_folder(self):
        from svnhelper import publish, server
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.gui.new_project_dialog import PublishDialog
        from svnhelper.svn_core import SvnClient
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SVNLITE_DATA_DIR"] = os.path.join(tmp, "data")
            self.addCleanup(os.environ.pop, "SVNLITE_DATA_DIR", None)
            repo = Path(tmp) / "server repo"
            subprocess.run(["svnadmin", "create", str(repo)], check=True)
            parent = server.build_url(repo.as_uri(), ["konstruktor", "projects"])
            subprocess.run(["svn", "mkdir", "--parents", "-m", "место", parent], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            base = Path(tmp) / "проекты"
            folder = base / "261028_вафли"
            (folder / "docs").mkdir(parents=True)
            (folder / "docs" / "схема.txt").write_text("схема", encoding="utf-8")
            root = tk.Tk()
            root.withdraw()
            quiet_clipboard(root)
            self.addCleanup(lambda: root.winfo_exists() and root.destroy())
            cfg = Config(server=repo.as_uri(), repos=["konstruktor"], work_dir=str(base))
            app = App(root, cfg, SettingsStore(Path(tmp) / "settings.json"), SvnClient("svn"),
                      os.path.join(tmp, "log.txt"))
            dialog = PublishDialog(app, str(folder), jobs_dir=Path(tmp) / "jobs")
            dialog.segments = ["konstruktor", "projects"]
            dialog._refresh()
            with mock.patch("tkinter.messagebox.showinfo") as info:
                dialog.send_btn.invoke()
                self.pump(root, lambda: dialog.result is not None or dialog.state == "ready", timeout=60)
            self.assertIsNotNone(dialog.result, dialog.status_var.get())
            self.assertIn("Проект отправлен на сервер", info.call_args.args[1])
            project_url = server.build_url(parent, ["261028_вафли"])
            self.assertEqual(dialog.result.project_url, project_url)
            self.assertTrue((folder / ".svn").is_dir())                        # привязана на месте
            self.assertEqual((folder / "docs" / "схема.txt").read_text("utf-8"), "схема")
            self.assertEqual(publish.pending_jobs(Path(tmp) / "jobs"), [])
            self.assertEqual(sorted(os.listdir(base)), ["261028_вафли"])



@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class ReviewT5Tests(unittest.TestCase):
    """Замечания ревью T.3/T.5: папка не затирается, откат, захват, выход, журнал на загрузке и скачивании, повтор."""

    setUp_base = NewProjectDialogTests.setUp
    pump = NewProjectDialogTests.pump
    make_app = NewProjectDialogTests.make_app
    open_dialog = NewProjectDialogTests.open_dialog
    setUp = NewProjectServerTests.setUp
    ready_dialog = NewProjectServerTests.ready_dialog
    trunk = NewProjectServerTests.trunk

    def seed_job(self, stage, **fields):
        from svnhelper.gui import new_project_dialog as npd
        creating = npd.creating_path(self.base, "261028_вафли")
        fields.setdefault("author", "ivanov")
        job = npd.CreateJob("k1", self.base, creating, self.dest, stage=stage, project_url=self.project_url,
                            path=str(self.jobs / "k1.json"), **fields)
        npd._save_job(job)
        return job

    def scaffold(self, rev_entries=1, with_files=False):
        """Папки проекта на сервере так, как их оставляет прерванная загрузка."""
        c = self.client
        for part in ("trunk", "branches", "tags"):
            c.add_dirs(f"{self.project_url}/{part}")
        for i in range(rev_entries):
            c.revision += 1
            c._log(self.project_url, "x")
        if with_files:
            c.files[self.trunk("a.txt")] = b"1"

    # -- блокер 1 -------------------------------------------------------------------

    def test_download_never_touches_folder_that_appeared_meanwhile(self):
        root, app = self.make_app()
        dialog = self.ready_dialog(app)

        def intruder():
            os.makedirs(self.dest)
            Path(self.dest, "чужое.txt").write_text("моё", encoding="utf-8")
        self.client.after_import = intruder
        with mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.create_btn.invoke()
            self.pump(root, lambda: dialog.state == "ready" and info.call_count)
        self.assertEqual(Path(self.dest, "чужое.txt").read_text("utf-8"), "моё")        # ничего не удалено
        self.assertEqual(os.listdir(self.dest), ["чужое.txt"])
        self.assertIn(self.project_url + "/trunk", self.client.dirs)
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        dialog.destroy()

    # -- 2: откат не удался --------------------------------------------------------------

    def test_stop_with_failed_rollback_says_server_may_be_unfinished(self):
        import threading
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        self.client.import_gate = gate = threading.Event()
        self.client.fail_remove = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013")
        dialog = self.ready_dialog(app)
        with mock.patch("tkinter.messagebox.askyesno", return_value=True), \
                mock.patch("tkinter.messagebox.showwarning") as warn:
            dialog.create_btn.invoke()
            self.pump(root, lambda: self.project_url + "/trunk" in self.client.dirs)
            dialog._on_close()
            gate.set()
            self.pump(root, lambda: dialog.state == "ready" and warn.call_count)
        from svnhelper import publish
        self.assertIn(publish.ROLLBACK_LATER_TEXT, warn.call_args.args[1])
        self.assertIn(publish.ROLLBACK_LATER_TEXT, dialog.status_var.get())
        self.assertEqual(os.listdir(self.base), [])
        self.assertEqual(read_job_stage(self.jobs), "uploading")       # каркас уберёт разбор при следующем запуске
        dialog.destroy()

    def test_failed_upload_with_failed_rollback_keeps_journal_and_next_start_rolls_back(self):
        """Ревью п.3: откат не удался - журнал не удаляется; разбор при следующем запуске убирает каркас."""
        from svnhelper import publish
        from svnhelper.gui import App
        from svnhelper.gui import new_project_dialog as npd
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        self.client.fail_import = SvnError(ErrorKind.OTHER, "svn: E165001: сбой на сервере")
        self.client.fail_remove = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013")
        dialog = self.ready_dialog(app)
        App.show_error.reset_mock()
        dialog.create_btn.invoke()
        self.pump(root, lambda: dialog.state == "ready" and App.show_error.call_count)
        self.assertIn(publish.ROLLBACK_LATER_TEXT, App.show_error.call_args.args[1])
        self.assertEqual(os.listdir(self.base), [])
        self.assertEqual(read_job_stage(self.jobs), "uploading")
        dialog.destroy()
        npd.recover_create_jobs(self.jobs, client=self.client, landed=[])       # откат снова не вышел
        self.assertEqual(read_job_stage(self.jobs), "uploading")
        self.client.fail_remove = None
        landed = []
        npd.recover_create_jobs(self.jobs, client=self.client, landed=landed)   # следующий запуск
        self.assertNotIn(self.project_url, self.client.dirs)
        self.assertEqual((landed, list(self.jobs.glob("*.json"))), ([], []))

    def test_shared_undo_text(self):
        from svnhelper import messages
        self.assertEqual(messages.create_undo_text(None), "")
        self.assertIn("возвращено", messages.create_undo_text(True))
        self.assertIn("(диск)", messages.create_undo_text(False, "диск"))

    # -- 3: захват ---------------------------------------------------------------------

    def test_background_answer_does_not_steal_grab_from_place_dialog(self):
        from svnhelper.gui.server_tree import ServerPlaceDialog
        root, app = self.make_app()
        dialog = self.ready_dialog(app)
        place = ServerPlaceDialog(app, [], parent=dialog)
        got = []
        dialog._run(lambda: 1, got.append, lambda e: got.append(e))
        self.pump(root, lambda: got)
        self.assertEqual(str(place.grab_current()), str(place))
        place.destroy()
        dialog.destroy()

    # -- 4: выход из программы ----------------------------------------------------------

    def test_exit_question_depends_on_stage_and_closing_shows_no_dialogs(self):
        import threading
        from svnhelper.gui import App
        root, app = self.make_app()
        self.client.import_gate = upload_gate = threading.Event()
        self.client.checkout_gate = download_gate = threading.Event()
        dialog = self.ready_dialog(app)
        app._new_project_dialog = dialog
        with mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            dialog.create_btn.invoke()
            self.pump(root, lambda: dialog.phase == "upload")
            app._request_exit()
            self.assertIn("созданное будет удалено", ask.call_args.args[1])
            upload_gate.set()
            self.pump(root, lambda: dialog.phase == "download")
            app._request_exit()
            self.assertEqual(ask.call_args.args[1], dialog.exit_question())
            self.assertIn("скачивание остановится", ask.call_args.args[1])
        # программа закрывается во время скачивания: ни вопросов, ни окон ошибок
        App.show_error.reset_mock()
        with mock.patch("tkinter.messagebox.showinfo") as info, mock.patch("tkinter.messagebox.showwarning") as warn, \
                mock.patch("tkinter.messagebox.askyesno") as ask:
            app._closing = True
            dialog.stop_for_exit()
            download_gate.set()
            self.pump(root, lambda: dialog.state == "ready")
        for shown in (info, warn, ask, App.show_error):
            shown.assert_not_called()
        self.assertEqual(os.listdir(self.base), [])
        self.assertIn(self.project_url + "/trunk", self.client.dirs)
        app._closing = False
        app._new_project_dialog = None
        dialog.destroy()

    def test_closing_during_upload_shows_no_dialogs(self):
        import threading
        from svnhelper.gui import App
        root, app = self.make_app()
        self.client.import_gate = gate = threading.Event()
        dialog = self.ready_dialog(app)
        dialog.create_btn.invoke()
        self.pump(root, lambda: dialog.phase == "upload")
        App.show_error.reset_mock()
        with mock.patch("tkinter.messagebox.showinfo") as info, mock.patch("tkinter.messagebox.showwarning") as warn:
            app._closing = True
            dialog.stop_for_exit()
            gate.set()
            self.pump(root, lambda: dialog.state == "ready")
        info.assert_not_called()
        warn.assert_not_called()
        App.show_error.assert_not_called()
        self.assertNotIn(self.project_url, self.client.dirs)
        app._closing = False
        dialog.destroy()

    def test_publish_dialog_closing_shows_no_dialogs(self):
        import threading
        from svnhelper import publish
        from svnhelper.gui import App
        from svnhelper.gui.new_project_dialog import PublishDialog
        root, app = self.make_app()
        folder = os.path.join(self.base, "п")
        os.makedirs(folder)
        dialog = PublishDialog(app, folder, jobs_dir=self.jobs)
        dialog.segments = ["konstruktor", "projects"]
        dialog._refresh()
        gate = threading.Event()

        def slow(*args, **kwargs):
            gate.wait(10)
            raise publish.PublishError(publish.FAILED, "upload", error=None)
        App.show_error.reset_mock()
        with mock.patch.object(publish, "publish", slow), mock.patch("tkinter.messagebox.showwarning") as warn:
            dialog.send_btn.invoke()
            self.pump(root, lambda: dialog.state == "running")
            app._closing = True
            gate.set()
            self.pump(root, lambda: dialog.state == "ready")
        warn.assert_not_called()
        App.show_error.assert_not_called()
        app._closing = False
        self.assertTrue(dialog.exit_question())
        dialog.destroy()

    # -- 5: журнал на загрузке ----------------------------------------------------------

    def test_upload_stage_is_journaled_with_author_and_url(self):
        import threading
        root, app = self.make_app()
        self.client.credentials = mock.Mock(username="ivanov")
        self.client.import_gate = gate = threading.Event()
        dialog = self.ready_dialog(app)
        dialog.create_btn.invoke()
        self.pump(root, lambda: self.project_url + "/trunk" in self.client.dirs)
        self.pump(root, lambda: read_job_stage(self.jobs) == "uploading")
        self.pump(root, lambda: (read_job(self.jobs) or {}).get("layout_revision"))     # номер правки каркаса записан
        data = read_job(self.jobs)
        self.assertEqual((data["project_url"], data["author"]), (self.project_url, "ivanov"))
        self.assertEqual(data["layout_revision"], self.client.revision)
        self.assertEqual(data["pid"], os.getpid())
        gate.set()
        self.pump(root, lambda: dialog.result is not None)
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_killed_during_upload_scaffold_is_rolled_back(self):
        from svnhelper.gui import new_project_dialog as npd
        self.make_app()
        job = self.seed_job("uploading")
        os.makedirs(os.path.join(job.creating, "01"))
        self.scaffold(rev_entries=1)
        landed = []
        npd.recover_create_jobs(self.jobs, client=self.client, landed=landed)
        self.assertFalse(os.path.exists(job.creating))
        self.assertNotIn(self.project_url, self.client.dirs)           # каркас откатан
        self.assertEqual(landed, [])
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_killed_during_upload_after_data_landed_is_reported(self):
        from svnhelper.gui import new_project_dialog as npd
        self.make_app()
        job = self.seed_job("uploading")
        os.makedirs(os.path.join(job.creating, "01"))
        self.scaffold(rev_entries=2, with_files=True)
        landed = []
        npd.recover_create_jobs(self.jobs, client=self.client, landed=landed)
        self.assertFalse(os.path.exists(job.creating))
        self.assertIn(self.project_url + "/trunk", self.client.dirs)    # готовый проект не трогаем
        self.assertEqual(landed, [self.project_url])
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_killed_during_upload_without_client_keeps_journal_and_foreign_author_is_left_alone(self):
        from svnhelper.gui import new_project_dialog as npd
        self.make_app()
        job = self.seed_job("uploading")
        os.makedirs(os.path.join(job.creating, "01"))
        self.scaffold(rev_entries=1)
        npd.recover_create_jobs(self.jobs)                              # связи нет
        self.assertFalse(os.path.exists(job.creating))                  # локальное убрано
        self.assertEqual(len(list(self.jobs.glob("*.json"))), 1)        # решение о сервере отложено
        self.client.log = [(u, e.__class__(e.revision, "petrov", None, "x")) for u, e in self.client.log]
        npd.recover_create_jobs(self.jobs, client=self.client)
        self.assertIn(self.project_url, self.client.dirs)               # чужое не трогаем
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_killed_during_upload_without_login_is_decided_by_revision(self):
        """Ревью п.4: логина в журнале нет (вход через кэш), но есть номер правки каркаса - разбор решает по нему."""
        from svnhelper.gui import new_project_dialog as npd
        job = self.seed_job("uploading", author="")
        self.scaffold(rev_entries=1)
        job.layout_revision = self.client.revision
        npd._save_job(job)
        npd.recover_create_jobs(self.jobs, client=self.client, landed=[])
        self.assertNotIn(self.project_url, self.client.dirs)            # только каркас: откатан
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        job = self.seed_job("uploading", author="")                     # загрузка дошла: правка после каркаса
        self.scaffold(rev_entries=1)
        job.layout_revision = self.client.revision
        npd._save_job(job)
        self.client.revision += 1
        self.client._log(self.project_url, "загрузка")
        landed = []
        npd.recover_create_jobs(self.jobs, client=self.client, landed=landed)
        self.assertEqual(landed, [self.project_url])
        self.assertIn(self.project_url + "/trunk", self.client.dirs)

    def test_killed_during_upload_without_login_and_revision_is_not_dropped_silently(self):
        from svnhelper import publish
        from svnhelper.gui import new_project_dialog as npd
        self.seed_job("uploading", author="")
        self.scaffold(rev_entries=1)
        npd.recover_create_jobs(self.jobs, client=self.client, landed=[])          # сказать некому: журнал ждёт
        self.assertEqual(len(list(self.jobs.glob("*.json"))), 1)
        unsure = []
        npd.recover_create_jobs(self.jobs, client=self.client, landed=[], unsure=unsure)
        self.assertEqual(unsure, [self.project_url])
        self.assertIn(self.project_url, self.client.dirs)                           # чужое не трогаем
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        self.assertIn("«261028_вафли»", publish.unknown_owner_text(self.project_url))

    def test_startup_tells_about_scaffold_of_unknown_owner(self):
        self.seed_job("uploading", author="")
        self.scaffold(rev_entries=1)
        root, app = self.make_app(recover=True)
        self.pump(root, lambda: app._created_notes)
        self.assertIn("Не удалось выяснить, чей недоделанный проект «261028_вафли»", app.publish_warn_var.get())
        self.assertIn(self.project_url, self.client.dirs)

    def test_startup_shows_note_about_project_created_but_not_downloaded(self):
        job = self.seed_job("download")
        os.makedirs(os.path.join(job.final, ".svn"))
        root, app = self.make_app(recover=True)
        self.pump(root, lambda: app._created_notes)
        self.assertEqual(app.publish_warn.winfo_manager(), "grid")
        self.assertIn("создан на сервере, но не скачан", app.publish_warn_var.get())
        self.assertIn("«261028_вафли»", app.publish_warn_var.get())
        self.assertIn("«Скачать проект»", app.publish_warn_var.get())
        app.publish_note_btn.invoke()
        self.assertEqual(app.publish_warn.winfo_manager(), "")

    # -- 6: журнал на скачивании ------------------------------------------------------------

    def test_download_is_journaled_and_folder_is_busy(self):
        import threading
        from svnhelper import publish
        root, app = self.make_app()
        self.client.checkout_gate = gate = threading.Event()
        dialog = self.ready_dialog(app)
        dialog.create_btn.invoke()
        self.pump(root, lambda: dialog.phase == "download")
        self.pump(root, lambda: read_job_stage(self.jobs) == "download")
        self.assertTrue(publish.is_busy(self.dest))
        gate.set()
        self.pump(root, lambda: dialog.result is not None)
        self.assertFalse(publish.is_busy(self.dest))
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_killed_during_download_removes_partial_folder(self):
        from svnhelper.gui import new_project_dialog as npd
        self.make_app()
        self.seed_job("download")
        os.makedirs(os.path.join(self.dest, ".svn"))
        Path(self.dest, "a.txt").write_text("1", encoding="utf-8")
        landed = []
        npd.recover_create_jobs(self.jobs, client=self.client, landed=landed)
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(landed, [self.project_url])
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    def test_killed_during_download_keeps_foreign_folder(self):
        from svnhelper.gui import new_project_dialog as npd
        self.make_app()
        self.seed_job("download")
        os.makedirs(self.dest)
        Path(self.dest, "моё.txt").write_text("1", encoding="utf-8")        # не начатая копия
        npd.recover_create_jobs(self.jobs, client=self.client)
        self.assertTrue(os.path.exists(os.path.join(self.dest, "моё.txt")))
        self.assertEqual(list(self.jobs.glob("*.json")), [])

    # -- 17: «Скачать ещё раз» -----------------------------------------------------------------

    def test_retry_download_button_after_failed_download(self):
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        self.client.fail_checkout = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013")
        dialog = self.ready_dialog(app)
        self.client.on_checkout = lambda: dialog.server_state.clear()      # 11: ключ мог пропасть
        with mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.create_btn.invoke()
            self.pump(root, lambda: dialog.state == "ready" and info.call_count)
        self.assertEqual(dialog.server_state[self.project_url], (True, True))
        self.assertEqual(dialog.retry_btn.cget("text"), "Скачать ещё раз")
        self.assertTrue(dialog.retry_btn.winfo_manager())
        self.client.fail_checkout = None
        dialog.retry_btn.invoke()
        self.pump(root, lambda: dialog.result is not None)
        self.assertEqual(dialog.result, self.dest)
        self.assertEqual(Path(self.dest, "261028_вафли.txt").read_bytes(), b"hello")
        self.assertEqual(self.client.files[self.trunk("261028_вафли.txt")], b"hello")
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        self.assertEqual(os.listdir(self.base), ["261028_вафли"])

    # -- 7: окно места закрыто, пока грузится дерево ----------------------------------------------

    def test_place_dialog_closed_while_folder_is_loading(self):
        import threading
        from svnhelper.gui.server_tree import ServerPlaceDialog
        root, app = self.make_app()
        self.client.list_gate = gate = threading.Event()
        place = ServerPlaceDialog(app, [])
        tree = place.tree
        first = tree.tree.get_children()[0]
        tree.tree.focus(first)
        tree.load_node(first)
        self.assertEqual(tree.busy, 1)
        place.destroy()
        with mock.patch("svnhelper.gui.app.log") as app_log:
            gate.set()
            self.pump(root, lambda: tree.busy == 0)
            root.update()
        app_log.exception.assert_not_called()

    # -- 8: вход только до того, как на сервере что-то появилось ---------------------------------------

    def test_login_retry_only_before_anything_is_on_server(self):
        from svnhelper.gui import App
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        self.client.fail_import = SvnError(ErrorKind.AUTH, "svn: E170001")      # отказ уже на этапе загрузки
        dialog = self.ready_dialog(app)
        App.show_error.reset_mock()
        with mock.patch.object(app.login, "request") as request:
            dialog.create_btn.invoke()
            self.pump(root, lambda: dialog.state == "ready" and App.show_error.call_count)
        request.assert_not_called()
        self.assertNotIn(self.project_url, self.client.dirs)
        self.client.fail_import = None
        self.client.fail_mkdir = SvnError(ErrorKind.AUTH, "svn: E170001")      # до создания папок: можно войти и повторить
        with mock.patch.object(app.login, "request") as request:
            dialog.create_btn.invoke()
            self.pump(root, lambda: request.call_count)
        dialog.destroy()

    # -- 9: нет прав, откат не удался ----------------------------------------------------------------------

    def test_no_rights_with_failed_rollback_mentions_unfinished_folder(self):
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        self.client.fail_import = SvnError(ErrorKind.AUTHZ, "svn: E220004")
        self.client.fail_remove = SvnError(ErrorKind.NO_CONNECTION, "svn: E170013")
        dialog = self.ready_dialog(app)
        with mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.create_btn.invoke()
            self.pump(root, lambda: dialog.result is not None)
        from svnhelper import publish
        self.assertIn("отправить можно позже", info.call_args.args[1])
        self.assertIn(publish.ROLLBACK_LATER_TEXT, info.call_args.args[1])
        self.assertEqual(read_job_stage(self.jobs), "uploading")

    # -- 10: проверка имени в фоне ---------------------------------------------------------------------------

    def test_name_check_failure_is_not_remembered_and_never_asks_for_login(self):
        from svnhelper.svn_core import ErrorKind, SvnError
        root, app = self.make_app()
        dialog = self.open_dialog(app, storage=None)
        self.client.exists_error = SvnError(ErrorKind.AUTH, "svn: E170001")
        with mock.patch.object(app.login, "request") as request:
            dialog.field_vars["date"].set("261028")
            dialog.field_vars["name"].set("вафли")
            self.pump(root, lambda: self.project_url in dialog.server_state)
        request.assert_not_called()
        self.assertIsNone(dialog.server_state[self.project_url])
        self.assertEqual(str(dialog.create_btn.cget("state")), "normal")          # «не проверено» создание не блокирует
        self.client.exists_error = None
        dialog.field_vars["name"].set("вафли ")                                    # следующий ввод
        self.assertNotIn(self.project_url, dialog.server_state)
        self.pump(root, lambda: dialog.server_state.get(self.project_url) == (True, False))
        dialog.destroy()

    def test_destroy_cancels_scheduled_work(self):
        root, app = self.make_app()
        dialog = self.open_dialog(app, storage=None)
        dialog.field_vars["name"].set("вафли")
        self.assertIsNotNone(dialog._server_after)
        dialog.destroy()
        self.assertIsNone(dialog._server_after)
        self.assertIsNone(dialog._tick_after)

    # -- 12, 13: окно «Место на сервере» ---------------------------------------------------------------------------

    def test_place_dialog_opens_on_current_place_and_double_click_chooses(self):
        from svnhelper import theme
        from svnhelper.gui.server_tree import ServerPlaceDialog
        root, app = self.make_app()
        parent = self.ready_dialog(app)
        place = ServerPlaceDialog(app, ["konstruktor", "projects"], parent=parent)
        self.assertEqual(str(place.wm_transient()), str(parent))
        self.assertEqual(place.cget("background"), theme.PALETTE.surface)
        self.pump(root, lambda: place.picked == ["konstruktor", "projects"])        # раскрыто и выделено
        self.assertTrue(place.tree.tree.bind("<Double-1>"))                  # двойной щелчок = «Выбрать»
        place._on_ok()
        self.assertEqual(place.result, ["konstruktor", "projects"])
        parent.destroy()

    def test_open_place_dialog_is_recolored_with_theme(self):
        from svnhelper.gui.server_tree import ServerPlaceDialog
        root, app = self.make_app()
        place = ServerPlaceDialog(app, [])
        app.set_theme("dark")
        self.assertEqual(str(place.tree.tree.tag_configure("project", "foreground")), str(app.palette.success))
        place.destroy()
        self.assertEqual(len(app._extra_trees), 1)           # остаётся только дерево вкладки «Скачать проект»

    # -- 14: высота окна ----------------------------------------------------------------------------------------------

    def test_new_project_window_fits_small_screen_with_scroll(self):
        root, app = self.make_app()
        fields = [{"id": "date", "label": "Дата", "default": "{{today:yyMMdd}}"},
                  {"id": "name", "label": "Название", "required": True, "hint": "например, вафли"}] + \
                 [{"id": f"f{i}", "label": f"Поле {i}", "hint": "подсказка"} for i in range(3)]
        self.client.add("elec", title="Электроника: проект",
                        manifest=self.tt.manifest_json(title="Электроника: проект", fields=fields).encode("utf-8"))
        with mock.patch.object(tk.Misc, "winfo_screenheight", return_value=600):
            dialog = self.open_dialog(app, storage=None)
            self.assertEqual(len(dialog.field_vars), 5)
            dialog._toggle_details()
            dialog.update_idletasks()
            self.assertLessEqual(dialog.winfo_reqheight(), 600 - 80)
            self.assertTrue(dialog._scroll.vbar.winfo_manager())           # не поместилось - есть прокрутка
        dialog.destroy()

    # -- 16: отказ при несохранённых изменениях истории ------------------------------------------------------------------

    def test_publish_refusal_for_unsaved_local_history_has_clear_text(self):
        from svnhelper import publish
        from svnhelper.gui.new_project_dialog import PublishDialog
        root, app = self.make_app()
        folder = os.path.join(self.base, "п")
        os.makedirs(folder)
        dialog = PublishDialog(app, folder, jobs_dir=self.jobs)
        dialog.segments = ["konstruktor", "projects"]
        dialog._refresh()
        error = publish.PublishError(publish.LOCAL_CHANGES, "check")
        # T.6: к отказу добавлен вопрос «Сохранить их у себя сейчас?» (ответ «нет» - окно остаётся открытым)
        with mock.patch.object(publish, "publish", side_effect=error), \
                mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            dialog.send_btn.invoke()
            self.pump(root, lambda: ask.call_count)
        self.assertTrue(ask.call_args.args[1].startswith(
            "В проекте есть несохранённые изменения. Сначала сохраните их у себя, затем отправляйте."))
        self.assertIn("Сохранить их у себя и сразу отправить", ask.call_args.args[1])
        dialog.destroy()


@unittest.skipUnless(HAS_DISPLAY and shutil.which("svn") and shutil.which("svnadmin"), "нет экрана или svn")
class LocalHistoryGuiTests(unittest.TestCase):
    """T.6: «Мои проекты» для проекта с историей на компьютере (file://, не сервер из настроек), на настоящем svn."""

    pump = NewProjectDialogTests.pump
    FOLDER = "261028_вафли"

    def setUp(self):
        from svnhelper import localrepo
        from svnhelper.gui import App
        self.addCleanup(gc.collect)           # последним: мусор окон собирает главный поток, а не фоновый (Tcl_AsyncDelete)
        App.show_error.reset_mock()
        self.localrepo = localrepo
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": os.path.join(self.tmp.name, "data")})
        env.start()
        self.addCleanup(env.stop)
        self.server_repo = Path(self.tmp.name) / "server repo"             # настоящий «сервер» - другое хранилище
        subprocess.run(["svnadmin", "create", str(self.server_repo)], check=True)
        self.base = Path(self.tmp.name) / "проекты @ 1%"
        self.folder = self.base / self.FOLDER
        (self.folder / "docs").mkdir(parents=True)
        (self.folder / "docs" / "схема.txt").write_text("схема", encoding="utf-8")
        (self.folder / "README.txt").write_text("описание", encoding="utf-8")
        (self.folder / ".git").mkdir()
        (self.folder / ".git" / "config").write_text("x")

    def make_app(self, create_history=True):
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        root = tk.Tk()
        root.withdraw()
        quiet_clipboard(root)
        self.addCleanup(lambda: root.winfo_exists() and root.destroy())
        cfg = Config(server=self.server_repo.as_uri(), repos=["konstruktor"], work_dir=str(self.base))
        store = SettingsStore(Path(self.tmp.name) / "settings.json")
        app = App(root, cfg, store, SvnClient("svn"), os.path.join(self.tmp.name, "log.txt"))
        app.tortoise_exe = None
        app._apply_tortoise()
        if create_history:
            self.result = self.localrepo.create_local_repo(app.client, str(self.folder), shutil.which("svnadmin"),
                                                           store=store, jobs_dir=Path(self.tmp.name) / "jobs")
        self.rescan(root, app)
        return root, app

    def rescan(self, root, app):
        app.dir_var.set(str(self.base))
        app._mine_scanned = False
        app._scan_projects()
        self.pump(root, lambda: app._mine_scanned and len(app.mine) >= 1)
        self.pump(root, lambda: not app._checking)

    def select(self, root, app, name=None):
        for iid, item in app.mine.items():
            if name is None or item.name == name:
                app.mine_tree.selection_set(iid)
                root.update()
                return iid, item
        self.fail("нет проекта " + str(name))

    @staticmethod
    def hidden(app):
        return app.actions_row._hidden

    @staticmethod
    def accents(app):
        """Все подсвеченные кнопки окна (обе колонки): на экране она одна."""
        return shown_accents(app)

    @staticmethod
    def save_local_shown(app):
        """«Сохранить у себя» показана в правой колонке «Изменений»."""
        return app.project_panel.save_local_btn not in app.project_panel.actions_for("changes")._hidden

    def checked(self, root, app, item):
        self.pump(root, lambda: item.status is not None and not item.checking and not app._checking)

    def test_card_is_simplified_and_buttons_follow_state(self):
        root, app = self.make_app()
        iid, item = self.select(root, app)
        self.checked(root, app, item)
        self.assertTrue(item.local)
        self.assertIsNone(item.line)                                           # ни веток, ни версий
        # главная кнопка: папка чистая (.git мусором не считается) - «Отправить на сервер…»
        self.assertEqual(self.accents(app), [app.publish_btn])
        self.assertEqual(app.publish_btn.cget("text"), "Отправить на сервер…")
        self.assertEqual(app.project_panel.save_local_btn.cget("text"), "Сохранить у себя")
        for button in (app.update_btn, app.server_btn):
            self.assertIn(button, self.hidden(app))                            # нет «Получить новое» и адреса сервера
        self.assertNotIn(app.publish_btn, self.hidden(app))
        self.assertTrue(self.save_local_shown(app))                            # «Сохранить у себя» - справа
        self.assertIn(app._commit_panel.save_button, app.project_panel.actions_for("changes")._hidden)   # а не «отмеченное»
        self.assertEqual(str(app.publish_btn.cget("state")), "normal")
        # шапка: вместо адреса и ветки - «Хранится только на этом компьютере»
        header = app.project_panel.header_text()
        self.assertIn("Хранится только на этом компьютере", header)
        self.assertIn("сохранение № 2", header)
        self.assertNotIn("Ветка:", header)
        self.assertIn("Всё сохранено у вас", header)
        values = app.mine_tree.item(iid, "values")
        self.assertEqual(values[8], "Хранится только на этом компьютере")      # столбец «Путь на сервере»
        # вкладок про сервер у такого проекта нет
        panel = app.project_panel
        for key in ("incoming", "locks", "lines"):
            self.assertEqual(str(panel.tabs.tab(panel.frames[key], "state")), "hidden")
        self.assertFalse(app._offline)

    def test_save_button_saves_everything_with_auto_comment(self):
        from svnhelper.gui import App
        root, app = self.make_app()
        iid, item = self.select(root, app)
        self.checked(root, app, item)
        (self.folder / "README.txt").write_text("правка", encoding="utf-8")
        (self.folder / "новый файл.txt").write_text("новый", encoding="utf-8")
        app._on_refresh_tab()
        self.pump(root, lambda: item.status is not None and item.status.local_changes and not app._checking)
        self.assertEqual(self.accents(app), [app.project_panel.save_local_btn])   # есть правки - главное «Сохранить у себя»
        self.assertEqual(sorted(os.path.basename(c.path) for c in item.status.local_changes),
                         ["README.txt", "новый файл.txt"])
        self.assertIn("Есть несохранённые правки", app.project_panel.header_text())
        with mock.patch("tkinter.messagebox.showinfo") as info, mock.patch("tkinter.messagebox.showwarning") as warn:
            app.project_panel.save_local_btn.invoke()
            self.assertTrue(app._committing)
            self.pump(root, lambda: not app._committing and item.status is not None
                      and not item.status.local_changes and not app._checking)
        info.assert_not_called()
        warn.assert_not_called()
        App.show_error.assert_not_called()
        # номер вырос, комментарий подставила программа и в «недавние» он не попал
        self.assertEqual(self.client_revision(app, item), 3)
        self.assertEqual(item.revision, 3)
        self.assertIn("сохранение № 3", app.project_panel.header_text())
        youngest = subprocess.run(["svnlook", "youngest", self.result.repo], stdout=subprocess.PIPE)
        self.assertEqual(int(youngest.stdout.decode().strip()), 3)
        log = app.client.recent_log(str(self.folder), 5)
        self.assertRegex(log[0].message, r"^Сохранено у себя \d\d\.\d\d\.\d{4} \d\d:\d\d$")
        self.assertEqual(app.settings.recent_comments, [])
        self.assertEqual(app.mine_status.get(), "Сохранено у себя: 2 файла")
        self.assertEqual(self.accents(app), [app.publish_btn])                 # снова чисто - «Отправить на сервер…»

    @staticmethod
    def client_revision(app, item):
        return app.client.info(item.path).revision

    def test_typed_comment_is_used_and_remembered(self):
        root, app = self.make_app()
        iid, item = self.select(root, app)
        self.checked(root, app, item)
        (self.folder / "README.txt").write_text("правка", encoding="utf-8")
        app._on_refresh_tab()
        self.pump(root, lambda: item.status is not None and item.status.local_changes and not app._checking)
        app._commit_item = item
        app._commit_panel.entry.insert(0, "Исправил описание")
        with mock.patch("tkinter.messagebox.showinfo"):
            app.project_panel.save_local_btn.invoke()
            self.pump(root, lambda: not app._committing and item.status is not None
                      and not item.status.local_changes and not app._checking)
        self.assertEqual(app.client.recent_log(str(self.folder), 1)[0].message, "Исправил описание")
        self.assertEqual(app.settings.recent_comments[:1], ["Исправил описание"])

    def test_nothing_to_save_says_so(self):
        root, app = self.make_app()
        iid, item = self.select(root, app)
        self.checked(root, app, item)
        app.project_panel.save_local_btn.invoke()
        self.pump(root, lambda: not app._committing and not app._checking)
        self.assertEqual(app.mine_status.get(), "Сохранять было нечего: файлы не изменились.")
        self.assertEqual(self.client_revision(app, item), 2)

    def test_other_projects_keep_server_buttons(self):
        root, app = self.make_app()
        other = self.base / "серверный"
        app.client.checkout(self.server_repo.as_uri(), str(other))        # проект с сервера (не история на компьютере)
        self.rescan(root, app)
        iid, item = self.select(root, app, "серверный")
        self.assertFalse(item.local)
        self.assertFalse(self.save_local_shown(app))                           # «Сохранить у себя» только у истории на компьютере
        for button in (app.update_btn, app.server_btn):
            self.assertNotIn(button, self.hidden(app))
        self.assertIn(app.publish_btn, self.hidden(app))
        panel = app.project_panel
        for key in ("incoming", "locks", "lines"):
            self.assertEqual(str(panel.tabs.tab(panel.frames[key], "state")), "normal")
        self.assertNotIn("Хранится только на этом компьютере", panel.header_text())
        self.select(root, app, self.FOLDER)                                    # и обратно
        self.assertTrue(self.save_local_shown(app))

    def lose_history(self, root, app, name=None):
        """Перечитывает список после потери истории и выбирает проект; ждёт, пока проверки закончатся."""
        self.rescan(root, app)
        iid, item = self.select(root, app, name or self.FOLDER)
        self.pump(root, lambda: not item.checking and not app._checking)
        return iid, item

    def test_history_lost_but_found_offers_restore_first(self):
        """Ревью 2026-11-02, п.5: хранилище на диске есть (переименовано), главная кнопка - «Восстановить историю»,
        «Продолжить без истории» - второе действие с подтверждением, «Отправить на сервер…» остаётся."""
        from svnhelper.gui import App
        root, app = self.make_app()
        other = self.base / "серверный"
        app.client.checkout(self.server_repo.as_uri(), str(other))        # проект с сервера (не история на компьютере)
        os.rename(self.result.repo, self.result.repo + "_moved")              # хранилище переименовано
        iid, item = self.lose_history(root, app)
        self.assertTrue(item.local and item.history_missing)
        self.assertEqual(item.history_found, self.result.repo + "_moved")
        header = app.project_panel.header_text()
        self.assertIn("История на этом компьютере потеряна", header)
        self.assertIn("Восстановить историю", header)
        self.assertEqual(app.special_btn.cget("text"), "Восстановить историю")
        self.assertEqual(self.accents(app), [app.special_btn])
        self.assertFalse(self.save_local_shown(app))                           # сохранять некуда: истории нет
        self.assertNotIn(app.publish_btn, self.hidden(app))                    # отправить можно и так
        self.assertNotIn(app.continue_btn, self.hidden(app))
        self.assertEqual(app.continue_btn.cget("text"), "Продолжить без истории")
        self.assertNotEqual(str(app.continue_btn.cget("style")), "Accent.TButton")      # второе действие, не главное
        # остальные проекты не страдают
        other_iid, other_item = self.select(root, app, "серверный")
        self.assertEqual(other_item.state, "svn")
        self.assertFalse(other_item.history_missing)
        self.assertIn(app.continue_btn, self.hidden(app))
        self.pump(root, lambda: other_item.status is not None)
        self.assertEqual(other_item.error, "")
        self.assertFalse(app._offline)
        self.select(root, app, self.FOLDER)
        # «Продолжить без истории»: другое предупреждение, «Нет» ничего не меняет
        with mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            app.continue_btn.invoke()
        self.assertIn("её можно вернуть кнопкой «Восстановить историю»", ask.call_args.args[1])
        self.assertIn("Точно продолжить без истории?", ask.call_args.args[1])
        self.assertTrue((self.folder / ".svn").is_dir())
        # «Восстановить историю»: папка снова на своей истории, прежние сохранения на месте, сохранение работает
        app.special_btn.invoke()
        self.pump(root, lambda: not item.history_missing and not app._checking and item.status is not None)
        self.assertIn("Готово: история проекта восстановлена.", app.project_panel.header_text())
        self.assertEqual(self.accents(app), [app.publish_btn])
        self.assertIn(app.continue_btn, self.hidden(app))
        self.assertIn(app.special_btn, self.hidden(app))
        self.assertEqual(app.settings.local_histories, {os.path.normcase(str(self.folder)): self.result.repo + "_moved"})
        self.assertEqual([e.revision for e in app.client.recent_log(str(self.folder), 5)], [2, 1])
        (self.folder / "README.txt").write_text("после восстановления", encoding="utf-8")
        app._on_refresh_tab()
        self.pump(root, lambda: item.status is not None and item.status.local_changes and not app._checking)
        with mock.patch("tkinter.messagebox.showinfo"):
            app.project_panel.save_local_btn.invoke()
            self.pump(root, lambda: not app._committing and item.status is not None
                      and not item.status.local_changes and not app._checking)
        self.assertEqual(self.client_revision(app, item), 3)
        App.show_error.assert_not_called()

    def test_history_lost_and_gone_makes_publish_the_main_action(self):
        """Хранилища на диске нет: «История этого проекта на компьютере не найдена», главная - «Отправить на сервер…»
        (при связи); «Продолжить без истории» - вторым действием с подтверждением."""
        from svnhelper.gui import App
        root, app = self.make_app()
        shutil.rmtree(self.result.repo, onerror=lambda f, path, _e: (os.chmod(path, 0o700), f(path)))
        iid, item = self.lose_history(root, app)
        self.assertTrue(item.local and item.history_missing)
        self.assertEqual(item.history_found, "")
        header = app.project_panel.header_text()
        self.assertIn("История этого проекта на компьютере не найдена", header)
        self.assertEqual(self.accents(app), [app.publish_btn])
        self.assertEqual(str(app.publish_btn.cget("state")), "normal")
        self.assertIn(app.special_btn, self.hidden(app))                       # «Восстановить» нечего
        self.assertFalse(self.save_local_shown(app))
        self.assertNotIn(app.continue_btn, self.hidden(app))
        app._set_offline(True)                                                 # без связи отправить нельзя, подсказка есть
        app._update_mine_buttons()
        self.assertEqual(str(app.publish_btn.cget("state")), "disabled")
        app._set_offline(False)
        app._update_mine_buttons()
        self.assertEqual(str(app.publish_btn.cget("state")), "normal")
        # «Нет» ничего не меняет
        with mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            app.continue_btn.invoke()
        self.assertIn("перестанет вести их историю", ask.call_args.args[1])
        self.assertIn("Точно продолжить без истории?", ask.call_args.args[1])
        self.assertTrue((self.folder / ".svn").is_dir())
        # «Да»: .svn убирается, файлы остаются, проект становится папкой «Только на этом компьютере»
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            app.continue_btn.invoke()
            self.pump(root, lambda: not (self.folder / ".svn").exists() and app._mine_scanned
                      and any(i.state == "plain" for i in app.mine.values()))
        self.pump(root, lambda: not app._checking)
        self.assertEqual((self.folder / "README.txt").read_text("utf-8"), "описание")
        self.assertEqual((self.folder / "docs" / "схема.txt").read_text("utf-8"), "схема")
        self.assertEqual(app.settings.local_histories, {})
        App.show_error.assert_not_called()
        iid, plain = self.select(root, app)
        self.assertEqual(plain.state, "plain")
        self.assertIn("Только на этом компьютере", app.project_panel.header_text())
        self.assertEqual(self.accents(app), [app.publish_btn])

    def test_renamed_projects_folder_is_restored_from_the_card(self):
        """Папку проектов переименовали вместе с историей: после выбора новой папки карточка предлагает «Восстановить
        историю», она работает, пути с «@» и «%» не мешают."""
        root, app = self.make_app()
        new_base = self.base.with_name("проекты @ 2% новые")
        os.rename(self.base, new_base)
        self.base, self.folder = new_base, new_base / self.FOLDER
        iid, item = self.lose_history(root, app)
        self.assertTrue(item.history_missing)
        self.assertEqual(os.path.normcase(item.history_found),
                         os.path.normcase(str(new_base / ".svnlite" / "history" / Path(self.result.repo).name)))
        self.assertEqual(app.special_btn.cget("text"), "Восстановить историю")
        app.special_btn.invoke()
        self.pump(root, lambda: not item.history_missing and item.status is not None and not app._checking)
        self.assertEqual(self.accents(app), [app.publish_btn])
        self.assertEqual(self.client_revision(app, item), 2)
        self.assertEqual(list(app.settings.local_histories), [os.path.normcase(str(self.folder))])

    def test_plain_folder_is_local_only_with_publish_as_main_action(self):
        """Ревью 2026-11-02, п.7: папка без истории - «Только на этом компьютере» и «Отправить на сервер…», а не
        «Обычная папка, не из SVN» с советом «Скачайте проект»."""
        root, app = self.make_app(create_history=False)
        iid, item = self.select(root, app)
        self.assertEqual(item.state, "plain")
        values = app.mine_tree.item(iid, "values")
        self.assertEqual(values[2], "● Только на этом компьютере")
        self.assertIn("Отправить на сервер", values[3])
        header = app.project_panel.header_text()
        self.assertIn("Только на этом компьютере", header)
        self.assertNotIn("Скачайте", header)
        self.assertNotIn("SVN", header)
        self.assertEqual(self.accents(app), [app.publish_btn])
        self.assertEqual(app.publish_btn.cget("text"), "Отправить на сервер…")
        self.assertEqual(str(app.publish_btn.cget("state")), "normal")
        for button in (app.update_btn, app.server_btn, app.continue_btn):
            self.assertIn(button, self.hidden(app))
        self.assertFalse(self.save_local_shown(app))
        app._set_offline(True)
        app._update_mine_buttons()
        self.assertEqual(str(app.publish_btn.cget("state")), "disabled")       # без связи - серая, с подсказкой
        self.assertIn("получится, когда появится связь", app.project_panel.header_text())
        app._set_offline(False)
        # кнопка открывает окно «Отправить на сервер»
        opened = []

        class FakeDialog:
            def __init__(self, app_, folder):
                opened.append(folder)
                self.result = None
        with mock.patch("svnhelper.gui.projects_tab.PublishDialog", FakeDialog), mock.patch.object(root, "wait_window"):
            app.publish_btn.invoke()
        self.assertEqual(opened, [str(self.folder)])

    def test_history_checks_do_not_touch_connection_state(self):
        root, app = self.make_app()
        app._set_offline(True)
        iid, item = self.select(root, app)
        app._check_status([item])
        self.pump(root, lambda: item.status is not None and not app._checking)
        self.assertTrue(app._offline)                  # локальная проверка не «возвращает связь»

    def test_archive_after_publish_is_deleted_only_on_request(self):
        from svnhelper import publish, server
        root, app = self.make_app()
        parent = server.build_url(self.server_repo.as_uri(), ["konstruktor", "projects"])
        subprocess.run(["svn", "mkdir", "--parents", "-m", "место", parent], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rules = self.localrepo.local_rules(app.config.filter)
        publish.publish(app.client, str(self.folder), parent, "Отправка", rules=rules,
                        jobs_dir=Path(self.tmp.name) / "jobs", retry_delay=0)
        self.rescan(root, app)
        iid, item = self.select(root, app)
        self.checked(root, app, item)
        self.assertFalse(item.local)                                           # теперь это проект на сервере
        self.assertEqual(item.archive, self.result.repo)                       # история осталась архивом
        self.assertTrue(os.path.isdir(self.result.repo))
        self.assertFalse(self.save_local_shown(app))                           # это уже не история на компьютере
        index = app._archive_idx
        self.assertIsNotNone(index)
        self.assertEqual(app.more_menu.entrycget(index, "label"), "Удалить прежние сохранения на этом компьютере…")
        self.assertEqual(str(app.more_menu.entrycget(index, "state")), "normal")
        with mock.patch("tkinter.messagebox.askyesno", return_value=False):
            app._on_delete_archive()
        self.assertTrue(os.path.isdir(self.result.repo))                       # отказались - всё на месте
        with mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask, \
                mock.patch("tkinter.messagebox.showinfo") as info:
            app._on_delete_archive()
            self.pump(root, lambda: info.call_count)
        self.assertIn(self.FOLDER, ask.call_args.args[1])
        self.assertFalse(os.path.exists(self.result.repo))
        self.assertEqual(app.settings.local_histories, {})
        self.assertEqual(item.archive, "")
        self.assertIsNone(app._archive_idx)                                    # архива больше нет - нет и пункта
        self.assertEqual(app.more_menu.index("end"), app._more_idx["experts"])
        self.assertTrue((self.folder / ".svn").is_dir())                       # проект на сервере цел

    def test_archive_menu_entry_is_absent_for_project_in_use(self):
        root, app = self.make_app()
        iid, item = self.select(root, app)
        self.assertTrue(item.local)
        self.assertIsNone(app._archive_idx)
        self.assertEqual(sorted(app._more_idx), ["experts", "publish"])

    def test_publish_refusal_for_unsaved_changes_saves_and_sends_in_one_step(self):
        """Ревью 2026-11-02: при отказе из-за несохранённых правок окно не закрывается, а по «Да» сохраняет правки у себя
        и сразу отправляет проект."""
        from svnhelper import server
        from svnhelper.gui import App
        from svnhelper.gui.new_project_dialog import PublishDialog
        root, app = self.make_app()
        parent = server.build_url(self.server_repo.as_uri(), ["konstruktor", "projects"])
        subprocess.run(["svn", "mkdir", "--parents", "-m", "место", parent], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (self.folder / "README.txt").write_text("правка", encoding="utf-8")
        (self.folder / "новый файл.txt").write_text("новый", encoding="utf-8")
        dialog = PublishDialog(app, str(self.folder), jobs_dir=Path(self.tmp.name) / "jobs")
        dialog.segments = ["konstruktor", "projects"]
        dialog._refresh()
        with mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask, \
                mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.send_btn.invoke()
            self.pump(root, lambda: dialog.result is not None, timeout=60)
        self.assertIn("Сохранить их у себя и сразу отправить", ask.call_args.args[1])
        self.assertEqual(ask.call_count, 1)
        info.assert_called_once()                                             # «Проект отправлен»
        App.show_error.assert_not_called()
        trunk = dialog.result.project_url + "/trunk"
        self.assertEqual(app.client.cat(trunk + "/README.txt"), "правка".encode("utf-8"))
        self.assertEqual(app.client.cat(trunk + "/новый файл.txt"), "новый".encode("utf-8"))
        # правки остались и в прежней истории: сохранение № 3 сделано до отправки
        self.assertEqual(int(subprocess.run(["svnlook", "youngest", self.result.repo], stdout=subprocess.PIPE)
                             .stdout.decode().strip()), 3)

    def test_publish_refusal_answer_no_keeps_dialog_open(self):
        from svnhelper.gui.new_project_dialog import PublishDialog
        root, app = self.make_app()
        (self.folder / "README.txt").write_text("правка", encoding="utf-8")
        dialog = PublishDialog(app, str(self.folder), jobs_dir=Path(self.tmp.name) / "jobs")
        dialog.segments = ["konstruktor", "projects"]
        dialog._refresh()
        with mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            dialog.send_btn.invoke()
            self.pump(root, lambda: ask.call_count and dialog.state == "ready")
        self.assertTrue(dialog.winfo_exists())
        self.assertIsNone(dialog.result)
        dialog.destroy()


@unittest.skipUnless(HAS_DISPLAY and shutil.which("svn") and shutil.which("svnadmin"), "нет экрана или svn")
class LocalHistoryDialogTests(unittest.TestCase):
    """T.6: новый проект «на компьютере» получает историю, если найден svnadmin.exe; без него - без истории и без ошибки."""

    pump = NewProjectDialogTests.pump
    make_app = NewProjectDialogTests.make_app
    open_dialog = NewProjectDialogTests.open_dialog
    DEST = "261028_вафли"

    def setUp(self):
        NewProjectDialogTests.setUp(self)
        self.addCleanup(gc.collect)           # последним: мусор окон собирает главный поток, а не фоновый

    def create(self, svnadmin):
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        root, app = self.make_app()
        dialog = self.open_dialog(app)
        dialog.client = SvnClient("svn")          # шаблон загружен на заглушке, историю ведёт настоящий svn
        dialog.field_vars["date"].set("261028")
        dialog.field_vars["name"].set("вафли")
        App.show_error.reset_mock()
        with mock.patch("svnhelper.config.find_svnadmin", return_value=svnadmin), \
                mock.patch("tkinter.messagebox.showinfo") as info:
            dialog.create_btn.invoke()
            self.pump(root, lambda: dialog.result is not None, timeout=60)
        return root, app, dialog, info

    def test_new_local_project_gets_history_when_svnadmin_found(self):
        from svnhelper.svn_core import SvnClient
        root, app, dialog, info = self.create(shutil.which("svnadmin"))
        dest = os.path.join(self.base, self.DEST)
        self.assertEqual(dialog.result, dest)
        info.assert_not_called()
        client = SvnClient("svn")
        wc = client.info(dest)
        self.assertTrue(wc.url.startswith("file:///") and wc.url.endswith("/trunk"))
        self.assertEqual(wc.revision, 2)
        self.assertEqual(client.status(dest).local_changes, [])
        self.assertEqual(sorted(os.listdir(self.base)), [".svnlite", self.DEST])
        self.assertEqual(len(app.settings.local_histories), 1)                 # путь в settings.json, не в project.json
        self.assertNotIn(".svnlite", Path(dest, "project.json").read_text("utf-8"))
        self.assertEqual(list(self.jobs.glob("*.json")), [])
        # в списке проект показан как хранящийся на этом компьютере
        app.client = client                          # список проектов читает настоящий svn, а не заглушку
        app._show_new_project(dest)
        self.pump(root, lambda: app.mine and app.mine_tree.selection())
        self.assertTrue(app.mine[app.mine_tree.selection()[0]].local)

    def test_no_svnadmin_means_no_history_and_no_error(self):
        from svnhelper.gui import App
        root, app, dialog, info = self.create(None)
        dest = os.path.join(self.base, self.DEST)
        self.assertEqual(dialog.result, dest)
        self.assertEqual(sorted(os.listdir(self.base)), [self.DEST])           # ни .svnlite, ни .svn
        self.assertFalse(os.path.exists(os.path.join(dest, ".svn")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "project.json")))
        info.assert_not_called()
        App.show_error.assert_not_called()
        self.assertEqual(app.settings.local_histories, {})

    def test_history_failure_keeps_project_without_history(self):
        from svnhelper.gui import App
        root, app, dialog, info = self.create(os.path.join(self.base, "нет такой программы.exe"))
        dest = os.path.join(self.base, self.DEST)
        self.assertEqual(dialog.result, dest)
        self.assertEqual(sorted(os.listdir(self.base)), [self.DEST])           # созданное для истории убрано
        self.assertTrue(os.path.isfile(os.path.join(dest, "project.json")))
        self.assertIn("историю на этом компьютере включить не удалось", info.call_args.args[1])
        App.show_error.assert_not_called()
        self.assertEqual(list(self.jobs.glob("*.json")), [])


@unittest.skipUnless(HAS_DISPLAY, "нет экрана")
class AgentDialogTests(unittest.TestCase):
    """AG.7: окно «Справка → ИИ-помощник…»: состояние политики, папки, команды подключения, журнал."""

    def setUp(self):
        import json
        self.json = json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(gc.collect)

    def make_app(self, scale=None, agent_max_mode="read_only"):
        from svnhelper.config import Config, SettingsStore
        from svnhelper.gui import App
        from svnhelper.svn_core import SvnClient
        root = tk.Tk()
        if scale is not None:
            root.tk.call("tk", "scaling", 96 * scale / 72)
        root.withdraw()
        quiet_clipboard(root)
        self.addCleanup(self.destroy_root, root)
        store = SettingsStore(Path(self.tmp.name) / "s.json")
        store.settings.project_dirs = [os.path.join(self.tmp.name, "Проекты")]
        cfg = Config(server="svn://srv", repos=["test"], agent_max_mode=agent_max_mode)
        app = App(root, cfg, store, SvnClient("svn"), os.path.join(self.tmp.name, "log.txt"))
        return root, app

    @staticmethod
    def destroy_root(root):
        try:
            root.destroy()
        except tk.TclError:                  # тест уже закрыл окно сам
            pass

    def open_dialog(self, **kwargs):
        root, app = self.make_app(**kwargs)
        app._open_agent_dialog()
        return root, app, app._agent_dialog

    def test_menu_item_opens_one_dialog(self):
        root, app = self.make_app()
        self.assertEqual(app.help_menu.entrycget("ИИ-помощник…", "state"), "normal")
        app.help_menu.invoke(app.help_menu.index("ИИ-помощник…"))
        first = app._agent_dialog
        self.assertTrue(first.winfo_exists())
        app._open_agent_dialog()
        self.assertIs(app._agent_dialog, first)                     # повторно окно не плодится

    def test_no_policy_file_shows_view_only_and_switches_are_off_and_disabled(self):
        from svnhelper import agent_policy
        root, app, dialog = self.open_dialog()
        self.assertEqual(dialog.policy.signature, agent_policy.SIG_NO_FILE)
        self.assertIn("только смотреть", dialog.mode_var.get())
        self.assertIn("не задана", dialog.signature_var.get())
        self.assertEqual(dialog.note_label.cget("text"), "")
        self.assertEqual(len(dialog.permission_boxes), 6)
        for action, box in dialog.permission_boxes.items():
            self.assertEqual(str(box.cget("state")), "disabled", action)
            self.assertFalse(dialog.permission_vars[action].get(), action)
            self.assertTrue(box.cget("text").startswith("Разрешить помощнику: "))
        self.assertEqual(dialog.fixed_var.get(), app.settings.project_dirs[0])
        self.assertEqual(str(dialog.add_btn.cget("state")), "normal")
        self.assertFalse(self.data.joinpath("agent_policy.json").exists())        # открытие окна ничего не пишет

    def test_ceiling_off_is_shown_and_overrides_mode(self):
        root, app, dialog = self.open_dialog(agent_max_mode="off")
        self.assertIn("выключен", dialog.ceiling_var.get())
        self.assertIn("выключен администратором", dialog.mode_var.get())

    def write_raw_policy(self, data):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "agent_policy.json").write_text(self.json.dumps(data), encoding="utf-8")

    def test_tampered_signature_is_shown_and_folders_locked(self):
        from svnhelper import agent_policy
        from svnhelper.ops import texts
        agent_policy.write({"mode": "write", "allow": ["lock"]})
        path = self.data / "agent_policy.json"
        data = self.json.loads(path.read_text("utf-8"))
        data["allow"] = ["lock", "publish"]                                    # правка не через программу
        path.write_text(self.json.dumps(data), encoding="utf-8")
        root, app, dialog = self.open_dialog()
        self.assertEqual(dialog.signature_var.get(), "изменена")
        self.assertEqual(dialog.note_label.cget("text"), texts.POLICY_TAMPERED)
        self.assertIn("только смотреть", dialog.mode_var.get())                # запись выключена
        self.assertEqual(str(dialog.add_btn.cget("state")), "disabled")
        self.assertEqual(str(dialog.remove_btn.cget("state")), "disabled")
        self.assertTrue(dialog.folders_hint.winfo_manager())

    def test_foreign_signature_asks_to_enable_again(self):
        import base64
        from svnhelper import agent_policy
        from svnhelper.ops import texts
        foreign = base64.b64encode(agent_policy._DPAPI_HEADER + b"\x00" * 40).decode("ascii")
        self.write_raw_policy({"format": 1, "mode": "write", "allow": ["lock"], "sig": foreign})
        root, app, dialog = self.open_dialog()
        self.assertEqual(dialog.signature_var.get(), "нужно включить заново")
        self.assertEqual(dialog.note_label.cget("text"), texts.POLICY_FOREIGN)
        self.assertEqual(str(dialog.add_btn.cget("state")), "disabled")

    def test_allowed_permissions_are_shown_but_not_editable(self):
        from svnhelper import agent_policy
        agent_policy.write({"mode": "write", "allow": ["lock", "save_local"]})
        root, app, dialog = self.open_dialog()
        self.assertEqual(dialog.signature_var.get(), "нормально")
        self.assertTrue(dialog.permission_vars["lock"].get() and dialog.permission_vars["save_local"].get())
        self.assertFalse(dialog.permission_vars["publish"].get())
        self.assertTrue(all(str(b.cget("state")) == "disabled" for b in dialog.permission_boxes.values()))

    def test_connection_commands_are_copied(self):
        from svnhelper.gui import agent_dialog
        root, app, dialog = self.open_dialog()
        with mock.patch.object(dialog, "clipboard_clear"), mock.patch.object(dialog, "clipboard_append") as append:
            dialog.copy_code_btn.invoke()
            command = append.call_args.args[0]
            dialog.copy_desktop_btn.invoke()
            snippet = append.call_args.args[0]
        self.assertTrue(command.startswith('claude mcp add --scope user svnlite -- "'), command)
        self.assertTrue(command.endswith('" mcp'), command)
        self.assertEqual(command, agent_dialog.claude_code_command())
        config = self.json.loads(snippet)["mcpServers"]["svnlite"]
        self.assertEqual(config["args"][-1], "mcp")
        self.assertEqual(config["command"], agent_dialog.cli_command()[0])
        self.assertIn("скопирована", dialog.status_var.get())

    def test_connection_command_for_frozen_program(self):
        from svnhelper.gui import agent_dialog
        exe = r"C:\Program Files\SVN-lite\SVN-lite.exe"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe):
            parts = agent_dialog.cli_command()
            self.assertEqual(parts, [str(Path(r"C:\Program Files\SVN-lite\svnlite.exe").resolve())])
            self.assertEqual(agent_dialog.claude_code_command(),
                             f'claude mcp add --scope user svnlite -- "{parts[0]}" mcp')
            snippet = self.json.loads(agent_dialog.claude_desktop_json())
            self.assertEqual(snippet, {"mcpServers": {"svnlite": {"command": parts[0], "args": ["mcp"]}}})

    def test_open_journal_opens_agent_folder(self):
        root, app, dialog = self.open_dialog()
        with mock.patch("os.startfile", create=True) as start:
            dialog.log_btn.invoke()
        start.assert_called_once_with(self.data / "agent")
        self.assertTrue((self.data / "agent").is_dir())

    def test_add_and_remove_folder_keep_other_policy_values(self):
        from svnhelper import agent_policy
        agent_policy.write({"mode": "write", "allow": ["lock"], "max_writes_per_hour": 7})
        folder = os.path.join(self.tmp.name, "Чертежи 2026")
        os.makedirs(folder)
        root, app, dialog = self.open_dialog()
        with mock.patch("tkinter.filedialog.askdirectory", return_value=folder.replace("\\", "/")):
            dialog.add_btn.invoke()
        policy = agent_policy.Policy.load()
        self.assertEqual(policy.signature, agent_policy.SIG_OK)
        self.assertEqual(policy.allowed_dirs, (os.path.normpath(folder),))
        self.assertEqual((policy.file_mode, policy.allow, policy.max_writes_per_hour), ("write", ("lock",), 7))
        self.assertEqual(dialog.folder_list.get(0, "end"), (os.path.normpath(folder),))
        with mock.patch("tkinter.filedialog.askdirectory", return_value=folder):
            dialog.add_btn.invoke()                                            # повтор не дублирует
        self.assertEqual(len(agent_policy.Policy.load().allowed_dirs), 1)
        dialog.folder_list.selection_set(0)
        dialog.remove_btn.invoke()
        self.assertEqual(agent_policy.Policy.load().allowed_dirs, ())
        self.assertEqual(agent_policy.Policy.load().allow, ("lock",))

    def test_admin_change_after_dialog_opened_is_not_rolled_back(self):
        """Администратор изменил политику (режим, разрешения, пределы, список папок), пока окно открыто: окно добавляет
        и убирает папку в свежем списке и не возвращает старые значения."""
        from svnhelper import agent_policy
        first, second, added = (os.path.join(self.tmp.name, name) for name in ("первая", "вторая", "новая"))
        for folder in (first, second, added):
            os.makedirs(folder)
        agent_policy.write({"mode": "read_only", "allow": [], "allowed_dirs": [first], "max_writes_per_hour": 7})
        root, app, dialog = self.open_dialog()
        self.assertEqual(dialog.folder_list.get(0, "end"), (first,))
        agent_policy.write({"mode": "write", "allow": ["lock"], "allowed_dirs": [first, second],
                            "max_writes_per_hour": 3})               # правка администратора после открытия окна
        with mock.patch("tkinter.filedialog.askdirectory", return_value=added):
            dialog.add_btn.invoke()
        policy = agent_policy.Policy.load()
        self.assertEqual(policy.allowed_dirs, (first, second, os.path.normpath(added)))     # вторая папка не потеряна
        self.assertEqual((policy.file_mode, policy.allow, policy.max_writes_per_hour), ("write", ("lock",), 3))
        self.assertEqual(dialog.folder_list.get(0, "end"), policy.allowed_dirs)              # окно показало свежее
        agent_policy.write({"mode": "write", "allow": ["lock"], "allowed_dirs": [second, os.path.normpath(added)],
                            "max_writes_per_hour": 3})               # администратор убрал первую папку
        dialog.folder_list.selection_clear(0, "end")
        dialog.folder_list.selection_set(1)                          # в окне это «вторая» (индекс 1 устаревшего списка)
        dialog.remove_btn.invoke()
        self.assertEqual(agent_policy.Policy.load().allowed_dirs, (os.path.normpath(added),))   # убрана именно «вторая»

    def test_folder_change_is_refused_when_policy_became_invalid_after_dialog_opened(self):
        from svnhelper import agent_policy
        agent_policy.write({"mode": "write", "allow": ["lock"]})
        root, app, dialog = self.open_dialog()
        path = self.data / "agent_policy.json"
        data = self.json.loads(path.read_text("utf-8"))
        data["allow"] = ["lock", "publish"]                                    # правка не через программу
        raw = self.json.dumps(data)
        path.write_text(raw, encoding="utf-8")
        with mock.patch("tkinter.filedialog.askdirectory", return_value=self.tmp.name),                 mock.patch("tkinter.messagebox.showwarning") as warning:
            self.assertEqual(str(dialog.add_btn.cget("state")), "normal")      # окно ещё не знало о правке
            dialog.add_btn.invoke()
        warning.assert_called_once()
        self.assertIn("Папки не изменены", warning.call_args.args[1])
        self.assertEqual(path.read_text("utf-8"), raw)                         # чужую правку окно не перезаписало
        self.assertEqual(str(dialog.add_btn.cget("state")), "disabled")        # и показало состояние заново

    def test_network_folder_is_refused_at_once_with_clear_message(self):
        from svnhelper import agent_policy
        root, app, dialog = self.open_dialog()
        with mock.patch("tkinter.filedialog.askdirectory", return_value="//server/share/проекты"),                 mock.patch("tkinter.messagebox.showwarning") as warning,                 mock.patch("svnhelper.agent_policy.write") as write:
            dialog.add_btn.invoke()
        write.assert_not_called()
        warning.assert_called_once()
        self.assertIn("сетевому адресу", warning.call_args.args[1])
        self.assertFalse(self.data.joinpath("agent_policy.json").exists())
        self.assertEqual(agent_policy.Policy.load().allowed_dirs, ())

    def test_source_run_uses_python_exe_not_pythonw(self):
        from svnhelper.gui import agent_dialog
        scripts = Path(self.tmp.name) / "Python"
        scripts.mkdir()
        (scripts / "python.exe").write_bytes(b"")
        (scripts / "pythonw.exe").write_bytes(b"")
        with mock.patch.object(sys, "executable", str(scripts / "pythonw.exe")):
            self.assertEqual(agent_dialog.cli_command()[0], str(scripts / "python.exe"))
        with mock.patch.object(sys, "executable", str(scripts / "python.exe")):
            self.assertEqual(agent_dialog.cli_command()[0], str(scripts / "python.exe"))
        (scripts / "python.exe").unlink()
        with mock.patch.object(sys, "executable", str(scripts / "pythonw.exe")):
            self.assertEqual(agent_dialog.cli_command()[0], str(scripts / "pythonw.exe"))   # соседа нет: что есть

    def test_copy_buttons_are_disabled_when_cli_exe_is_missing(self):
        from svnhelper.gui import agent_dialog
        root, app, dialog = self.open_dialog()
        self.assertEqual(str(dialog.copy_code_btn.cget("state")), "normal")          # из исходников: python есть
        exe_dir = Path(self.tmp.name) / "SVN-lite"
        exe_dir.mkdir()
        (exe_dir / "SVN-lite.exe").write_bytes(b"")
        with mock.patch.object(sys, "frozen", True, create=True),                 mock.patch.object(sys, "executable", str(exe_dir / "SVN-lite.exe")):
            dialog._refresh()
            for button in (dialog.copy_code_btn, dialog.copy_desktop_btn):
                self.assertEqual(str(button.cget("state")), "disabled")
            self.assertIn(agent_dialog.CLI_MISSING_TEXT, dialog.cli_label.cget("text"))
            (exe_dir / "svnlite.exe").write_bytes(b"")                                # администратор положил файл
            dialog._refresh()
            for button in (dialog.copy_code_btn, dialog.copy_desktop_btn):
                self.assertEqual(str(button.cget("state")), "normal")

    def test_add_folder_without_policy_file_signs_view_only_policy(self):
        from svnhelper import agent_policy
        folder = os.path.join(self.tmp.name, "общая")
        os.makedirs(folder)
        root, app, dialog = self.open_dialog()
        with mock.patch("tkinter.filedialog.askdirectory", return_value=folder):
            dialog.add_btn.invoke()
        policy = agent_policy.Policy.load()
        self.assertEqual((policy.signature, policy.file_mode, policy.allow), ("ok", "read_only", ()))
        self.assertEqual(policy.allowed_dirs, (os.path.normpath(folder),))
        self.assertIn("только смотреть", dialog.mode_var.get())

    def test_folder_write_failure_is_reported_not_swallowed(self):
        root, app, dialog = self.open_dialog()
        with mock.patch("tkinter.filedialog.askdirectory", return_value=self.tmp.name), \
                mock.patch("svnhelper.agent_policy.write", side_effect=OSError("диск защищён от записи")), \
                mock.patch("tkinter.messagebox.showerror") as error:
            dialog.add_btn.invoke()
        error.assert_called_once()
        self.assertIn("Не удалось записать настройки помощника", error.call_args.args[1])
        self.assertEqual(dialog.folder_list.get(0, "end"), ())

    def test_dialog_fits_screen_at_125_and_150_percent_and_keeps_main_window_geometry(self):
        for scale in (1.25, 1.5):
            root, app = self.make_app(scale=scale)
            root.update_idletasks()
            before = (app._min_width, app._min_height, root.minsize())
            app._open_agent_dialog()
            dialog = app._agent_dialog
            # самый длинный вариант: тревога про подпись и строка про скопированное
            dialog.note_label.configure(text="x " * 100)
            dialog.note_label.grid()
            dialog.status_var.set("y " * 100)
            dialog.status_label.pack()
            dialog.update_idletasks()
            self.assertLessEqual(dialog.winfo_reqheight(), root.winfo_screenheight() - 60, scale)
            self.assertLessEqual(dialog.winfo_reqwidth(), root.winfo_screenwidth(), scale)
            self.assertEqual(before, (app._min_width, app._min_height, root.minsize()))   # окно программы не менялось
            dialog.destroy()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
