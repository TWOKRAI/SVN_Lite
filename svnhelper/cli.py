"""Консольная программа svnlite: операции чтения слоя ops для человека, скриптов и ИИ-помощника.

Команды строятся из реестра операций (`svnhelper.ops.registry`): новая операция чтения появляется в консоли
сама, списков команд здесь нет. Команд записи нет. Подкоманда `mcp` запускает сервер для ИИ-помощника
(`svnhelper.mcp_server`), он подключается лениво: без него остальные команды работают.

Правила: stdout только результат (JSON без потерь кириллицы или текст), пароль не запрашивается и не
читается, окно программы, трей и общий журнал svn_helper.log не затрагиваются, tkinter не нужен.
Коды возврата: 0 успех, 1 прочее, 2 неверные параметры, 3 нет связи, 4 нужен вход, 5 нет прав, 6 отказ политики,
7 нужна работа человека, 8 папку обрабатывает другая программа, 9 не найден svn.exe.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from typing import Optional

from . import __version__, agent_env, agentlog
from .ops import all_operations, get, make_context, run
from .ops.errors import OpsError, from_exception, invalid_args
from .ops.result import EXIT_ARGS, EXIT_OTHER, Result

log = logging.getLogger("svnhelper.cli")

PROG = "svnlite"

DESCRIPTION = ("Помощник для работы с проектами: показывает состояние проектов, правки, новое у коллег, историю "
               "и занятые файлы. Только чтение: ничего не меняет, пароль не запрашивает. По умолчанию, когда вывод "
               "идёт не в консоль, ответ выдаётся в виде JSON.")
EPILOG = """Коды возврата:
  0  успех
  1  прочее (не найдено, уже есть, отмена)
  2  неверные параметры
  3  нет связи с сервером или сервер не отвечает
  4  нужен вход: откройте SVN-lite и войдите
  5  нет прав
  6  отказ политики помощника
  7  нужна работа человека (конфликт, на сервере новее, файл открыт в другой программе)
  8  папку проекта обрабатывает другая программа
  9  не найдена программа svn.exe"""

HELP_COMMANDS_TITLE = "команды"
HELP_COMMANDS_METAVAR = "команда"
HELP_MCP = "Запустить сервер для ИИ-помощника (Claude Desktop, Claude Code) по стандартному вводу и выводу."
HELP_HELP = "Показать справку и выйти."
HELP_VERSION = "Показать версию и выйти."
HELP_JSON = "Ответ в виде JSON (по умолчанию, когда вывод идёт не в консоль)."
HELP_TEXT = "Ответ обычным текстом для человека."
HELP_DETAILS = "Добавить технические подробности для администратора (адрес, коды ошибок)."
HELP_COMMANDS_FILE = ("Записать справочник команд в файл Markdown (UTF-8) с вашими проектами: его можно отдать любому "
                      "ИИ-чату, чтобы он подсказал команды для терминала.")
HELP_OFFLINE = "Не обращаться к серверу: только то, что есть на этом компьютере."
HELP_OFFLINE_DOCTOR = "Не проверять связь с сервером."

ARGS_BAD = "Неверно заданы параметры командной строки."
ARGS_HINT = "Справка: svnlite --help, по команде: svnlite <команда> --help."
WARNING_PREFIX = "Внимание: "
DETAILS_TITLE = "Подробности для администратора:"
INTERRUPTED = "Прервано."
MCP_MISSING = ("Режим для ИИ-помощника недоступен: в этой сборке нет сервера помощника "
               "или не установлен пакет mcp.")

MCP_COMMAND = "mcp"
COMMON_FLAGS = ("--json", "--text", "--details")     # общие флаги, допустимые до названия команды
MCP_MODULE = "svnhelper.mcp_server"

# Имя команды: имя операции с дефисом (server_projects -> server-projects); исключения и запасные названия
COMMAND_NAMES = {"project_info": "info"}
COMMAND_ALIASES = {"project_info": ["project-info"]}
# Флаги, которые называются не по имени параметра: параметр -> (флаг, значение при указании флага, справка)
FLAG_OVERRIDES = {
    "all_projects": ("--all", True, None),
    "check_server": ("--offline", False, HELP_OFFLINE),
    "check_connection": ("--offline", False, HELP_OFFLINE_DOCTOR),
}

_HELP_WORDS = (("usage: ", "использование: "), ("positional arguments:", "аргументы:"),
               ("optional arguments:", "параметры:"), ("options:", "параметры:"))


class _ArgsError(Exception):
    """Командная строка разобрана неверно (текст от argparse по-английски, человеку он не показывается)."""


class _Parser(argparse.ArgumentParser):
    """argparse с русской справкой; ошибка разбора не печатается и не завершает процесс, а становится ответом."""

    def error(self, message: str):
        raise _ArgsError(message)

    def format_help(self) -> str:
        return _russian_help(super().format_help())

    def format_usage(self) -> str:
        return _russian_help(super().format_usage())


def _russian_help(text: str) -> str:
    for english, russian in _HELP_WORDS:
        text = text.replace(english, russian)
    return text


def command_name(operation_name: str) -> str:
    return COMMAND_NAMES.get(operation_name, operation_name.replace("_", "-"))


def _common_options() -> argparse.ArgumentParser:
    """Флаги, которые принимаются и до, и после названия команды. Значение по умолчанию SUPPRESS: флаг, не указанный
    после команды, не затирает указанный до неё."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", dest="format", action="store_const", const="json", default=argparse.SUPPRESS,
                        help=HELP_JSON)
    common.add_argument("--text", dest="format", action="store_const", const="text", default=argparse.SUPPRESS,
                        help=HELP_TEXT)
    common.add_argument("--details", action="store_true", default=argparse.SUPPRESS, help=HELP_DETAILS)
    return common


