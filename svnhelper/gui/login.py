"""Одно окно входа на все фоновые задачи и текст ошибки подключения."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .. import messages
from ..svn_core import ErrorKind
from .models import JobStamp, MAX_LOGIN_ATTEMPTS
from .widgets import LoginDialog


@dataclass
class _Waiter:
    retry: Callable[[], None]
    give_up: Callable[[], None]
    job_gen: Optional["JobStamp"] = None


class LoginCoordinator:
    """Одно окно входа на все фоновые задачи, до 3 попыток подряд."""

    def __init__(self, app: "App"):
        self.app = app
        self.dialog_open = False
        self.attempts = 0
        self.epoch = 0  # растёт, когда окно входа закрыто без новых данных или попытки исчерпаны
        self.waiters: list[_Waiter] = []

    def reset_attempts(self) -> None:
        """Вызывается после успешного обращения к серверу с учётными данными: вход подтверждён."""
        self.attempts = 0
        self.app.mark_login_confirmed()

    def stamp(self) -> JobStamp:
        client = self.app.client
        return JobStamp(client.cred_generation if client else 0, self.epoch)

    def request(self, retry: Callable[[], None], give_up: Callable[[], None], job_gen: JobStamp) -> None:
        client = self.app.client
        if client is None:  # svn.exe убрали, пока задача работала: входить не во что
            give_up()
            return
        waiter = _Waiter(retry, give_up, job_gen)
        if client.cred_generation != job_gen.cred_gen:
            retry()  # данные уже ввели, пока задача работала
            return
        if job_gen.login_epoch != self.epoch:
            give_up()  # окно входа уже закрыли отменой, второй раз не спрашиваем
            return
        if client.credentials is not None:
            self.app.mark_login_failed()  # сервер отверг текущие данные: «Вы вошли как» больше неправда
        if self.dialog_open or self.app.error_open:
            self.waiters.append(waiter)  # окно входа не открываем поверх другого окна; flush() вернётся к ним
            return
        if self.attempts >= MAX_LOGIN_ATTEMPTS:
            self.attempts = 0
            self.epoch += 1
            self.app.show_error(
                "Не удалось войти",
                "Логин или пароль не подошли три раза подряд. Проверьте их и попробуйте позже "
                "или обратитесь к администратору.",
                action="Вход на сервер (три неверные попытки)", kind=ErrorKind.AUTH)
            give_up()
            return
        self.waiters.append(waiter)
        self._ask(retrying=client.credentials is not None)

    def flush(self) -> None:
        """Окно ошибки закрылось: ожидавшие входа задачи проходят проверки заново (окно входа откроется для первой)."""
        if not self.waiters or self.dialog_open or self.app.error_open:
            return
        waiters, self.waiters = self.waiters, []
        for w in waiters:
            if w.job_gen is None:
                w.retry()
            else:
                self.request(w.retry, w.give_up, w.job_gen)

    def _ask(self, retrying: bool) -> None:
        if retrying:
            text = (f"Неверный логин или пароль. Попробуйте ещё раз "
                    f"(попытка {self.attempts + 1} из {MAX_LOGIN_ATTEMPTS}).")
        else:
            text = "Для доступа к серверу нужны ваш логин и пароль. Их выдаёт администратор."
        self.app.reveal_window()      # из фона окно могло быть спрятано в трей: сначала показываем программу
        self.dialog_open = True
        try:
            dialog = LoginDialog(self.app.root, self.app.settings.username or self.app.config.username, text,
                                 remember=self.app.remember_default())
            self.app.root.wait_window(dialog)
        finally:
            self.dialog_open = False
            self.app.post(self.app.after_modal_closed)  # ошибки, дожидавшиеся закрытия окна входа
        waiters, self.waiters = self.waiters, []
        client = self.app.client
        if dialog.result is None or client is None:  # отмена, либо svn.exe убрали, пока окно было открыто
            self.attempts = 0
            self.epoch += 1
            for w in waiters:
                w.give_up()
            return
        user, password = dialog.result
        self.attempts += 1
        previous_user = self.app.settings.username or self.app.config.username
        self.app.settings.username = user
        remember = dialog.remember if isinstance(dialog.remember, bool) else self.app.remember_default()
        # пароль сохраняется только зашифрованным и по желанию; пароль прежнего логина новому не достаётся
        self.app.apply_remember(password, remember, reset_saved=not password and user != previous_user)
        client.set_credentials(user, password)
        self.app._update_login_label()
        for w in waiters:
            w.retry()


def connection_error_text(kind: ErrorKind, repo: str, has_login: bool) -> str:
    """Итог «Проверить подключение» при ошибке: что случилось и что делать."""
    if kind is ErrorKind.AUTH:
        return ("Неверный логин или пароль. Проверьте их и попробуйте ещё раз." if has_login else
                "Сервер просит логин и пароль. Введите их выше и проверьте ещё раз.")
    if kind is ErrorKind.AUTHZ:
        return f"Вход выполнен, но нет доступа к папке «{repo}». Обратитесь к администратору."
    if kind is ErrorKind.NO_CONNECTION:
        return "Нет связи с сервером. Проверьте сеть или VPN и адрес сервера."
    if kind is ErrorKind.TIMEOUT:
        return "Сервер не отвечает. Проверьте сеть или VPN и попробуйте позже."
    if kind is ErrorKind.NOT_FOUND:
        return f"Папка «{repo}» не найдена на сервере. Проверьте адрес сервера и список папок."
    return messages.short_message(kind) + " Подробности записаны в журнал."
