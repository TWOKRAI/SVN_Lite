"""Task AG.2: консольная программа svnlite (svnhelper/cli.py) поверх реестра операций.

В процессе: `main(argv, ctx)` с фейковым svn (tests/fake_svn.py), вывод перехватывается. Через subprocess:
`python -m svnhelper.cli` с SVNLITE_DATA_DIR во временной папке; фейковый svn подключается через svn.bat из настроек.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))                # test_ops_read импортируется без пакета (и при запуске tests.test_cli)

from svnhelper import cli, config, winutil                                   # noqa: E402
from svnhelper.config import SettingsStore, protect_text                     # noqa: E402
from svnhelper.ops import all_operations, texts                              # noqa: E402
from svnhelper.ops.result import Result                                      # noqa: E402
from test_ops_read import CANARY, NAME, OpsBase                              # noqa: E402

FAKE_SCRIPT = ROOT / "tests" / "fake_svn.py"
CODES = {"noconn_remote": 3, "auth_remote": 4, "authz_remote": 5, "hang_remote": 3}
SLOW = "hang_remote"        # сервер молчит: в отдельном процессе ждать пришлось бы весь бюджет операции (120 с)


def run_cli(argv: list, ctx=None) -> tuple:
    """(код возврата, stdout, stderr) вызова main в этом процессе."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, ctx)
    return code, out.getvalue(), err.getvalue()


