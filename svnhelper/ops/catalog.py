"""Каталог команд помощника (операция `commands`, инструмент MCP `list_commands`, команда `svnlite commands`).

Один источник для трёх читателей: агент спрашивает его через MCP, чтобы знать свои возможности; разработчик или
сотрудник без агента получает готовый Markdown (`--markdown` или `--file справочник.md`), отдаёт его любому ИИ-чату и
получает от него команды для терминала с настоящими названиями и путями своих проектов. Каталог строится из реестра
операций, поэтому не расходится с программой: новая операция появляется в нём сама. Чтение: ничего не меняет, svn не
нужен (без svn список проектов пропускается).
"""
from __future__ import annotations

import logging
from typing import Optional

from . import texts, texts_write
from .context import OpsContext
from .registry import REGISTRY, Operation, operation
from .result import MAX_ENTRIES, Result, clean_name

log = logging.getLogger("svnhelper.ops")

MAX_PROJECTS = 30                   # проектов в справочнике
PLACEHOLDER = {"project": "<проект>", "folder": "<папка>"}
KIND_READ, KIND_PLAN, KIND_APPLY = "read", "plan", "apply"
# в каком порядке показывать группы
GROUPS = (
    ("Смотреть (ничего не меняют)", lambda op: op.read_only and not op.name.endswith("_plan")),
    ("Составить план записи (ничего не меняют, выдают токен)", lambda op: op.name.endswith("_plan")),
    ("Выполнить запись по токену плана", lambda op: not op.read_only),
)


def op_kind(op: Operation) -> str:
    if not op.read_only:
        return KIND_APPLY
    return KIND_PLAN if op.name.endswith("_plan") else KIND_READ


def mcp_name(op: Operation) -> str:
    from ..mcp_server import tool_name          # только словарь имён; сам SDK mcp не нужен
    return tool_name(op.name)


def command_word(op: Operation) -> str:
    from ..cli import command_name              # отложенно: cli сам импортирует ops
    return command_name(op.name)


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def cli_arguments(op: Operation) -> list:
    """Аргументы команды в том виде, как их принимает консольная программа: [(текст, обязателен)]."""
    from ..cli import FLAG_OVERRIDES
    write = not op.read_only or op.name.endswith("_plan")
    out: list = []
    for index, param in enumerate(op.params):
        if param.name in FLAG_OVERRIDES and not write:
            out.append((FLAG_OVERRIDES[param.name][0], False))
        elif write and param.name == "message":
            out.append(("--message-file <файл>", param.required))
        elif write and index == 0 and param.required and param.type == "string":
            out.append((f"<{PLACEHOLDER_NAME.get(param.name, param.name)}>", True))
        elif write and param.name == "fields":
            out.append(("--field <имя=значение>", False))
        elif write and param.type == "list":
            out.append((f"{_flag(param.name)} <файл> [<файл> ...]", param.required))
        elif write and param.type == "string":
            out.append((f"{_flag(param.name)} <{param.name}>", param.required))
        elif param.type == "string" and not write:
            out.append((f"<{PLACEHOLDER_NAME.get(param.name, param.name)}>", param.required))
        elif param.type == "list" and not write:
            out.append((f"<{PLACEHOLDER_NAME.get(param.name, param.name)}> ...", param.required))
        elif param.type == "integer":
            out.append((f"{_flag(param.name)} <число>", param.required))
        elif param.type == "boolean":
            out.append((f"--no-{param.name.replace('_', '-')}" if param.default else _flag(param.name), False))
    return out


PLACEHOLDER_NAME = {"project": "проект", "folder": "папка", "template": "шаблон", "path": "путь", "projects": "проект"}


def cli_syntax(op: Operation) -> str:
    parts = ["svnlite", command_word(op)]
    parts += [text if required else f"[{text}]" for text, required in cli_arguments(op)]
    return " ".join(parts)


def _quote(value: str) -> str:
    return f'"{value}"' if any(ch in value for ch in " &()^%!") or not value.isascii() else value


