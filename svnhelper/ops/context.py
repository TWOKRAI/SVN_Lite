"""Контекст операций: настройки, конфигурация, клиент svn без учётных данных, политика.

Пароль помощнику не нужен: клиент создаётся без логина и пароля (svn берёт свой кэш входа), а `store_auth=False`
запрещает ему что-либо запоминать. `effective_credentials` и DPAPI здесь не вызываются. Сеть при создании
контекста не используется: общий конфиг берётся из копии на диске (`load_shared_cache`).
"""
from __future__ import annotations

import contextlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Iterator, Optional

from ..config import Config, SettingsStore, build_config, find_svn, load_shared_cache
from ..svn_core import CancelToken, ErrorKind, SvnClient, SvnError

log = logging.getLogger("svnhelper.ops")

OP_TIMEOUT = 120.0          # секунд на всю работу одной операции со svn; дольше - ответ «сервер не отвечает»
CONNECT_TIMEOUT = 10.0      # секунд на проверку связи в doctor


class ReadOnlyPolicy:
    """Заглушка политики: помощник только смотрит. Настоящую политику (режим, папки, лимиты) подключает AG.4."""

    mode = "read_only"
    allowed_dirs: tuple = ()

    def describe(self) -> dict:
        return {"mode": self.mode}


@dataclass
class OpsContext:
    store: SettingsStore
    config: Config
    client: Optional[SvnClient]
    policy: object = field(default_factory=ReadOnlyPolicy)
    client_name: str = "cli"                    # cli | mcp | test
    timeout: float = OP_TIMEOUT
    connect_timeout: float = CONNECT_TIMEOUT

    @property
    def settings(self):
        return self.store.settings

    @property
    def project_dirs(self) -> list:
        """Папки для проектов из настроек и разрешённые политикой (без повторов, по порядку)."""
        dirs = list(self.settings.project_dirs) + [d for d in getattr(self.policy, "allowed_dirs", ()) if d]
        return list(dict.fromkeys(dirs))


def make_context(client: Optional[SvnClient] = None, store: Optional[SettingsStore] = None,
                 config: Optional[Config] = None, policy: object = None, client_name: str = "cli",
                 timeout: float = OP_TIMEOUT) -> OpsContext:
    """Собирает контекст. Всё, что не передано, берётся по умолчанию. svn.exe не найден: client=None, операции со
    svn отвечают кодом 9. Ничего не записывает на диск."""
    store = store or SettingsStore()
    if config is None:
        config = build_config(store.settings, load_shared_cache())
    if client is None:
        svn = find_svn(config.svn_path)
        if svn:
            client = SvnClient(svn)
            client.store_auth = False
        else:
            log.warning("svn.exe не найден: операции со svn недоступны")
    return OpsContext(store, config, client, policy if policy is not None else ReadOnlyPolicy(), client_name, timeout)


@contextlib.contextmanager
def deadline(ctx: OpsContext, seconds: Optional[float] = None) -> Iterator[CancelToken]:
    """Токен отмены, который сам срабатывает через seconds секунд (по умолчанию ctx.timeout). Если svn остановлен по
    времени, наружу выходит SvnError(TIMEOUT), а не «отменено»."""
    token = CancelToken()
    fired = threading.Event()

    def expire() -> None:
        fired.set()
        token.cancel()

    timer = threading.Timer(seconds if seconds is not None else ctx.timeout, expire)
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