class CliInProcessTests(OpsBase):
    """Команды на фейковом svn: валидный JSON и коды возврата."""

    # команда -> аргументы командной строки (по одной на каждую операцию реестра)
    COMMANDS = {"doctor": ["doctor"], "projects": ["projects"], "status": ["status", NAME],
                "changes": ["changes", NAME], "incoming": ["incoming", NAME],
                "history": ["history", NAME, "--limit", "5"], "locks": ["locks", NAME], "templates": ["templates"],
                "project_info": ["info", NAME], "server_projects": ["server-projects", "konstruktor"]}

    def setUp(self):
        super().setUp()
        self.ctx.timeout = 1.5
        self.ctx.connect_timeout = 1
        # настоящая программа подключает журнал агента (agentlog); здесь его нет, и предупреждения svnhelper
        # попали бы в stderr через резервный обработчик logging
        quiet = logging.NullHandler()
        logging.getLogger("svnhelper").addHandler(quiet)
        self.addCleanup(logging.getLogger("svnhelper").removeHandler, quiet)

    def cli(self, *argv) -> tuple:
        return run_cli(list(argv), self.ctx)

    def test_commands_come_from_the_registry(self):
        self.assertEqual(sorted(self.COMMANDS), sorted(op.name for op in all_operations()))
        for op in all_operations():
            code, out, err = run_cli([cli.command_name(op.name), "--help"])
            self.assertEqual((op.name, code, err), (op.name, 0, ""))
            self.assertIn(op.description_ru, " ".join(out.split()))
        code, out, _err = run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn(cli.MCP_COMMAND, out)
        for op in all_operations():
            self.assertIn(cli.command_name(op.name), out)
            self.assertIn(op.title_ru, " ".join(out.split()))

    def test_every_command_gives_valid_json_and_zero(self):
        for name, argv in self.COMMANDS.items():
            code, out, err = self.cli(*argv)
            self.assertEqual(err, "", name)
            data = json.loads(out)
            self.assertEqual((name, data["tool"]), (name, name))
            self.assertEqual(out.count("\n"), 1)                        # один JSON в одной строке
            result = Result(name, data["ok"], data["data"], data["text"], data["warnings"], data["untrusted_fields"])
            self.assertEqual(list(data)[:6], ["schema", "ok", "tool", "data", "text", "warnings"])
            self.assertEqual((name, code), (name, 0 if data["ok"] else data["error"]["exit_code"]))
            self.assertTrue(data["ok"], (name, data["text"]))
            self.assertTrue(result.text)

    def test_status_all_and_offline_do_not_touch_the_server(self):
        code, out, _err = self.cli("status", "--all", "--offline")
        data = json.loads(out)
        self.assertEqual((code, data["ok"], data["data"]["projects"][0]["server_checked"]), (0, True, False))
        self.assertNotIn("status -u", [" ".join(c["args"][:2]) for c in self.calls()])
        for call in self.calls():
            self.assertFalse({"-u", "--show-updates"} & set(call["args"]), call["args"])
            self.assertNotIn(call["args"][0], ("list", "log", "cat"))
        code, out, _err = self.cli("status", NAME)
        self.assertTrue(json.loads(out)["data"]["projects"][0]["server_checked"])

    def test_network_failures_give_exit_codes_3_4_5(self):
        for mode, code in CODES.items():
            self.mode(mode)
            for argv in (["incoming", NAME], ["history", NAME], ["server-projects", "konstruktor"], ["doctor"]):
                got, out, err = self.cli(*argv)
                data = json.loads(out)
                self.assertEqual((mode, argv[0], got, err), (mode, argv[0], code, ""))
                self.assertFalse(data["ok"])
                self.assertEqual(data["error"]["exit_code"], code)
                self.assertTrue(data["error"]["message"])
                self.assertNotIn("svn", data)                          # подробности только по --details

    def test_status_without_link_is_ok(self):
        self.mode("noconn_remote")
        code, out, _err = self.cli("status", NAME)
        data = json.loads(out)
        self.assertEqual((code, data["ok"]), (0, True))
        self.assertIn(texts.WARN_OFFLINE, data["warnings"])

    def test_unknown_project_gives_1(self):
        code, out, err = self.cli("changes", "нет_такого_проекта")
        data = json.loads(out)
        self.assertEqual((code, err, data["ok"], data["error"]["kind"]), (1, "", False, "project_not_found"))
        self.assertEqual(data["error"]["exit_code"], 1)

    def test_bad_arguments_give_2_and_json_answer(self):
        cases = ([], ["нет_такой_команды"], ["history"], ["history", NAME, "--limit", "много"], ["status"],
                 ["status", "--all", NAME], ["status", "--нет-такого-флага"], ["changes", NAME, "лишний"])
        for argv in cases:
            code, out, err = self.cli(*argv)
            self.assertEqual((argv, code, err), (argv, 2, ""))
            data = json.loads(out)
            self.assertFalse(data["ok"])
            self.assertEqual(data["error"]["kind"], "invalid_args")
            self.assertEqual(data["error"]["exit_code"], 2)
            self.assertRegex(data["text"], "[А-Яа-я]")
            self.assertNotIn("usage", out)                            # английский текст argparse человеку не показывается
        self.assertEqual(self.commands(), [])                         # неверная команда svn не вызывает

    def test_bad_arguments_in_text_mode_go_to_stderr(self):
        code, out, err = self.cli("--text", "history")
        self.assertEqual((code, out), (2, ""))
        self.assertIn(cli.ARGS_BAD, err)

    def test_flags_work_before_and_after_the_command(self):
        for argv in (["--text", "status", NAME], ["status", NAME, "--text"], ["--json", "--text", "status", NAME]):
            code, out, _err = self.cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertIn(f"«{NAME}»", out)
            self.assertFalse(out.lstrip().startswith("{"), argv)
        for argv in (["--text", "--json", "status", NAME], ["status", NAME, "--json"]):
            self.assertEqual(json.loads(self.cli(*argv)[1])["tool"], "status", argv)

    def test_text_mode_prints_text_and_warnings(self):
        self.mode("noconn_remote")
        code, out, err = self.cli("status", NAME, "--text")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"«{NAME}»", out)
        self.assertIn(cli.WARNING_PREFIX + texts.WARN_OFFLINE, out)
        code, out, err = self.cli("--text", "incoming", NAME)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("Нет связи с сервером", err)

    def test_details_add_the_svn_block_only_when_asked(self):
        self.mode("noconn_remote")
        _code, out, _err = self.cli("incoming", NAME)
        self.assertNotIn("svn", json.loads(out))
        self.assertNotIn("E170013", out)
        _code, out, _err = self.cli("incoming", NAME, "--details")
        data = json.loads(out)
        self.assertIn("svn", data)
        self.assertIn("E170013", json.dumps(data))
        _code, _out, err = self.cli("incoming", NAME, "--text", "--details")
        self.assertIn(cli.DETAILS_TITLE, err)
        self.assertIn("E170013", err)

    def test_parameters_reach_the_operation(self):
        captured = []

        def fake_run(ctx, name, **params):
            captured.append((name, params))
            return Result(name)

        with mock.patch.object(cli, "run", fake_run):
            for argv in (["status", "/c/Users/Иванов/Проекты/x", "b"], ["status", "--all", "--offline"],
                         ["history", "p", "--limit", "7"], ["server-projects", "konstruktor/projects", "--depth", "2"],
                         ["server-projects"], ["doctor", "--offline"], ["doctor"], ["info", "p"],
                         ["project-info", "p"], ["incoming", "a b"]):
                self.assertEqual(run_cli(argv, self.ctx)[0], 0, argv)
        self.assertEqual(captured, [
            ("status", {"projects": ["/c/Users/Иванов/Проекты/x", "b"]}),
            ("status", {"all_projects": True, "check_server": False}),
            ("history", {"project": "p", "limit": 7}),
            ("server_projects", {"path": "konstruktor/projects", "depth": 2}),
            ("server_projects", {}),
            ("doctor", {"check_connection": False}),
            ("doctor", {}),
            ("project_info", {"project": "p"}),
            ("project_info", {"project": "p"}),
            ("incoming", {"project": "a b"}),
        ])

    def test_no_svn_gives_exit_9(self):
        self.ctx.client = None
        code, out, _err = self.cli("changes", NAME)
        self.assertEqual((code, json.loads(out)["error"]["kind"]), (9, "no_svn"))

    def test_no_window_mutex_no_window_log_no_settings(self):
        with mock.patch.object(winutil, "acquire_single_instance") as mutex, \
                mock.patch.object(config, "setup_logging") as window_log:
            self.cli("status", NAME)
            self.cli("projects")
        mutex.assert_not_called()
        window_log.assert_not_called()
        self.assertIsNone(winutil._mutex_handle)
        self.assertFalse((self.root / "settings.json").exists())
        self.assertFalse((self.data_dir / "svn_helper.log").exists())

    def test_unexpected_context_failure_is_an_answer_not_a_traceback(self):
        with mock.patch.object(cli, "make_context", side_effect=RuntimeError("сломалось")), \
                mock.patch.object(cli.agentlog, "setup"):
            code, out, err = run_cli(["projects"])
        data = json.loads(out)
        self.assertEqual((code, err, data["ok"], data["error"]["kind"]), (1, "", False, "internal"))


