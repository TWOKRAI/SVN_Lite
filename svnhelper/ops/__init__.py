"""Слой операций для ИИ-помощника: консольная программа (svnlite) и сервер MCP вызывают одни и те же функции.

Каждая операция чтения получает контекст (`make_context`) и возвращает `Result` (словарь `Result.to_dict()`
одинаков для всех потребителей). Реестр операций с описаниями словами сотрудника: `registry`.
Пакет не импортирует tkinter и svnhelper.gui.
"""
from __future__ import annotations

from . import diag, projects, remote            # noqa: F401  (операции регистрируются при импорте)
from .context import OpsContext, ReadOnlyPolicy, make_context
from .registry import REGISTRY, Operation, Param, all_operations, get, run
from .result import ErrorInfo, Result

__all__ = ["ErrorInfo", "OpsContext", "Operation", "Param", "REGISTRY", "ReadOnlyPolicy", "Result", "all_operations",
           "get", "make_context", "run"]
