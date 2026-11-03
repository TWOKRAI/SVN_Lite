"""Фоновая проверка и уведомления в окне (Task E.2): таймер, поток с svn, показ окошек, переход к проекту.

Правила потоков: svn вызывается только в фоновом потоке (run_bg), решения принимает Notifier, а окошки
создаются и закрываются только в главном потоке (результат потока приходит через App.post).
"""
from __future__ import annotations

import dataclasses
import os
import time
import tkinter as tk
from typing import Optional

from .. import __version__, messages, publish, tray
from ..config import add_project_dir, same_dir
from ..notifier import (check_projects, discover_projects, info_notice, KIND_INFO, Notice, Notifier, plural,
                        ProjectInfo, sample_notice, SNOOZE_HOURS)
from .models import log
from .popup import PopupStack

NOTICE_TICK_MS = 30_000        # как часто таймер смотрит, не пора ли проверять и не пора ли показать отложенное
OPEN_RETRY_MS = 500            # ждём, пока список проектов построится, чтобы выбрать нужный
OPEN_RETRIES = 20


class NotifyMixin:
    """Подмешивается в App: App.__init__ вызывает _notice_setup() в конце."""

    def _notice_setup(self) -> None:
        self.notifier = Notifier(lambda: self.settings)
        self.popups = PopupStack(self.root, lambda: self.palette, self.px, self._notice_open, self._notice_later)
        self._notice_checking = False                  # фоновая проверка идёт
        self._notice_force = False                     # ждёт «Проверить сейчас»
        self._notice_last_check: Optional[float] = None
        self._notice_snapshot_at = 0.0                 # когда (time.monotonic) сделан последний снимок проектов
        self.root.after(NOTICE_TICK_MS, self._notice_tick)

    # -- таймер -------------------------------------------------------------------

    def request_notice_check(self) -> None:
        """«Проверить сейчас» из меню значка: проверка начнётся, как только программа свободна."""
        self._notice_force = True
        self._notice_tick(reschedule=False)

    def _notice_tick(self, reschedule: bool = True) -> None:
        if self._closing:
            return
        if reschedule:
            self.root.after(NOTICE_TICK_MS, self._notice_tick)
        if self._notice_checking:
            return
        if self._notice_force or (self.settings.notify_enabled and self.notifier.due_for_check(self._notice_last_check)):
            if self._notice_can_check():
                self._notice_start_check()
            return
        if self.settings.notify_enabled:
            self._notice_show(force=False)     # вдруг то, что мешало (окно было в фокусе), уже не мешает

    def _notice_can_check(self) -> bool:
        """Не мешаем тяжёлым операциям: обновление, скачивание, сохранение, занятие файлов."""
        return bool(self.client) and not (self.job or self.upd or self._committing or self._locking
                                          or self._create_running() or self.login.dialog_open)

    def _notice_dirs(self) -> list[str]:
        dirs = list(self.settings.project_dirs)
        current = self.dir_var.get().strip() if hasattr(self, "dir_var") else ""
        return add_project_dir(dirs, current) if current else dirs

    def _busy_paths(self) -> frozenset:
        """Снимок занятых папок на этот момент (для фонового потока): окна TortoiseSVN, починка, а также
        проекты, в которых сейчас идёт получение нового или сохранение."""
        busy = {os.path.normcase(os.path.normpath(p)) for p in self._tortoise_open}
        if self.upd is not None:
            busy.add(os.path.normcase(os.path.normpath(self.upd.item.path)))
        if self._committing and self._commit_item is not None:
            busy.add(os.path.normcase(os.path.normpath(self._commit_item.path)))
        return frozenset(busy) | publish.busy_paths()   # отправка на сервер (тот же вид ключей)

    def _notice_start_check(self) -> None:
        manual, self._notice_force = self._notice_force, False
        self._notice_checking = True
        dirs = self._notice_dirs()
        client, rules, busy = self.client, self.config.filter, self._busy_paths()

        def work():
            projects = discover_projects(dirs)       # чтение папок может быть долгим (сетевой диск): в фоне
            return check_projects(client, projects, rules,
                                  skip=lambda path: os.path.normcase(os.path.normpath(path)) in busy
                                  or publish.is_busy(path))

        self.run_bg(work, lambda res, err, _stamp: self._notice_checked(res, err, manual))

    def _notice_checked(self, result, error, manual: bool) -> None:
        self._notice_checking = False
        self._notice_last_check = self.notifier.timestamp()
        if self._closing:
            return
        if error is not None:
            log.error("Фоновая проверка не удалась", exc_info=error)
            if manual:
                self._notice_info(messages.NOTICE_FAILED)
            return
        if result.auth_failed or result.offline:
            log.info("Фоновая проверка пропущена: %s", "нужен вход" if result.auth_failed else "нет связи")
            if manual:
                self._notice_info(messages.NOTICE_AUTH if result.auth_failed else messages.NOTICE_OFFLINE)
            return
        self._notice_snapshot_at = time.monotonic()
        self.notifier.update(result.projects)
        self._notice_show(force=manual)

    # -- показ --------------------------------------------------------------------

    def _window_active(self) -> bool:
        """Окно программы на экране и в фокусе: пользователь и так всё видит, окошки не нужны."""
        try:
            return self.root.state() == "normal" and self.root.focus_displayof() is not None
        except (tk.TclError, KeyError):
            return False

    def _live_infos(self) -> list[ProjectInfo]:
        """Снимок проектов, поправленный свежими данными вкладки «Мои проекты» (она обновляется чаще):
        сохранили правки или получили новое после проверки - напоминать об этом уже не нужно."""
        infos = []
        by_path = {os.path.normcase(os.path.normpath(i.path)): i for i in self.mine.values()}
        for info in self.notifier.infos:
            item = by_path.get(os.path.normcase(os.path.normpath(info.path)))
            if (item is not None and item.status is not None and item.checked and not item.checking
                    and item.last_check is not None and item.last_check > self._notice_snapshot_at):
                info = dataclasses.replace(info, incoming=max(len(item.status.incoming_files), item.status.incoming),
                                           unsaved=len(item.status.local_changes), conflicts=item.status.conflicts)
            infos.append(info)
        return infos

    def _notice_show(self, force: bool) -> None:
        if self._closing:
            return
        if not force and self._window_active():
            return                       # ничего не отмечаем показанным: покажем, когда окно уйдёт из фокуса
        self.notifier.update(self._live_infos())
        notices = self.notifier.candidates(self.config.latest_version, __version__, force=force)
        if force and not notices:
            notices = [info_notice()]
        seconds = self.settings.notify_popup_sec
        for notice in notices:
            self.popups.show(notice, seconds)
            self.notifier.mark_shown(notice)

    def _notice_info(self, text: str, title: str = messages.NOTICE_CHECK_TITLE) -> None:
        self.popups.show(Notice("info:" + text, KIND_INFO, title, text), self.settings.notify_popup_sec)

    def show_test_notice(self) -> None:
        """Кнопка «Проверить уведомления» в параметрах."""
        self.popups.show(sample_notice(), self.settings.notify_popup_sec)

    def toggle_snooze(self) -> None:
        """Пункт меню значка «Не беспокоить»: включает паузу на SNOOZE_HOURS часа, повторный выбор её снимает."""
        if self.notifier.snoozed:
            self.notifier.cancel_snooze()
            self._notice_info(messages.NOTICE_SNOOZE_OFF, "Не беспокоить")
            return
        self.notifier.snooze()
        self._notice_info(messages.NOTICE_SNOOZED.format(
            hours=SNOOZE_HOURS, unit=plural(SNOOZE_HOURS, ("час", "часа", "часов"))), "Не беспокоить")

    def tray_labels(self) -> dict:
        """Подписи пунктов меню значка, зависящие от состояния (вызывается из потока значка, только читает)."""
        until = self.notifier.snooze_until
        if until is None:
            return {}
        return {tray.EVENT_SNOOZE: messages.NOTICE_SNOOZE_MENU_ON.format(time=f"{until:%H:%M}")}

    def popups_open(self) -> bool:
        """На экране или в очереди есть окошки уведомлений."""
        return bool(self.popups.visible or self.popups.waiting)

    # -- кнопки окошка --------------------------------------------------------------

    def _notice_later(self, notice: Notice) -> None:
        self.notifier.postpone(notice)

    def _notice_open(self, notice: Notice) -> None:
        """«Открыть»: показывает программу и сразу нужный проект и вкладку."""
        self.show_window()
        if notice.path:
            self._open_notice_project(notice.path, notice.tab, 0)

    def _open_notice_project(self, path: str, tab: str, attempt: int) -> None:
        if self._closing:
            return
        item = next((i for i in self.mine.values() if same_dir(i.path, path)), None)
        if item is None:
            if attempt == 0:
                if self._list_mode() != "mine":
                    self.select_list_mode("mine")      # список проектов построится при открытии подвкладки
                if not same_dir(self.dir_var.get(), os.path.dirname(path)):
                    self._switch_project_dir(os.path.dirname(path))
            if attempt < OPEN_RETRIES:
                self.root.after(OPEN_RETRY_MS, self._open_notice_project, path, tab, attempt + 1)
            return
        iid = self._iid_of(item)
        if iid:
            self.mine_tree.selection_set(iid)
            self.mine_tree.focus(iid)
            self.mine_tree.see(iid)
            self._on_mine_select()
        if tab in self.project_tab_frames:
            self.open_project_tab(tab)
        elif self._list_mode() != "mine":
            self.select_list_mode("mine")