class McpSubcommandTests(unittest.TestCase):
    def test_mcp_calls_the_server_main_lazily(self):
        calls = []
        fake = types.SimpleNamespace(main=lambda: calls.append(list(sys.argv)) or 0)
        saved = list(sys.argv)
        with mock.patch.dict(sys.modules, {"svnhelper.mcp_server": fake}):
            code, out, err = run_cli(["mcp", "--лишний"])
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(calls, [[saved[0], "--лишний"]])
        self.assertEqual(sys.argv, saved)

    def test_common_flags_before_mcp_are_accepted(self):
        """`svnlite --json mcp`, `--details mcp`, `--text mcp`: раньше падало с AttributeError (у mcp нет operation)."""
        for flags in (["--json"], ["--details"], ["--text"], ["--json", "--details"]):
            calls = []
            fake = types.SimpleNamespace(main=lambda: calls.append(list(sys.argv)) or 0)
            saved = list(sys.argv)
            with self.subTest(flags=flags), mock.patch.dict(sys.modules, {"svnhelper.mcp_server": fake}):
                code, out, err = run_cli([*flags, "mcp"])
            self.assertEqual((code, out, err), (0, "", ""))               # в stdout ничего лишнего
            self.assertEqual(calls, [[saved[0]]])
            self.assertEqual(sys.argv, saved)

    def test_flags_do_not_turn_other_words_into_mcp(self):
        self.assertIsNone(cli._mcp_argv(["--json", "projects", "mcp"]))
        self.assertIsNone(cli._mcp_argv(["--json"]))
        self.assertEqual(cli._mcp_argv(["--text", "mcp", "--x"]), ["mcp", "--x"])

    def test_mcp_subparser_without_operation_never_crashes(self):
        """Запасная защита: даже если разбор дошёл до подкоманды mcp, ответ - ошибка в JSON, а не AttributeError."""
        with mock.patch.object(cli, "_mcp_argv", return_value=None), mock.patch.object(cli.agentlog, "setup"),                 mock.patch.object(cli, "make_context", side_effect=RuntimeError("нет")):
            code, out, err = run_cli(["--json", "mcp"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["ok"], False)

    def test_mcp_returns_the_server_exit_code(self):
        fake = types.SimpleNamespace(main=lambda: 3)
        with mock.patch.dict(sys.modules, {"svnhelper.mcp_server": fake}):
            self.assertEqual(run_cli(["mcp"])[0], 3)
        fake = types.SimpleNamespace(main=lambda: None)
        with mock.patch.dict(sys.modules, {"svnhelper.mcp_server": fake}):
            self.assertEqual(run_cli(["mcp"])[0], 0)

    def test_missing_server_gives_1_and_russian_message(self):
        with mock.patch.object(cli.importlib, "import_module", side_effect=ModuleNotFoundError("No module named 'mcp'")):
            code, out, err = run_cli(["mcp"])
        self.assertEqual((code, out), (1, ""))                      # stdout принадлежит протоколу: пусто
        self.assertIn(cli.MCP_MISSING, err)

    def test_other_commands_do_not_import_the_server(self):
        with mock.patch.object(cli.importlib, "import_module", side_effect=AssertionError("импорт не нужен")):
            self.assertEqual(run_cli(["--version"])[0], 0)
            self.assertEqual(run_cli(["--help"])[0], 0)


class CliImportTests(unittest.TestCase):
    def test_import_does_not_load_tkinter_or_gui(self):
        code = ("import sys, svnhelper.cli; "
                "bad = [m for m in sys.modules if m == 'tkinter' or m.startswith(('tkinter.', 'svnhelper.gui'))]; "
                "print(bad); sys.exit(1 if bad else 0)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "[]"), result.stderr)

    def test_entry_point_and_spec_exist(self):
        self.assertIn("from svnhelper.cli import main", (ROOT / "svnlite_cli.py").read_text(encoding="utf-8"))
        spec = (ROOT / "packaging" / "svnlite.spec").read_text(encoding="utf-8")
        for needle in ("console=True", "upx=False", "excludes=['tkinter']", "name='svnlite'", "svnlite_cli.py",
                       "except ImportError", "collect_submodules('mcp')"):
            self.assertIn(needle, spec)
        build = (ROOT / "packaging" / "build.bat").read_text(encoding="utf-8")
        self.assertIn("svnlite.spec", build)
        self.assertEqual(build.count("goto :fail"), build.count("if errorlevel 1 goto :fail"))
        self.assertIn("svnlite.exe", build.split("echo Done:")[1])


@unittest.skipUnless(sys.platform == "win32", "svn.bat и кодовые страницы консоли есть только в Windows")
class CliSubprocessTests(unittest.TestCase):
    """Настоящий процесс: `python -m svnhelper.cli`, кодировки консоли, файлы на диске."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.log = self.root / "calls.jsonl"
        self.base = self.root / "Проекты"
        (self.base / NAME / ".svn").mkdir(parents=True)
        self.bat = self.root / "svn.bat"
        self.bat.write_text(f'@echo off\r\n"{sys.executable}" "{FAKE_SCRIPT}" %*\r\n', encoding="ascii")
        self.settings_path = self.data / "settings.json"

    def write_settings(self, password: bool = False) -> None:
        store = SettingsStore(self.settings_path)
        store.settings.project_dirs = [str(self.base)]
        store.settings.svn_path = str(self.bat)
        if password:
            store.settings.username = "ivanov"
            store.settings.password_enc = protect_text(CANARY)
        store.save()

    def run_cli(self, *argv, encoding="utf-8", mode="") -> subprocess.CompletedProcess:
        env = dict(os.environ, SVNLITE_DATA_DIR=str(self.data), FAKE_SVN_LOG=str(self.log), PYTHONIOENCODING=encoding)
        env.pop("FAKE_SVN_MODE", None)
        if mode:
            env["FAKE_SVN_MODE"] = mode
        return subprocess.run([sys.executable, "-m", "svnhelper.cli", *argv], cwd=ROOT, env=env, capture_output=True,
                              timeout=120, stdin=subprocess.DEVNULL)

    def test_json_is_utf8_whatever_the_console_encoding(self):
        self.write_settings()
        for encoding in ("cp866", "cp1251", "utf-8"):
            for argv in (["projects"], ["status", NAME], ["info", NAME]):
                done = self.run_cli(*argv, encoding=encoding)
                self.assertEqual((encoding, argv, done.returncode), (encoding, argv, 0), done.stderr)
                data = json.loads(done.stdout.decode("utf-8"))              # парсится без потерь
                assert_valid_json(self, data)
                self.assertIn(NAME, json.dumps(data, ensure_ascii=False))
                self.assertRegex(data["text"], "[А-Яа-я]")
                self.assertNotIn(b"\\u04", done.stdout)                     # кириллица не экранирована

    def test_text_mode_uses_console_encoding(self):
        self.write_settings()
        for encoding in ("cp866", "cp1251"):
            done = self.run_cli("status", NAME, "--text", encoding=encoding)
            self.assertEqual((encoding, done.returncode), (encoding, 0), done.stderr)
            text = done.stdout.decode(encoding)
            self.assertIn(NAME, text)                                       # кириллица цела
            self.assertNotIn("�", text)
        self.assertIn(f"«{NAME}»", self.run_cli("status", NAME, "--text", encoding="cp1251").stdout.decode("cp1251"))

    def test_text_mode_survives_unencodable_characters(self):
        self.write_settings()
        done = self.run_cli("status", NAME, "--text", encoding="ascii")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(b"Traceback", done.stderr)
        self.assertIn(b"?", done.stdout)

    def test_error_codes_and_stdout_only_result(self):
        self.write_settings()
        for mode, code in CODES.items():
            if mode == SLOW:
                continue
            done = self.run_cli("incoming", NAME, mode=mode)
            self.assertEqual((mode, done.returncode), (mode, code), done.stderr)
            data = json.loads(done.stdout.decode("utf-8"))
            self.assertFalse(data["ok"])
            self.assertEqual(done.stderr, b"")
        done = self.run_cli("history")
        self.assertEqual(done.returncode, 2)
        self.assertFalse(json.loads(done.stdout.decode("utf-8"))["ok"])

    def test_help_and_version(self):
        done = self.run_cli("--version", encoding="cp866")
        self.assertEqual((done.returncode, done.stdout.decode("utf-8").strip()), (0, f"svnlite {cli.__version__}"))
        done = self.run_cli("--help", encoding="cp866")
        self.assertEqual(done.returncode, 0)
        self.assertIn("использование:", done.stdout.decode("utf-8"))

    def test_does_not_create_settings_or_window_log(self):
        done = self.run_cli("projects")                                       # настроек нет, svn найдёт сам или нет
        self.assertIn(done.returncode, (0, 1, 9), done.stderr)
        json.loads(done.stdout.decode("utf-8"))
        self.assertFalse(self.settings_path.exists())
        self.assertFalse((self.data / "svn_helper.log").exists())
        self.assertTrue(list((self.data / "logs").glob("svnlite_agent_*.log")))     # свой журнал есть

    def test_existing_settings_are_not_rewritten(self):
        self.write_settings()
        before = (self.settings_path.read_bytes(), self.settings_path.stat().st_mtime_ns)
        for argv in (["projects"], ["status", NAME], ["locks", NAME]):
            self.assertEqual(self.run_cli(*argv).returncode, 0)
        self.assertEqual((self.settings_path.read_bytes(), self.settings_path.stat().st_mtime_ns), before)
        self.assertFalse((self.data / "svn_helper.log").exists())

    def test_canary_password_is_nowhere(self):
        self.write_settings(password=True)
        outputs = []
        for argv in (["doctor", "--details"], ["projects", "--details"], ["status", NAME, "--details"],
                     ["incoming", NAME, "--text", "--details"], ["locks", NAME]):
            for mode in ("", "auth_remote"):
                done = self.run_cli(*argv, mode=mode)
                outputs += [done.stdout, done.stderr]
        for path in self.data.rglob("*"):
            if path.is_file() and path.name != "settings.json":
                outputs.append(path.read_bytes())
        self.assertTrue(self.log.exists())
        for call in (json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()):
            self.assertIsNone(call["password"])
            self.assertNotIn("--password", call["args"])
        for chunk in outputs:
            self.assertNotIn(CANARY.encode("utf-8"), chunk)
            self.assertNotIn(CANARY.encode("cp866", errors="ignore"), chunk)


def assert_valid_json(test: unittest.TestCase, data: dict) -> None:
    test.assertEqual(list(data)[:8], ["schema", "ok", "tool", "data", "text", "warnings", "untrusted_fields", "error"])
    test.assertEqual(data["schema"], 1)


if __name__ == "__main__":
    unittest.main()