def _add_write_param(parser: argparse.ArgumentParser, param, first: bool) -> None:
    """Параметр операции записи -> аргумент команды. Проект или папка (первый обязательный) позиционный; комментарий
    берётся только из файла или со стандартного ввода (--message-file), чтобы кавычки и кодировка консоли его не
    искажали; остальное параметрами с именем."""
    flag = "--" + param.name.replace("_", "-")
    if param.name == "message":
        parser.add_argument("--message-file", dest="message_file", metavar="ФАЙЛ", default=None,
                            help=param.description_ru + " Текст берётся из файла в UTF-8; «-» читает стандартный ввод.")
    elif first and param.required and param.type == "string":
        parser.add_argument(param.name, help=param.description_ru)
    elif param.name == "fields":
        parser.add_argument("--field", dest="fields", action="append", metavar="имя=значение", default=None,
                            help=param.description_ru)
    elif param.type == "list":
        parser.add_argument(flag, dest=param.name, nargs="+", metavar="файл", default=None, help=param.description_ru)
    elif param.type == "integer":
        parser.add_argument(flag, dest=param.name, type=int, default=None, help=param.description_ru,
                            required=param.required)
    elif param.type == "boolean":
        parser.add_argument(flag, dest=param.name, action="store_const", const=True, default=None,
                            help=param.description_ru)
    else:
        parser.add_argument(flag, dest=param.name, default=None, help=param.description_ru, required=param.required)


def _add_param(parser: argparse.ArgumentParser, param) -> None:
    """Параметр операции -> аргумент команды. Строки и списки позиционные, числа и логические флагами."""
    if param.name in FLAG_OVERRIDES:
        flag, value, help_text = FLAG_OVERRIDES[param.name]
        parser.add_argument(flag, dest=param.name, action="store_const", const=value, default=None,
                            help=help_text or param.description_ru)
    elif param.type == "string":
        parser.add_argument(param.name, nargs=None if param.required else "?", default=None,
                            help=param.description_ru)
    elif param.type == "list":
        parser.add_argument(param.name, nargs="*", default=None, metavar=param.name, help=param.description_ru)
    elif param.type == "integer":
        parser.add_argument("--" + param.name.replace("_", "-"), dest=param.name, type=int, default=None,
                            help=param.description_ru)
    else:                                                       # boolean без особого флага
        flag = "--" + param.name.replace("_", "-")
        if param.default:
            parser.add_argument("--no-" + flag[2:], dest=param.name, action="store_const", const=False,
                                default=None, help=param.description_ru)
        else:
            parser.add_argument(flag, dest=param.name, action="store_const", const=True, default=None,
                                help=param.description_ru)


