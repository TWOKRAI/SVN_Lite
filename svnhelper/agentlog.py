"""Журнал консольной программы и сервера для ИИ-агента.

Окно пишет в svn_helper.log (config.setup_logging), агент - в свой файл: %APPDATA%\\SvnHelper\\logs\\svnlite_agent_ГГГГ-ММ.log.
Один файл на месяц, дописывается обычным FileHandler. RotatingFileHandler здесь нельзя: два процесса
(консоль и сервер агента) пишут в один файл, а ротация общего файла в Windows падает с PermissionError.
В стандартный вывод журнал не пишет: у сервера для агента на нём живёт протокол.
"""
from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import data_dir
from .messages import redact

LOG_PREFIX = "svnlite_agent_"
KEEP_MONTHS = 3            # файлы старше этого срока удаляются при старте
_NAME_RE = re.compile(r"^svnlite_agent_(\d{4})-(\d{2})\.log$")
_MARK = "_svnlite_agent_handler"

log = logging.getLogger("svnhelper.agentlog")


class _RedactingFormatter(logging.Formatter):
    """Убирает пароли из каждой строки журнала (в том числе из текста ошибки)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def agent_log_name(now: datetime) -> str:
    return f"{LOG_PREFIX}{now:%Y-%m}.log"


def _month_index(year: int, month: int) -> int:
    return year * 12 + month


def _remove_old_logs(folder: Path, now: datetime) -> None:
    """Удаляет журналы старше KEEP_MONTHS месяцев. Не вышло (файл занят другой программой): пишет в журнал и идёт дальше."""
    limit = _month_index(now.year, now.month) - KEEP_MONTHS
    for path in folder.glob(f"{LOG_PREFIX}*.log"):
        match = _NAME_RE.match(path.name)
        if match and _month_index(int(match.group(1)), int(match.group(2))) < limit:
            try:
                path.unlink()
            except OSError:
                log.exception("Не удалось удалить старый журнал %s", path)


def setup(client: str, now: Optional[datetime] = None) -> Path:
    """Подключает журнал агента к логгеру "svnhelper". client: "cli" или "mcp" (попадает в каждую строку).

    Другие обработчики (в том числе svn_helper.log) не трогает. Повторный вызов заменяет прежний обработчик агента.
    Возвращает путь к файлу журнала.
    """
    now = now or datetime.now()
    folder = data_dir() / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = Path(tempfile.gettempdir())      # как у окна: журнал важнее места
    path = folder / agent_log_name(now)

    root = logging.getLogger("svnhelper")
    for handler in [h for h in root.handlers if getattr(h, _MARK, False)]:
        root.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    setattr(handler, _MARK, True)
    handler.setFormatter(_RedactingFormatter(f"%(asctime)s %(levelname)s [{client}] %(name)s: %(message)s"))
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    _remove_old_logs(folder, now)
    return path
