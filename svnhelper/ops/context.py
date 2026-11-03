"""Контекст операций: настройки, конфигурация, клиент svn без учётных данных, политика.

Пароль помощнику не нужен: клиент создаётся без логина и пароля (svn берёт свой кэш входа), а `store_auth=False`
запрещает ему что-либо запоминать. `effective_credentials` и DPAPI здесь не вызываются, пароль из конфигурации
обнуляется сразу после чтения. Сеть при создании контекста не используется: общий конфиг берётся из копии на диске
(`load_shared_cache`).
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from ..agent_policy import Policy
from ..config import Config, SettingsStore, build_config, find_svn, load_shared_cache
from ..svn_core import CancelToken, ErrorKind, SvnClient, SvnError

log = logging.getLogger("svnhelper.ops")

OP_TIMEOUT = 120.0          # секунд на всю операцию (все вызовы svn вместе); дольше - ответ «сервер не отвечает»
CONNECT_TIMEOUT = 10.0      # секунд на проверку связи в doctor
LOCAL_TIMEOUT = 30.0        # секунд на чтение того, что лежит на компьютере, когда общий бюджет уже потрачен
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$")


def is_url(text: str) -> bool:
    """Похоже на адрес (svn://сервер/...): без него к серверу не обращаемся."""
    return bool(_URL_RE.match((text or "").strip()))


class ReadOnlyPolicy(Policy):
    """Политика «только чтение» без чтения файла (для тестов и потребителей, которым запись не нужна). Настоящая
    политика (режим, папки, лимиты, подпись) - `agent_policy.Policy.load`, её make_context берёт по умолчанию."""


@dataclass
class OpsContext:
    store: SettingsStore
    config: Config
    client: Optional[SvnClient]
    policy: object = field(default_factory=ReadOnlyPolicy)
    client_name: str = "cli"                    # cli | mcp | test
    timeout: float = OP_TIMEOUT
    connect_timeout: float = CONNECT_TIMEOUT
    started: float = 0.0                        # когда началась текущая операция (monotonic); 0 - не началась
    client_info: str = ""                       # имя клиента из clientInfo MCP (для журнала действий)
    audit: dict = field(default_factory=dict)   # сведения для журнала, которые операция кладёт сама (files, rev_after...)

    @property
    def settings(self):
        return self.store.settings

    @property
    def project_dirs(self) -> list:
        """Папки для проектов из настроек и разрешённые политикой (без повторов, по порядку)."""
        dirs = list(self.settings.project_dirs) + [d for d in getattr(self.policy, "allowed_dirs", ()) if d]
        return list(dict.fromkeys(dirs))

    def begin(self) -> "OpsContext":
        """Копия контекста с отметкой начала операции: общий бюджет времени считается от неё. Копия нужна, потому что
        один контекст (сервер MCP) обслуживает несколько операций одновременно. Политика из файла перечитывается:
        сервер живёт долго, а сотрудник меняет политику в окне. У копии свой словарь `audit`."""
        policy = self.policy.reload() if getattr(self.policy, "reloadable", False) else self.policy
        return dataclasses.replace(self, started=time.monotonic(), policy=policy, audit={})

    def remaining(self) -> float:
        """Сколько секунд осталось на операцию (без отметки начала - весь бюджет)."""
        if not self.started:
            return self.timeout
        return self.timeout - (time.monotonic() - self.started)


def make_context(client: Optional[SvnClient] = None, store: Optional[SettingsStore] = None,
                 config: Optional[Config] = None, policy: object = None, client_name: str = "cli",
                 timeout: float = OP_TIMEOUT, client_info: str = "") -> OpsContext:
    """Собирает контекст. Всё, что не передано, берётся по умолчанию. svn.exe не найден: client=None, операции со
    svn отвечают кодом 9. Ничего не записывает на диск. Клиент с логином и паролем не принимается (ValueError)."""
    store = store or SettingsStore()
    if config is None:
        config = build_config(store.settings, load_shared_cache())
    config.password = ""                        # помощнику пароль не нужен: дальше он нигде не окажется
    if client is not None and getattr(client, "credentials", None) is not None:
        raise ValueError("client with credentials is not allowed for the assistant")
    if client is None:
        svn = find_svn(config.svn_path)
        if svn:
            client = SvnClient(svn)
            client.store_auth = False
        else:
            log.warning("svn.exe не найден: операции со svn недоступны")
    if policy is None:
        policy = Policy.load(ceiling=config.agent_max_mode)         # файл политики и потолок администратора
    return OpsContext(store, config, client, policy, client_name, timeout, client_info=client_info)


@contextlib.contextmanager
def deadline(ctx: OpsContext, seconds: Optional[float] = None, fresh: bool = False) -> Iterator[CancelToken]:
    """Токен отмены, который сам срабатывает, когда выйдет время: остаток бюджета операции (`ctx.remaining()`) или
    seconds, если оно меньше. fresh=True: бюджет операции не учитывается, берётся LOCAL_TIMEOUT (чтение того, что лежит
    на компьютере, после того как сервер не ответил). Если svn остановлен по времени, наружу выходит
    SvnError(TIMEOUT), а не «отменено»."""
    limit = LOCAL_TIMEOUT if fresh else ctx.remaining()
    if seconds is not None:
        limit = min(limit, seconds)
    if limit <= 0:
        raise SvnError(ErrorKind.TIMEOUT, "Время операции вышло")
    token = CancelToken()
    fired = threading.Event()

    def expire() -> None:
        fired.set()
        token.cancel()

    timer = threading.Timer(limit, expire)
    timer.daemon = True
    timer.start()
    try:
        yield token
    except SvnError as exc:
        if fired.is_set() and exc.kind is ErrorKind.CANCELLED:
            raise SvnError(ErrorKind.TIMEOUT, "Превышено время ожидания операции") from exc
        raise
    finally:
        timer.cancel()