def build_parser() -> argparse.ArgumentParser:
    """Разборщик командной строки: по подкоманде на каждую операцию реестра."""
    common = _common_options()
    parser = _Parser(prog=PROG, description=DESCRIPTION, epilog=EPILOG, add_help=False, allow_abbrev=False,
                     parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-h", "--help", action="help", help=HELP_HELP)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}", help=HELP_VERSION)
    sub = parser.add_subparsers(dest="command", required=True, title=HELP_COMMANDS_TITLE,
                                metavar=HELP_COMMANDS_METAVAR)
    for op in all_operations():
        cmd = sub.add_parser(command_name(op.name), aliases=COMMAND_ALIASES.get(op.name, []), help=op.title_ru,
                             description=op.description_ru, parents=[common], add_help=False, allow_abbrev=False)
        cmd.add_argument("-h", "--help", action="help", help=HELP_HELP)
        if op.name == "commands":
            cmd.add_argument("--file", metavar="ФАЙЛ", default=None, help=HELP_COMMANDS_FILE)
        for index, param in enumerate(op.params):
            if op.read_only and not op.name.endswith("_plan"):
                _add_param(cmd, param)
            else:
                _add_write_param(cmd, param, index == 0)
        cmd.set_defaults(operation=op.name)
    sub.add_parser(MCP_COMMAND, help=HELP_MCP, add_help=False)      # только для справки: разбирается до argparse
    return parser


CATALOG_HEADER = "# Справочник команд SVN-lite"
MAX_MESSAGE_BYTES = 8000                    # комментарий к сохранению: несколько абзацев, не файл целиком
MAX_FILE_NOTE = "Файл слишком большой для комментария."


def _protected_places() -> list:
    """Папки, из которых помощник не читает и в которые не пишет через --message-file и --file: данные программы
    (настройки, политика, журналы, планы), кэш входа svn, ключи ssh, настройки Claude."""
    from .config import data_dir
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    places = [str(data_dir()), os.path.join(appdata, "Subversion"), os.path.join(home, ".ssh"),
              os.path.join(home, ".claude"), os.path.join(home, ".subversion")]
    return [os.path.normcase(os.path.realpath(p)) for p in places]


def is_protected(path: str) -> bool:
    real = os.path.normcase(os.path.realpath(path))
    return any(real == p or real.startswith(p + os.sep) for p in _protected_places())


def _save_catalog(result: Result, target: str) -> Result:
    """Справочник команд в файл (UTF-8). Только файл `.md` вне служебных папок программы; существующий файл заменяется,
    только если это прежний справочник (его первая строка). Не записался: ответ с ошибкой, файла нет."""
    try:
        if not target.lower().endswith(".md") or is_protected(target):
            raise invalid_args("Справочник записывается в файл с расширением .md вне служебных папок программы.")
        if os.path.exists(target):
            with open(target, "r", encoding="utf-8", errors="replace") as old:
                if not old.readline().startswith(CATALOG_HEADER):
                    raise invalid_args("Такой файл уже есть и это не справочник команд: выберите другое имя.")
        with open(target, "w", encoding="utf-8", newline=chr(10)) as handle:
            handle.write(result.data.get("markdown", ""))
    except OpsError as exc:
        return Result.failure("commands", from_exception(exc))
    except OSError as exc:
        return Result.failure("commands", from_exception(exc))
    result.data["file"] = target
    result.text = f"Справочник команд записан в файл: {target}. " + result.text
    return result


def read_message(source: str) -> str:
    """Комментарий из файла (UTF-8) или со стандартного ввода («-»). Не прочитался: ответ с кодом 2.

    Помощник не должен отправить на сервер чужой файл под видом комментария: служебные папки программы, кэш входа,
    ключи и настройки Claude не читаются, размер ограничен (MAX_MESSAGE_BYTES), двоичное содержимое отвергается.
    Комментарий и без того показывается человеку в плане."""
    try:
        if source == "-":
            data = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        else:
            if is_protected(source):
                raise invalid_args("Из этой папки комментарий не читается: положите текст комментария в обычный файл.")
            with open(source, "rb") as handle:
                data = handle.read(MAX_MESSAGE_BYTES + 1)
        if len(data) > MAX_MESSAGE_BYTES:
            raise invalid_args(f"{MAX_FILE_NOTE} Комментарий не длиннее {MAX_MESSAGE_BYTES} байт.")
        if b"\x00" in data:
            raise invalid_args("Файл с комментарием не текстовый.")
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise invalid_args(f"Файл с комментарием не прочитан: {exc.__class__.__name__}. Нужен текст в кодировке UTF-8.") from exc


def _params(args: argparse.Namespace, op_name: str) -> dict:
    """Значения, которые человек указал; остальное операция берёт по умолчанию."""
    params = {}
    for param in get(op_name).params:
        value = getattr(args, param.name, None)
        if value is not None and value != []:
            params[param.name] = value
    message_file = getattr(args, "message_file", None)
    if message_file:
        params["message"] = read_message(message_file)
    return params


