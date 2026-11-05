"""Task AG.5 (блокеры «До AG.5», режимы политики, каталог команд, консольная запись): мелкие проверки без сети."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from svnhelper import agent_env, agent_policy, cli, folderlock, jobobject                         # noqa: E402
from svnhelper.agent_policy import (MODE_CONFIRM, MODE_FULL, SIG_OK, Policy, clean_policy)         # noqa: E402
from svnhelper.config import Config, SettingsStore                                                # noqa: E402
from svnhelper.ops import audit, catalog, make_context, run, all_operations                       # noqa: E402
from svnhelper.ops.registry import REGISTRY                                                       # noqa: E402
from svnhelper.svn_core import SvnClient                                                          # noqa: E402

FAKE = [sys.executable, str(ROOT / "tests" / "fake_svn.py")]


class AgentEnvTests(unittest.TestCase):
    def test_frozen_process_ignores_data_dir_override(self):
        with mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": "C:\\чужая"}):
            self.assertFalse(agent_env.ignore_data_dir_override(frozen=False))
            self.assertIn("SVNLITE_DATA_DIR", os.environ)
            self.assertTrue(agent_env.ignore_data_dir_override(frozen=True))
            self.assertNotIn("SVNLITE_DATA_DIR", os.environ)

    def test_harden_turns_strict_locks_on(self):
        was = folderlock.is_strict()
        self.addCleanup(folderlock.set_strict, was)
        with mock.patch.dict(os.environ, {}, clear=False):
            result = agent_env.harden(frozen=False, bind_children=False)
        self.assertTrue(result["strict_locks"])
        self.assertTrue(folderlock.is_strict())

    def test_strict_locks_refuse_mutation_without_lock_directory(self):
        from svnhelper import publish
        was = folderlock.is_strict()
        self.addCleanup(folderlock.set_strict, was)
        folderlock.set_strict(True)
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "файл"
            blocker.write_text("x", encoding="utf-8")
            with mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": str(blocker / "данные")}):
                with self.assertRaises(publish.PublishError) as caught:
                    with publish.mark_busy(tmp, "тест"):
                        pass
        self.assertEqual(caught.exception.code, publish.LOCK_UNAVAILABLE)

    def test_cli_main_hardens_real_runs_only(self):
        with mock.patch.object(agent_env, "harden") as harden, mock.patch.object(cli, "make_context") as ctx, \
                mock.patch.object(cli.agentlog, "setup"), mock.patch.object(cli, "run") as run_op:
            from svnhelper.ops.result import Result
            run_op.return_value = Result("doctor")
            cli.main(["doctor", "--offline", "--json"])
            self.assertTrue(harden.called)
            harden.reset_mock()
            cli.main(["doctor", "--offline", "--json"], ctx=object())
            self.assertFalse(harden.called)

    def test_job_object_is_idempotent(self):
        first = jobobject.bind_children_to_process()
        self.assertEqual(jobobject.bind_children_to_process(), first)


class PolicyModesTests(unittest.TestCase):
    def test_modes_and_old_write_value(self):
        clean, problems = clean_policy({"mode": "write"})
        self.assertEqual((clean["mode"], problems), ("write", []))             # старое значение остаётся и значит «с подтверждением»
        self.assertTrue(Policy(file_mode=clean["mode"], ceiling="write").confirm)
        self.assertEqual(clean_policy({"mode": MODE_FULL})[0]["mode"], MODE_FULL)
        self.assertEqual(clean_policy({"mode": "что-то"})[0]["mode"], "read_only")

    def test_confirm_and_effective_mode(self):
        self.assertTrue(Policy(file_mode=MODE_CONFIRM, ceiling="write").confirm)
        self.assertFalse(Policy(file_mode=MODE_FULL, ceiling="write").confirm)
        self.assertEqual(Policy(file_mode=MODE_FULL, ceiling="write").mode, "write")
        self.assertEqual(Policy(file_mode=MODE_FULL, ceiling="read_only").mode, "read_only")
        self.assertEqual(Policy(file_mode=MODE_FULL, ceiling="off").mode, "off")
        self.assertEqual(Policy(file_mode="read_only", ceiling="write").mode, "read_only")
        self.assertEqual(Policy(file_mode="write", ceiling="write").mode, "write")

    def test_new_actions_are_known(self):
        self.assertIn("branches", agent_policy.ACTIONS)
        self.assertIn("rollback", agent_policy.ACTIONS)

    def test_signed_policy_roundtrip_with_new_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent_policy.json"
            with mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": tmp}):
                agent_policy.write({"mode": MODE_FULL, "allow": ["branches", "rollback", "save_server"]}, path)
                with mock.patch.object(agent_policy, "policy_path", return_value=path):
                    loaded = Policy.load(ceiling="write")
        self.assertEqual((loaded.signature, loaded.file_mode), (SIG_OK, MODE_FULL))
        self.assertEqual(sorted(loaded.allow), ["branches", "rollback", "save_server"])
        self.assertFalse(loaded.confirm)


class AuditRetentionTests(unittest.TestCase):
    def test_old_audit_files_are_removed_on_first_write_of_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("audit_2025-01.jsonl", "audit_2025-12.jsonl", "audit_2026-11.jsonl", "другое.txt"):
                (folder / name).write_text("{}\n", encoding="utf-8")
            removed = audit.prune_old(datetime(2026, 11, 5), folder)
            left = sorted(p.name for p in folder.iterdir())
        self.assertEqual(removed, 1)                       # старше 12 месяцев (до ноября 2025) только январь 2025
        self.assertEqual(left, ["audit_2025-12.jsonl", "audit_2026-11.jsonl", "другое.txt"])

    def test_record_prunes_when_month_starts(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": tmp}):
            old = Path(tmp) / "agent" / "audit_2020-01.jsonl"
            old.parent.mkdir(parents=True)
            old.write_text("{}\n", encoding="utf-8")
            ctx = make_context(client=SvnClient(FAKE), store=SettingsStore(Path(tmp) / "s.json"), config=Config())
            self.assertTrue(audit.record(ctx, "projects"))
            self.assertFalse(old.exists())


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        store = SettingsStore(Path(self.tmp.name) / "s.json")
        self.base = Path(self.tmp.name) / "Проекты"
        (self.base / "261028_отбраковщик").mkdir(parents=True)
        store.settings.project_dirs = [str(self.base)]
        self.ctx = make_context(client=None, store=store, config=Config())
        self.ctx.client = None                              # как на компьютере без svn.exe

    def test_catalog_lists_every_operation_once(self):
        result = run(self.ctx, "commands", markdown=True)
        self.assertTrue(result.ok, result.text)
        names = [item["name"] for item in result.data["commands"]]
        self.assertEqual(sorted(names), sorted(REGISTRY))
        self.assertEqual(len(names), len(set(names)))
        kinds = {item["kind"] for item in result.data["commands"]}
        self.assertEqual(kinds, {"read", "plan", "apply"})
        self.assertTrue(result.warnings)                    # без svn.exe проекты в справочник не вошли

    def test_every_example_parses_with_the_real_command_line_parser(self):
        parser = cli.build_parser()
        for item in catalog.catalog("мой проект"):
            words = self._words(item["example"])
            self.assertEqual(words[0], "svnlite")
            filled = [self._fill(w) for w in words[1:]]
            try:
                parser.parse_args(filled)
            except cli._ArgsError as exc:
                self.fail(f"{item['example']}: {exc}")

    @staticmethod
    def _words(line: str) -> list:
        import shlex
        return shlex.split(line.replace("<токен из плана>", "0" * 32), posix=True)

    @staticmethod
    def _fill(word: str) -> str:
        return "5" if word == "<число>" else word.replace("<токен из плана>", "0" * 32)

    def test_markdown_has_rules_projects_and_real_names(self):
        with mock.patch.object(catalog, "_project_rows", return_value=[
                {"name": "261028_отбраковщик", "path": "C:\\Проекты\\261028_отбраковщик", "kind": "project",
                 "line": "main", "number": 7}]):
            result = run(self.ctx, "commands", markdown=True)
        text = result.data["markdown"]
        self.assertIn("# Справочник команд SVN-lite", text)
        self.assertIn("C:\\Проекты\\261028_отбраковщик", text)
        self.assertIn('svnlite status "261028_отбраковщик" --text', text)
        self.assertIn("--message-file", text)
        self.assertIn("-plan", text)
        self.assertNotIn("пароль:", text.lower())

    def test_cli_writes_catalog_file_in_utf8(self):
        outer = tempfile.TemporaryDirectory()
        self.addCleanup(outer.cleanup)
        target = Path(outer.name) / "справочник.md"       # вне папки данных программы
        code = cli.main(["commands", "--file", str(target), "--json"], ctx=self.ctx)
        self.assertEqual(code, 0)
        self.assertTrue(target.read_text(encoding="utf-8").startswith("# Справочник команд SVN-lite"))
        broken = Path(outer.name) / "нет такой папки" / "x.md"
        self.assertEqual(cli.main(["commands", "--file", str(broken), "--json"], ctx=self.ctx), 1)

    def test_catalog_does_not_change_anything_and_is_not_a_write(self):
        self.assertTrue(REGISTRY["commands"].read_only)
        self.assertFalse(REGISTRY["commands"].needs_svn)


class CliWriteTests(unittest.TestCase):
    def test_message_comes_from_file_or_stdin_only(self):
        parser = cli.build_parser()
        args = parser.parse_args(["save-plan", "проект", "--message-file", "m.txt", "--files", "a.txt", "b/c.txt"])
        self.assertEqual((args.project, args.message_file, args.files), ("проект", "m.txt", ["a.txt", "b/c.txt"]))
        with self.assertRaises(cli._ArgsError):
            parser.parse_args(["save-plan", "проект", "--message", "текст"])

    def test_read_message_utf8_and_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.txt"
            path.write_bytes("Правка «чертежа»".encode("utf-8-sig"))
            self.assertEqual(cli.read_message(str(path)), "Правка «чертежа»")
            from svnhelper.ops.errors import OpsError
            with self.assertRaises(OpsError):
                cli.read_message(str(Path(tmp) / "нет.txt"))
            path.write_bytes("Правка".encode("cp1251"))
            with self.assertRaises(OpsError):
                cli.read_message(str(path))

    def test_apply_commands_take_token_and_destructive_ones_exist(self):
        parser = cli.build_parser()
        args = parser.parse_args(["rollback-apply", "проект", "--revision", "12", "--token", "a" * 32])
        self.assertEqual((args.revision, args.token), (12, "a" * 32))
        for name in ("save", "get_new", "lock", "unlock", "branch", "switch", "rollback", "create_project", "publish"):
            self.assertIn(name + "_plan", REGISTRY)
            self.assertIn(name + "_apply", REGISTRY)
            self.assertTrue(REGISTRY[name + "_plan"].read_only)
            self.assertFalse(REGISTRY[name + "_apply"].read_only)

    def test_there_is_no_command_for_dangerous_operations(self):
        forbidden = ("resolve", "cleanup", "revert", "delete", "remove", "force", "password", "policy", "settings")
        for op in all_operations():
            for word in forbidden:
                self.assertNotIn(word, op.name)
                self.assertNotIn(word, cli.command_name(op.name))


class CliFileGuardTests(unittest.TestCase):
    """Замечания проверки безопасности: --message-file не читает чужое, --file не затирает чужое."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "данные"
        self.data.mkdir()
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        from svnhelper.ops.errors import OpsError
        self.OpsError = OpsError

    def test_message_file_refuses_program_data_big_and_binary_files(self):
        secret = self.data / "settings.json"
        secret.write_text("{}", encoding="utf-8")
        with self.assertRaises(self.OpsError):
            cli.read_message(str(secret))
        big = Path(self.tmp.name) / "большой.txt"
        big.write_bytes(b"a" * (cli.MAX_MESSAGE_BYTES + 1))
        with self.assertRaises(self.OpsError):
            cli.read_message(str(big))
        binary = Path(self.tmp.name) / "двоичный.bin"
        binary.write_bytes(b"ab" + bytes([0]) + b"cd")
        with self.assertRaises(self.OpsError):
            cli.read_message(str(binary))
        fine = Path(self.tmp.name) / "комментарий.txt"
        fine.write_text("Правка допусков в чертеже", encoding="utf-8")
        self.assertEqual(cli.read_message(str(fine)), "Правка допусков в чертеже")

    def test_catalog_file_only_md_outside_program_folders_and_never_over_foreign_file(self):
        from svnhelper.ops.result import Result
        result = Result("commands", data={"markdown": cli.CATALOG_HEADER + chr(10) + "текст"})
        folder = Path(self.tmp.name) / "вне"
        folder.mkdir()
        for bad in (folder / "справочник.txt", self.data / "справочник.md", self.data / "agent_policy.json"):
            self.assertFalse(cli._save_catalog(Result("commands", data=dict(result.data)), str(bad)).ok, bad)
            self.assertFalse(bad.exists() and bad.read_text(encoding="utf-8").startswith(cli.CATALOG_HEADER), bad)
        foreign = folder / "чужой.md"
        foreign.write_text("мои заметки", encoding="utf-8")
        self.assertFalse(cli._save_catalog(Result("commands", data=dict(result.data)), str(foreign)).ok)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "мои заметки")
        mine = folder / "справочник.md"
        self.assertTrue(cli._save_catalog(Result("commands", data=dict(result.data)), str(mine)).ok)
        self.assertTrue(cli._save_catalog(Result("commands", data=dict(result.data)), str(mine)).ok)      # прежний справочник заменяется


if __name__ == "__main__":
    unittest.main()
