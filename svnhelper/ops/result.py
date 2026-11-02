"""Единый вид ответа операций (CLI, MCP и помощник отвечают одним и тем же словарём) и чистка чужого текста.

Формат описан в docs/PLAN_agent_access.md, раздел «Формат ответов». Ключи и коды стабильные, английские;
`text` и `message` по-русски. Технические сведения (адрес, номера, коды E######, ответ svn после `messages.redact`)
лежат только в блоке `svn` и попадают в словарь только при `details=True`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA = 1
MAX_ENTRIES = 50            # записей коллег в одном ответе
MAX_COMMENT = 500           # знаков в комментарии коллеги

# управляющие и невидимые знаки (в том числе переключатели направления письма): ими прячут текст от человека
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]")

# коды возврата консольной программы
EXIT_OK = 0
EXIT_OTHER = 1
EXIT_ARGS = 2
EXIT_NO_CONNECTION = 3
EXIT_NEED_LOGIN = 4
EXIT_NO_RIGHTS = 5
EXIT_POLICY = 6
EXIT_HUMAN = 7
EXIT_FOLDER_BUSY = 8
EXIT_NO_SVN = 9


def clean_text(text: Any, limit: int = MAX_COMMENT) -> str:
    """Чужой текст (комментарий, имя автора или файла): без управляющих знаков, не длиннее limit знаков."""
    value = _CONTROL_RE.sub("", str(text if text is not None else "")).replace("\r\n", "\n").replace("\r", "\n")
    value = value.strip()
    if len(value) > limit:
        value = value[:limit].rstrip() + "…"
    return value


def one_line(text: str) -> str:
    """Текст в одну строку (для строки ответа в текстовом режиме)."""
    return " ".join(text.split())


@dataclass
class ErrorInfo:
    """Ошибка для человека и для программы: kind и exit_code стабильные, message и next_step по-русски."""
    kind: str
    message: str
    next_step: str = ""
    retryable: bool = False
    exit_code: int = EXIT_OTHER
    next_action: str = ""                                   # nothing | get_new | wait | human_login ...
    svn: dict = field(default_factory=dict)                 # технические подробности: только в блок svn ответа

    def to_dict(self) -> dict:
        data = {"kind": self.kind, "message": self.message, "next_step": self.next_step,
                "retryable": self.retryable, "exit_code": self.exit_code}
        if self.next_action:
            data["next_action"] = self.next_action
        return data


@dataclass
class Result:
    tool: str
    ok: bool = True
    data: dict = field(default_factory=dict)
    text: str = ""
    warnings: list = field(default_factory=list)
    untrusted_fields: list = field(default_factory=list)
    error: Optional[ErrorInfo] = None
    svn: dict = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.ok else (self.error.exit_code if self.error else EXIT_OTHER)

    @classmethod
    def failure(cls, tool: str, error: ErrorInfo, data: Optional[dict] = None, warnings: Optional[list] = None,
                svn: Optional[dict] = None) -> "Result":
        text = " ".join(part for part in (error.message, error.next_step) if part)
        return cls(tool, False, dict(data or {}), text, list(warnings or []), [], error, dict(svn or {}))

    def to_dict(self, details: bool = False) -> dict:
        out = {"schema": SCHEMA, "ok": self.ok, "tool": self.tool, "data": self.data, "text": self.text,
               "warnings": list(self.warnings), "untrusted_fields": list(self.untrusted_fields),
               "error": self.error.to_dict() if self.error else None}
        if details:
            svn = dict(self.svn)
            if self.error and self.error.svn:
                svn.setdefault("error", self.error.svn)
            out["svn"] = svn
        return out