def cli_example(op: Operation, project: str) -> str:
    """Готовая строка для терминала: настоящий проект подставлен, остальное в <угловых скобках>."""
    parts = ["svnlite", command_word(op)]
    for text, required in cli_arguments(op):
        if not required and "<проект>" not in text and "<папка>" not in text:
            continue
        parts.append(text.replace("<проект> ...", _quote(project)).replace("<проект>", _quote(project))
                     .replace("<папка>", _quote(project)))
    if op_kind(op) == KIND_APPLY:
        parts.append("--token <токен из плана>")
    return " ".join(parts) + " --text"


def describe_op(op: Operation, project: str) -> dict:
    return {"name": op.name, "tool": mcp_name(op), "cli": command_word(op), "kind": op_kind(op),
            "title": op.title_ru, "description": op.description_ru, "changes_data": not op.read_only,
            "permission": _permission(op), "usage": cli_syntax(op), "example": cli_example(op, project),
            "params": [{"name": p.name, "type": p.type, "required": p.required, "description": p.description_ru,
                        "default": p.default} for p in op.params],
            "questions": list(op.examples_ru)}


def _permission(op: Operation) -> str:
    """Какое разрешение политики нужно записи (пусто для чтения)."""
    if op.read_only and not op.name.endswith("_plan"):
        return ""
    base = op.name.rsplit("_", 1)[0]
    if base == "save":
        return "save_server (или save_local с параметром local)"
    if base == "create_project":
        return "create_local (или create_server для сервера)"
    return texts_write_action(base)


def texts_write_action(base: str) -> str:
    from .writing import texts_write_action as action
    try:
        return action(base, {})
    except KeyError:
        return ""


def catalog(project: str = "") -> list:
    from .registry import all_operations
    return [describe_op(op, project or "<проект>") for op in all_operations()]


def _project_rows(ctx: OpsContext, warnings: list) -> list:
    """Проекты на этом компьютере для справочника. Без svn.exe или при сбое: пусто и предупреждение."""
    if ctx.client is None:
        warnings.append("Программа svn.exe не найдена: список ваших проектов в справочник не вошёл.")
        return []
    try:
        result = REGISTRY["projects"].func(ctx)
    except Exception as exc:                                # noqa: BLE001  (справочник нужен и без списка проектов)
        log.warning("Список проектов для справочника не получен: %s", exc)
        warnings.append("Список ваших проектов получить не удалось: в справочнике только общие примеры.")
        return []
    warnings.extend(w for w in result.warnings if w not in warnings)
    return result.data.get("projects", [])[:MAX_PROJECTS]


