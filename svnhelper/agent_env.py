"""Среда процесса помощника (`svnlite.exe` и `svnlite.exe mcp`): закрывает обходы защиты до записи.

`harden()` вызывают `cli.main` и `mcp_server.main`, когда запускается настоящий процесс (тесты передают свой контекст и
эту функцию не вызывают). Что она делает:

* **SVNLITE_DATA_DIR.** Переменная переключает папку данных (настройки, политика, счётчик записей, журнал, замки) и нужна
  только демо-режиму окна и тестам. Собранный `svnlite.exe`, запущенный помощником с этой переменной, направил бы
  политику, счётчик и журнал в чужую папку и обошёл бы их. У собранного exe (`sys.frozen`) переменная игнорируется:
  она удаляется из среды до первого обращения к `config.data_dir()`.
* **Строгий режим замков.** Без каталога замков (`folderlock.set_strict`) изменяющая операция отказывает (код 8), а не
  работает «на авось».
* **Объект «задание» Windows.** `svn.exe` завершается вместе с программой (`jobobject`).
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from . import folderlock, jobobject

log = logging.getLogger("svnhelper.agent_env")

DATA_DIR_VAR = "SVNLITE_DATA_DIR"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def ignore_data_dir_override(frozen: Optional[bool] = None) -> bool:
    """У собранной программы переменную SVNLITE_DATA_DIR убирает из среды. True, если она была и убрана."""
    if not (is_frozen() if frozen is None else frozen):
        return False
    value = os.environ.pop(DATA_DIR_VAR, None)
    if value:
        log.warning("Переменная %s проигнорирована: у собранной программы папка данных не переключается", DATA_DIR_VAR)
        return True
    return False


def harden(frozen: Optional[bool] = None, bind_children: bool = True) -> dict:
    """Подготовка среды настоящего процесса помощника. Возвращает, что сделано (для журнала и тестов)."""
    removed = ignore_data_dir_override(frozen)
    folderlock.set_strict(True)
    bound = jobobject.bind_children_to_process() if bind_children else False
    return {"data_dir_override_removed": removed, "strict_locks": True, "children_bound": bound}
