"""Реестр операций: единственный источник для консольной программы, описаний инструментов MCP и текстового помощника.

Каждая операция описана в `texts.OPERATIONS` (название и описание словами сотрудника, живые фразы, параметры) и
регистрируется декоратором `@operation("имя")`. Вызов `op(ctx, **params)` всегда возвращает Result и не бросает
исключений: ошибки превращаются в ErrorInfo (`errors.from_exception`). Операции чтения: read_only=True.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import texts
from .errors import OpsError, from_exception, invalid_args, no_svn
from .result import Result

log = logging.getLogger("svnhelper.ops")

PARAM_TYPES = ("string", "integer", "boolean", "list")


@dataclass(frozen=True)
class Param:
    name: str
    type: str                       # string | integer | boolean | list (список строк)
    required: bool
    description_ru: str
    default: Any = None


@dataclass
class Operation:
    name: str                       # стабильное английское имя
    title_ru: str
    description_ru: str
    examples_ru: list
    params: list = field(default_factory=list)
    read_only: bool = True
    needs_svn: bool = True          # без svn.exe операция отвечает кодом 9
    func: Optional[Callable] = None

    def __call__(self, ctx, **params) -> Result:
        try:
            values = self._check_params(params)
            if self.needs_svn and ctx.client is None:
                raise no_svn()
            result = self.func(ctx.begin(), **values)
        except Exception as exc:                     # операции не бросают исключений: ответ всегда Result
            error = from_exception(exc)
            data = exc.data if isinstance(exc, OpsError) else {}
            fields = exc.untrusted_fields if isinstance(exc, OpsError) else []
            return Result.failure(self.name, error, data, untrusted_fields=fields)
        result.tool = self.name
        return result

    def _check_params(self, params: dict) -> dict:
        known = {p.name: p for p in self.params}
        unknown = sorted(set(params) - set(known))
        if unknown:
            raise invalid_args(texts.ARG_UNKNOWN.format(names=", ".join(unknown)))
        values = {}
        for param in self.params:
            if param.name not in params or params[param.name] is None:
                if param.required:
                    raise invalid_args(texts.ARG_MISSING.format(name=param.name, hint=param.description_ru))
                values[param.name] = param.default
                continue
            value = params[param.name]
            if not _type_ok(param.type, value):
                raise invalid_args(texts.ARG_WRONG.format(name=param.name, hint=param.description_ru))
            values[param.name] = list(value) if param.type == "list" else value
        return values

    def json_schema(self) -> dict:
        """Схема параметров (JSON Schema) для описаний инструментов MCP."""
        kinds = {"string": {"type": "string"}, "integer": {"type": "integer"}, "boolean": {"type": "boolean"},
                 "list": {"type": "array", "items": {"type": "string"}}}
        props = {p.name: {**kinds[p.type], "description": p.description_ru} for p in self.params}
        return {"type": "object", "properties": props, "required": [p.name for p in self.params if p.required]}

    def describe(self) -> dict:
        """Словарь с описанием операции (для справки и тестов)."""
        return {"name": self.name, "title_ru": self.title_ru, "description_ru": self.description_ru,
                "examples_ru": list(self.examples_ru), "read_only": self.read_only,
                "params": [{"name": p.name, "type": p.type, "required": p.required,
                            "description_ru": p.description_ru, "default": p.default} for p in self.params]}


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)


REGISTRY: dict[str, Operation] = {}


def operation(name: str, needs_svn: bool = True, read_only: bool = True) -> Callable[[Callable], Operation]:
    """Регистрирует операцию. Описание берётся из texts.OPERATIONS[name]; неполное описание - ошибка при импорте."""
    def decorator(func: Callable) -> Operation:
        meta = texts.OPERATIONS[name]
        params = [Param(*item) for item in meta["params"]]
        op = Operation(name, meta["title_ru"], meta["description_ru"], list(meta["examples_ru"]), params,
                       read_only, needs_svn, func)
        problems = check_operation(op)
        if problems:
            raise ValueError(f"operation {name} is described incompletely: {'; '.join(problems)}")
        REGISTRY[name] = op
        return op
    return decorator


def check_operation(op: Operation) -> list:
    """Что не заполнено в описании операции (пустой список: всё в порядке)."""
    problems = []
    if not op.name or not op.name.isascii() or not op.name.replace("_", "").isalnum():
        problems.append("name must be ascii")
    if not op.title_ru.strip():
        problems.append("no title_ru")
    if not op.description_ru.strip():
        problems.append("no description_ru")
    if not 3 <= len(op.examples_ru) <= 6 or not all(isinstance(e, str) and e.strip() for e in op.examples_ru):
        problems.append("examples_ru must hold 3 to 6 phrases")
    for param in op.params:
        if param.type not in PARAM_TYPES:
            problems.append(f"param {param.name}: unknown type")
        if not param.description_ru.strip():
            problems.append(f"param {param.name}: no description_ru")
    if not op.read_only:
        problems.append("write operations are not part of this delivery")
    return problems


def get(name: str) -> Operation:
    return REGISTRY[name]


def all_operations() -> list:
    return list(REGISTRY.values())


def run(ctx, name: str, **params) -> Result:
    """Вызов операции по имени. Неизвестное имя - ответ с ошибкой, а не исключение."""
    op = REGISTRY.get(name)
    if op is None:
        return Result.failure(name, from_exception(invalid_args(texts.ARG_NO_OPERATION.format(name=name))))
    return op(ctx, **params)