def build_markdown(items: list, rows: list, ctx: OpsContext, project: str) -> str:
    """Справочник одним текстом для ИИ-чата: правила, режим, проекты, команды с примерами."""
    policy = ctx.policy.describe() if hasattr(ctx.policy, "describe") else {}
    allow = ", ".join(policy.get("allow", [])) or "ничего"
    lines = [
        "# Справочник команд SVN-lite",
        "",
        "Этот файл можно целиком отдать ИИ-чату и попросить: «Посоветуй, какие команды ввести в терминале, чтобы "
        "сделать то-то». Команды запускаются в обычной консоли (cmd или PowerShell); программа называется `svnlite`.",
        "",
        "## Правила",
        "",
        "- Пароль не вводится и не нужен: вход делается один раз в окне SVN-lite.",
        "- Ответ `--text` читается человеком, `--json` — программой. Для себя добавляйте `--text`.",
        "- Сначала смотрите (`status`, `changes`), потом меняйте. Любое изменение — два шага: `…-plan` показывает, что "
        "будет сделано, и выдаёт токен; `…-apply --token <токен>` выполняет. Показывайте результат плана человеку. "
        "В `…-apply` повторяются те же параметры, что были в плане: иначе токен не подойдёт.",
        "- Комментарий к сохранению пишется в текстовый файл (UTF-8) и передаётся как `--message-file комментарий.txt` "
        "(так не ломаются русские буквы и кавычки). `-` вместо имени файла читает комментарий из стандартного ввода.",
        "- Названия с пробелами и русскими буквами берутся в кавычки: `\"261028_отбраковщик\"`.",
        "- Чего команды не умеют никогда: удалять на сервере, ломать чужие занятые файлы, разбирать конфликты, менять "
        "пароль и настройки. Это делает человек в окне SVN-lite.",
        "",
        "## Что разрешено сейчас",
        "",
        f"- Режим: {policy.get('mode', 'неизвестно')} ({policy.get('file_mode', '')}); "
        f"подтверждение токеном: {'нужно' if policy.get('confirm', True) else 'не нужно'}.",
        f"- Разрешённые действия записи: {allow}.",
        "- Если запись выключена, команды `…-plan` и `…-apply` отвечают кодом 6. Включает запись сотрудник в окне "
        "SVN-lite или администратор.",
        "",
    ]
    if rows:
        lines += ["## Мои проекты", "", "| Название | Папка | Где хранится | Линия | Сохранение № |", "|---|---|---|---|---|"]
        for row in rows:
            lines.append(f"| {row['name']} | `{row['path']}` | {texts.KIND_RU.get(row['kind'], row['kind'])} | "
                         f"{texts.LINE_RU.get(row['line'], row['line'])} | {row.get('number') or ''} |")
        lines.append("")
        lines.append(f"В примерах ниже подставлен проект «{project}»: замените его на нужный.")
        lines.append("")
    else:
        lines += ["## Мои проекты", "", "Список проектов в справочник не вошёл (нет папок для проектов или программы "
                  "svn.exe). В примерах ниже вместо названия стоит `<проект>`.", ""]
    lines += ["## Команды", ""]
    for title, belongs in GROUPS:
        group = [item for item in items if belongs(REGISTRY[item["name"]])]
        if not group:
            continue
        lines += [f"### {title}", ""]
        for item in group:
            lines.append(f"#### `{item['usage']}`")
            lines.append("")
            lines.append(f"{item['title']}. {item['description']}")
            if item["permission"]:
                lines.append(f"Нужное разрешение помощнику: `{item['permission']}`.")
            lines.append(f"В MCP: `{item['tool']}`.")
            if item["params"]:
                lines.append("")
                for param in item["params"]:
                    mark = "обязательно" if param["required"] else "по желанию"
                    lines.append(f"- `{param['name']}` ({mark}): {param['description']}")
            lines += ["", "Пример:", "", "```", item["example"], "```", ""]
    lines += ["## Коды возврата", ""] + [
        "- 0 успех; 1 прочее (не найдено, уже есть); 2 неверные параметры; 3 нет связи с сервером;",
        "- 4 нужен вход (откройте SVN-lite и войдите); 5 нет прав; 6 отказ политики помощника (запись выключена, "
        "не разрешено, нет или устарел токен, лимит);",
        "- 7 нужна работа человека (конфликт, на сервере новее, файл надо занять); 8 папку обрабатывает другая "
        "программа; 9 не найдена программа svn.exe.",
        "",
    ]
    return "\n".join(lines)


@operation("commands", needs_svn=False)
def commands(ctx: OpsContext, markdown: bool = False, project: Optional[str] = None) -> Result:
    warnings: list = []
    rows = _project_rows(ctx, warnings)
    chosen = (project or "").strip() or (rows[0]["name"] if rows else "")
    items = catalog(chosen)
    data = {"commands": items, "projects": [{"name": clean_name(r["name"]), "path": clean_name(r["path"]),
                                              "kind": r["kind"], "line": r["line"], "number": r.get("number")}
                                             for r in rows[:MAX_ENTRIES]]}
    if markdown:
        data["markdown"] = build_markdown(items, rows, ctx, chosen or "<проект>")
    reads = sum(1 for item in items if item["kind"] == KIND_READ)
    writes = sum(1 for item in items if item["kind"] == KIND_APPLY)
    text = (f"Команд помощника: {len(items)} (смотреть: {reads}, менять: {writes} с планом и подтверждением). "
            "Справочник для ИИ-чата: команда commands --markdown или --file справочник.md.")
    return Result("commands", data=data, text=text, warnings=warnings,
                  untrusted_fields=["projects[].name", "projects[].path"])