def _wants_json(argv: list, explicit: Optional[str] = None) -> bool:
    """JSON, если так просили; не просили: JSON, когда вывод не в консоль."""
    chosen = explicit
    if chosen is None:
        flags = [a for a in argv if a in ("--json", "--text")]
        chosen = flags[-1][2:] if flags else None
    if chosen is not None:
        return chosen == "json"
    return not sys.stdout.isatty()


def _configure_streams(json_mode: bool) -> None:
    """JSON всегда в UTF-8; текст в кодировке консоли, непредставимые знаки заменяются, а не роняют программу."""
    for stream, utf8 in ((sys.stdout, json_mode), (sys.stderr, False)):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if utf8:
            reconfigure(encoding="utf-8", errors="replace")
        else:
            reconfigure(errors="replace")


def _svn_block(result: Result) -> dict:
    return result.to_dict(details=True).get("svn") or {}


def render(result: Result, json_mode: bool, details: bool) -> tuple:
    """(текст для stdout, текст для stderr). Результат идёт в stdout, ошибка в текстовом режиме в stderr."""
    if json_mode:
        return json.dumps(result.to_dict(details), ensure_ascii=False) + "\n", ""
    lines = [result.text] if result.text else []
    lines += [WARNING_PREFIX + warning for warning in result.warnings]
    block = _svn_block(result) if details else {}
    if block:
        lines += [DETAILS_TITLE, json.dumps(block, ensure_ascii=False, indent=2)]
    text = "\n".join(lines) + "\n"
    return ("", text) if not result.ok else (text, "")


def _emit(result: Result, json_mode: bool, details: bool = False) -> int:
    out, err = render(result, json_mode, details)
    if out:
        sys.stdout.write(out)
        sys.stdout.flush()
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    return result.exit_code


def _args_failure(detail: str) -> Result:
    error = from_exception(OpsError("invalid_args", EXIT_ARGS, ARGS_BAD, ARGS_HINT, svn={"detail": detail}))
    return Result.failure("cli", error)


def run_mcp(argv: list) -> int:
    """Подкоманда mcp: сервер для ИИ-помощника. Модуль подключается здесь, а не при старте; stdout принадлежит
    протоколу, поэтому сообщения идут в stderr."""
    try:
        module = importlib.import_module(MCP_MODULE)
    except ImportError as exc:
        _configure_streams(False)
        sys.stderr.write(f"{MCP_MISSING}\n({exc})\n")
        return 1
    saved = sys.argv
    sys.argv = [saved[0] if saved else PROG, *argv[1:]]
    try:
        code = module.main()
    finally:
        sys.argv = saved
    return code if isinstance(code, int) else 0


def _mcp_argv(argv: list) -> Optional[list]:
    """Аргументы, начиная с `mcp`, если это команда mcp (общие флаги до неё допустимы и игнорируются: сервер говорит
    по протоколу, у него нет ни JSON, ни текста, ни подробностей); иначе None."""
    start = 0
    while start < len(argv) and argv[start] in COMMON_FLAGS:
        start += 1
    return argv[start:] if start < len(argv) and argv[start] == MCP_COMMAND else None


def main(argv: Optional[list] = None, ctx=None) -> int:
    """Точка входа. argv: аргументы без имени программы (по умолчанию из командной строки); ctx: готовый контекст
    операций (для тестов; без него создаётся свой и подключается журнал консольной программы)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    mcp_argv = _mcp_argv(argv)
    if mcp_argv is not None:
        return run_mcp(mcp_argv)
    json_mode = _wants_json(argv)
    _configure_streams(json_mode)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:                                  # справка и версия
        return stop.code if isinstance(stop.code, int) else 0
    except _ArgsError as exc:
        return _emit(_args_failure(str(exc)), json_mode)
    json_mode = _wants_json(argv, getattr(args, "format", None))
    details = bool(getattr(args, "details", False))
    try:
        if ctx is None:
            agent_env.harden()                                  # папка данных, строгие замки, svn.exe вместе с программой
            agentlog.setup("cli")
            ctx = make_context(client_name="cli")
        params = _params(args, args.operation)
        out_file = getattr(args, "file", None) if args.operation == "commands" else None
        if out_file:
            params["markdown"] = True
        result = run(ctx, args.operation, **params)
        if out_file and result.ok:
            result = _save_catalog(result, out_file)
    except KeyboardInterrupt:
        sys.stderr.write(INTERRUPTED + "\n")
        return EXIT_OTHER
    except Exception as exc:                                    # операции не бросают, но создание контекста может
        result = Result.failure(getattr(args, "operation", "cli"), from_exception(exc))
    return _emit(result, json_mode, details)


if __name__ == "__main__":
    sys.exit(main())
