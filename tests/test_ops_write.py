"""Task AG.5 и AG.8: операции записи слоя ops на настоящем svn через file:/// (получить новое, сохранить, занять и
освободить, ветки и версии, переключение, отмена сохранения), политика и план с токеном, журнал действий."""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from svnhelper import agent_policy, folderlock, publish                                           # noqa: E402
from svnhelper.agent_policy import MODE_CONFIRM, MODE_FULL, SIG_OK, Policy                        # noqa: E402
from svnhelper.config import Config, SettingsStore                                                # noqa: E402
from svnhelper.ops import audit, make_context, plan as plans, projects as ops_projects, run       # noqa: E402
from svnhelper.svn_core import SvnClient                                                          # noqa: E402

HAVE_SVN = bool(shutil.which("svn") and shutil.which("svnadmin"))
ME = getpass.getuser()
NAME = "пробный проект"
ALL = ("save_local", "save_server", "lock", "get_new", "create_local", "create_server", "publish", "branches",
       "rollback")
CANARY = "КАНАРЕЙКА-пароль-77"


def svn(*args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(f"{args}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode(errors="replace")


def policy(mode=MODE_CONFIRM, allow=ALL, **limits) -> Policy:
    return Policy(file_mode=mode, ceiling="write", allow=tuple(allow), signature=SIG_OK, **limits)


@unittest.skipUnless(HAVE_SVN, "нет svn/svnadmin")
class WriteOpsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = root = Path(self.tmp.name)
        saved = os.environ.get("SVNLITE_DATA_DIR")
        self.addCleanup(lambda: os.environ.pop("SVNLITE_DATA_DIR", None) if saved is None
                        else os.environ.__setitem__("SVNLITE_DATA_DIR", saved))
        os.environ["SVNLITE_DATA_DIR"] = str(root / "data")
        repo = root / "repos" / "test"
        repo.parent.mkdir()
        svn("svnadmin", "create", str(repo))
        init = root / "init"
        svn("svn", "checkout", repo.as_uri(), str(init))
        proj = init / "projects" / NAME
        for sub in ("trunk/docs", "branches", "tags"):
            (proj / sub).mkdir(parents=True)
        (proj / "trunk" / "чертёж 1.txt").write_text("hello", encoding="utf-8")
        (proj / "trunk" / "docs" / "b.txt").write_text("b", encoding="utf-8")
        svn("svn", "add", "--force", ".", cwd=init)
        svn("svn", "commit", "-m", "init", cwd=init)
        from urllib.parse import quote
        self.url = repo.as_uri() + "/projects/" + quote(NAME) + "/trunk"
        self.base = root / "Проекты"
        self.base.mkdir()
        self.mine = self.base / NAME
        self.other = root / "other"
        svn("svn", "checkout", self.url, str(self.mine))
        svn("svn", "checkout", self.url, str(self.other))
        self.store = SettingsStore(root / "settings.json")
        self.store.settings.project_dirs = [str(self.base)]
        self.store.settings.username = ME
        self.config = Config(server=(root / "repos").as_uri(), repos=["test"])
        ops_projects.clear_status_cache()
        self.addCleanup(ops_projects.clear_status_cache)
        self.use(policy())

    def use(self, pol: Policy):
        self.ctx = make_context(client=SvnClient("svn"), store=self.store, config=self.config, policy=pol)
        return self.ctx

    def call(self, op_name, /, **params):
        ops_projects.clear_status_cache()
        return run(self.ctx, op_name, **params)

    def plan_and_apply(self, op, **params):
        planned = self.call(op + "_plan", **params)
        self.assertTrue(planned.ok, planned.text)
        token = planned.data["plan_id"]
        self.assertTrue(token)
        return planned, self.call(op + "_apply", token=token, **params)

    def last_message(self) -> str:
        """Комментарий последнего сохранения (разбор XML: консольный вывод svn идёт в кодировке системы)."""
        import xml.etree.ElementTree as ET
        out = subprocess.run(["svn", "log", "--xml", "-l", "1", self.url], stdout=subprocess.PIPE, check=True).stdout
        return ET.fromstring(out).findtext("logentry/msg") or ""

    def ls_names(self, url: str) -> list:
        import xml.etree.ElementTree as ET
        out = subprocess.run(["svn", "ls", "--xml", url], stdout=subprocess.PIPE, check=True).stdout
        return [e.findtext("name") for e in ET.fromstring(out).iter("entry")]

    def head(self) -> int:
        out = svn("svn", "info", "--show-item", "last-changed-revision", self.url)
        return int(out.strip())

    def colleague_commit(self, name: str, text: str, message="Правка от коллеги"):
        (self.other / name).write_text(text, encoding="utf-8")
        svn("svn", "add", "--force", str(self.other / name) + "@")
        svn("svn", "commit", "-m", message, str(self.other), "--username", "petrov")


class PolicyAndPlanTests(WriteOpsBase):
    def test_read_only_policy_refuses_plan_and_apply(self):
        self.use(Policy(file_mode="read_only", ceiling="write", signature=SIG_OK))
        (self.mine / "новый.txt").write_text("x", encoding="utf-8")
        for op in ("save_plan", "save_apply"):
            result = self.call(op, project=NAME, message="Пробное сохранение файла")
            self.assertFalse(result.ok)
            self.assertEqual(result.exit_code, 6, op)

    def test_missing_permission_refuses(self):
        self.use(policy(allow=("lock",)))
        (self.mine / "новый.txt").write_text("x", encoding="utf-8")
        result = self.call("save_plan", project=NAME, message="Пробное сохранение файла")
        self.assertEqual(result.exit_code, 6)

    def test_confirm_mode_needs_token_and_apply_is_one_shot(self):
        (self.mine / "новый файл.txt").write_text("привет", encoding="utf-8")
        before = self.head()
        planned = self.call("save_plan", project=NAME, message="Добавлен новый файл с приветствием")
        self.assertTrue(planned.ok, planned.text)
        self.assertEqual(self.head(), before)                          # план ничего не меняет
        self.assertTrue(planned.data["requires_confirmation"])
        token = planned.data["plan_id"]
        # без токена
        refused = self.call("save_apply", project=NAME, message="Добавлен новый файл с приветствием")
        self.assertEqual(refused.exit_code, 6)
        self.assertEqual(self.head(), before)
        # токен от другого комментария
        other = self.call("save_apply", project=NAME, message="Совсем другой комментарий к файлу", token=token)
        self.assertEqual((other.exit_code, other.error.kind), (6, "policy_denied"))
        self.assertEqual(self.head(), before)
        # верный токен
        done = self.call("save_apply", project=NAME, message="Добавлен новый файл с приветствием", token=token)
        self.assertTrue(done.ok, done.text)
        self.assertEqual(done.data["number"], before + 1)
        self.assertEqual(self.head(), before + 1)
        # повтор: тот же ответ, номер не растёт
        again = self.call("save_apply", project=NAME, message="Добавлен новый файл с приветствием", token=token)
        self.assertTrue(again.ok)
        self.assertTrue(again.data["already_applied"])
        self.assertEqual(again.data["number"], before + 1)
        self.assertEqual(self.head(), before + 1)
        self.assertIn("по просьбе ИИ-помощника", self.last_message())

    def test_expired_and_unknown_token(self):
        (self.mine / "новый.txt").write_text("x", encoding="utf-8")
        planned = self.call("save_plan", project=NAME, message="Пробное сохранение файла")
        token = planned.data["plan_id"]
        for bad in ("0" * 32, "../../etc", "x" * 5, token[:-1] + ("0" if token[-1] != "0" else "1")):
            result = self.call("save_apply", project=NAME, message="Пробное сохранение файла", token=bad)
            self.assertEqual(result.exit_code, 6, bad)
        stored = plans.load(token)
        stored.expires = 1.0
        plans._write(stored)
        result = self.call("save_apply", project=NAME, message="Пробное сохранение файла", token=token)
        self.assertEqual(result.exit_code, 6)

    def test_stale_plan_when_files_change(self):
        (self.mine / "новый.txt").write_text("x", encoding="utf-8")
        planned = self.call("save_plan", project=NAME, message="Пробное сохранение файла")
        (self.mine / "ещё один.txt").write_text("y", encoding="utf-8")
        result = self.call("save_apply", project=NAME, message="Пробное сохранение файла",
                           token=planned.data["plan_id"])
        self.assertEqual((result.exit_code, result.error.kind), (6, "policy_denied"))
        self.assertIn("изменился", result.error.message)

    def test_full_mode_applies_without_token_but_limits_hold(self):
        self.use(policy(MODE_FULL, max_writes_per_hour=2))
        (self.mine / "a.txt").write_text("1", encoding="utf-8")
        first = self.call("save_apply", project=NAME, message="Первое сохранение файла a")
        self.assertTrue(first.ok, first.text)
        (self.mine / "b.txt").write_text("2", encoding="utf-8")
        second = self.call("save_apply", project=NAME, message="Второе сохранение файла b")
        self.assertTrue(second.ok, second.text)
        (self.mine / "c.txt").write_text("3", encoding="utf-8")
        third = self.call("save_apply", project=NAME, message="Третье сохранение файла c")
        self.assertEqual(third.exit_code, 6)
        self.assertEqual(self.head(), second.data["number"])

    def test_busy_folder_gives_code_8_and_changes_nothing(self):
        (self.mine / "новый.txt").write_text("x", encoding="utf-8")
        before = self.head()
        planned = self.call("save_plan", project=NAME, message="Пробное сохранение файла")
        with publish.mark_busy(str(self.mine), "тест"):
            result = self.call("save_apply", project=NAME, message="Пробное сохранение файла",
                               token=planned.data["plan_id"])
        self.assertEqual(result.exit_code, 8)
        self.assertEqual(self.head(), before)

    def test_audit_records_apply_without_secrets(self):
        (self.mine / "новый.txt").write_text("x", encoding="utf-8")
        _planned, done = self.plan_and_apply("save", project=NAME, message="Пробное сохранение файла")
        rows = [r for r in audit.read_entries() if r["operation"] == "save_apply"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertFalse(row["read_only"])
        self.assertEqual((row["outcome"], row["rev_after"]), ("ok", done.data["number"]))
        self.assertEqual(row["rev_before"], done.data["number"] - 1)
        self.assertTrue(row["plan_id"] and row["op_id"])
        self.assertEqual([f["path"] for f in row["files"]], ["новый.txt"])
        self.assertIn("Пробное сохранение файла", row["comment"])
        self.assertTrue((Path(os.environ["SVNLITE_DATA_DIR"]) / "agent" / "last_change.json").is_file())


class SaveTests(WriteOpsBase):
    def test_incoming_changes_block_save_with_code_7(self):
        self.colleague_commit("от коллеги.txt", "x")
        (self.mine / "моё.txt").write_text("m", encoding="utf-8")
        result = self.call("save_plan", project=NAME, message="Пробное сохранение файла")
        self.assertEqual((result.exit_code, result.error.kind), (7, "incoming_first"))

    def test_conflict_blocks_save(self):
        self.colleague_commit("чертёж 1.txt", "коллега")
        (self.mine / "чертёж 1.txt").write_text("мой", encoding="utf-8")
        svn("svn", "update", "--accept", "postpone", str(self.mine))
        result = self.call("save_plan", project=NAME, message="Пробное сохранение файла")
        self.assertEqual(result.exit_code, 7)

    def test_nothing_to_save_is_ok_and_has_no_token(self):
        result = self.call("save_plan", project=NAME, message="Пробное сохранение файла")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["noop"])
        self.assertEqual(result.data["plan_id"], "")

    def test_limits_and_short_comment(self):
        self.use(policy(max_files_per_save=2, max_deletes_per_save=0, min_comment_chars=10))
        for i in range(3):
            (self.mine / f"f{i}.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.call("save_plan", project=NAME, message="Три новых файла сразу").exit_code, 6)
        self.assertEqual(self.call("save_plan", project=NAME, message="коротко", files=["f0.txt"]).exit_code, 6)
        ok = self.call("save_plan", project=NAME, message="Только один файл из трёх", files=["f0.txt"])
        self.assertTrue(ok.ok, ok.text)
        self.assertEqual(ok.data["count"], 1)
        os.remove(self.mine / "чертёж 1.txt")
        refused = self.call("save_plan", project=NAME, message="Удаление чертежа из проекта", files=["чертёж 1.txt"])
        self.assertEqual(refused.exit_code, 6)
        allowed = self.call("save_plan", project=NAME, message="Удаление чертежа из проекта",
                            files=["чертёж 1.txt"], allow_deletes=True)
        self.assertTrue(allowed.ok, allowed.text)

    def test_unknown_file_and_lock_required(self):
        missing = self.call("save_plan", project=NAME, message="Пробное сохранение файла", files=["нет такого.txt"])
        self.assertEqual(missing.exit_code, 1)
        svn("svn", "propset", "svn:needs-lock", "yes", str(self.mine / "docs" / "b.txt") + "@")
        svn("svn", "commit", "-m", "needs-lock", str(self.mine))
        svn("svn", "update", str(self.other))
        os.chmod(self.mine / "docs" / "b.txt", 0o666)
        (self.mine / "docs" / "b.txt").write_text("правка без занятия", encoding="utf-8")
        result = self.call("save_plan", project=NAME, message="Правка файла без занятия")
        self.assertEqual((result.exit_code, result.error.kind), (7, "lock_needed"))
        self.assertIn("docs/b.txt", result.error.to_dict() and result.data["files"])

    def test_wrong_project_ref_and_outside_folder(self):
        outside = self.root / "вне" / "проект"
        svn("svn", "checkout", self.url, str(outside))
        (outside / "x.txt").write_text("x", encoding="utf-8")
        for ref in (str(outside), "нет-такого-проекта", "..\\x"):
            result = self.call("save_plan", project=ref, message="Пробное сохранение файла")
            self.assertFalse(result.ok, ref)
            self.assertIn(result.exit_code, (1, 6), ref)

    def test_save_in_version_is_refused_with_code_5(self):
        from svnhelper.ops.projects import Probe
        with mock.patch("svnhelper.ops.projects.probe_folder") as probe:
            real = ops_projects.probe_folder.__wrapped__ if hasattr(ops_projects.probe_folder, "__wrapped__") else None
            self.assertIsNone(real)
        tag_dir = self.base / "версия"
        svn("svn", "copy", self.url.rsplit("/", 1)[0] + "/trunk", self.url.rsplit("/", 1)[0] + "/tags/v1", "-m", "tag")
        svn("svn", "checkout", self.url.rsplit("/", 1)[0] + "/tags/v1", str(tag_dir))
        (tag_dir / "x.txt").write_text("x", encoding="utf-8")
        result = self.call("save_plan", project="версия", message="Пробное сохранение файла")
        self.assertEqual(result.exit_code, 5)


class GetNewAndLockTests(WriteOpsBase):
    def test_get_new_applies_and_keeps_local_changes(self):
        self.colleague_commit("от коллеги.txt", "x")
        (self.mine / "моё.txt").write_text("m", encoding="utf-8")
        _plan, done = self.plan_and_apply("get_new", project=NAME)
        self.assertTrue(done.ok, done.text)
        self.assertEqual(done.data["number"], self.head())
        self.assertTrue((self.mine / "от коллеги.txt").is_file())
        self.assertTrue((self.mine / "моё.txt").is_file())

    def test_get_new_nothing_incoming(self):
        planned = self.call("get_new_plan", project=NAME)
        self.assertTrue(planned.ok and planned.data["noop"])

    def test_get_new_conflict_is_reported_with_code_7(self):
        self.colleague_commit("чертёж 1.txt", "коллега")
        (self.mine / "чертёж 1.txt").write_text("мой", encoding="utf-8")
        _plan, done = self.plan_and_apply("get_new", project=NAME)
        self.assertEqual(done.exit_code, 7)
        self.assertEqual(done.data["conflicts"], ["чертёж 1.txt"])

    def test_lock_and_unlock_own_file(self):
        planned, locked = self.plan_and_apply("lock", project=NAME, files=["docs/b.txt"])
        self.assertTrue(locked.ok, locked.text)
        self.assertIn("docs/b.txt", [x.replace("\\", "/") for x in locked.data["files"]])
        info = svn("svn", "info", str(self.mine / "docs" / "b.txt") + "@")
        self.assertIn("Lock Token", info)
        again = self.call("lock_plan", project=NAME, files=["docs/b.txt"])
        self.assertTrue(again.data["noop"])
        _p, freed = self.plan_and_apply("unlock", project=NAME, files=["docs/b.txt"])
        self.assertTrue(freed.ok, freed.text)
        self.assertNotIn("Lock Token", svn("svn", "info", str(self.mine / "docs" / "b.txt") + "@"))

    def test_foreign_lock_is_never_broken(self):
        svn("svn", "lock", str(self.other / "docs" / "b.txt") + "@", "-m", "правлю", "--username", "petrov")
        taken = self.call("lock_plan", project=NAME, files=["docs/b.txt"])
        self.assertEqual((taken.exit_code, taken.error.kind), (5, "lock_foreign"))
        freed = self.call("unlock_plan", project=NAME, files=["docs/b.txt"])
        self.assertEqual((freed.exit_code, freed.error.kind), (5, "lock_foreign"))
        self.assertIn("petrov", svn("svn", "info", self.url + "/docs/b.txt"))

    def test_lock_rejects_paths_outside_project(self):
        for bad in ("../x.txt", "нет.txt", ".svn/entries", "docs"):
            result = self.call("lock_plan", project=NAME, files=[bad])
            self.assertFalse(result.ok, bad)


class LineTests(WriteOpsBase):
    def test_create_branch_and_version_and_switch(self):
        _p, made = self.plan_and_apply("branch", project=NAME, name="допуски", kind="branch")
        self.assertTrue(made.ok, made.text)
        _p, tagged = self.plan_and_apply("branch", project=NAME, name="2026-11-05", kind="version")
        self.assertTrue(tagged.ok, tagged.text)
        self.assertIn("допуски", self.ls_names(self.url.rsplit("/", 1)[0] + "/branches"))
        self.assertIn("2026-11-05", self.ls_names(self.url.rsplit("/", 1)[0] + "/tags"))
        duplicate = self.call("branch_plan", project=NAME, name="допуски")
        self.assertEqual(duplicate.exit_code, 1)
        bad_name = self.call("branch_plan", project=NAME, name="a/b")
        self.assertEqual(bad_name.exit_code, 2)
        _p, switched = self.plan_and_apply("switch", project=NAME, kind="branch", name="допуски")
        self.assertTrue(switched.ok, switched.text)
        self.assertIn("branches/", svn("svn", "info", "--show-item", "url", str(self.mine) + "@"))
        same = self.call("switch_plan", project=NAME, kind="branch", name="допуски")
        self.assertTrue(same.data["noop"])
        _p, back = self.plan_and_apply("switch", project=NAME, kind="main")
        self.assertTrue(back.ok, back.text)
        self.assertTrue(svn("svn", "info", "--show-item", "url", str(self.mine) + "@").strip().endswith("trunk"))

    def test_switch_refuses_with_unsaved_changes_or_missing_line(self):
        (self.mine / "новый.txt").write_text("x", encoding="utf-8")
        self.plan_and_apply("branch", project=NAME, name="ветка1")
        (self.mine / "docs" / "b.txt").write_text("правка", encoding="utf-8")
        refused = self.call("switch_plan", project=NAME, kind="branch", name="ветка1")
        self.assertEqual(refused.exit_code, 7)
        missing = self.call("switch_plan", project=NAME, kind="branch", name="нет такой")
        self.assertEqual(missing.exit_code, 1)

    def test_branches_need_their_own_permission(self):
        self.use(policy(allow=("save_server", "lock", "get_new")))
        self.assertEqual(self.call("branch_plan", project=NAME, name="x").exit_code, 6)
        self.assertEqual(self.call("switch_plan", project=NAME, kind="main").exit_code, 6)


class RollbackTests(WriteOpsBase):
    def save(self, name, text, message):
        (self.mine / name).write_text(text, encoding="utf-8")
        return self.plan_and_apply("save", project=NAME, message=message)[1]

    def test_rollback_and_redo(self):
        done = self.save("чертёж 1.txt", "изменено помощником", "Правка чертежа по просьбе")
        revision = done.data["number"]
        _p, undone = self.plan_and_apply("rollback", project=NAME, revision=revision)
        self.assertTrue(undone.ok, undone.text)
        self.assertEqual(undone.data["number"], revision + 1)
        self.assertEqual((self.mine / "чертёж 1.txt").read_text(encoding="utf-8"), "hello")
        self.assertEqual(self.head(), revision + 1)
        self.assertIn("Отмена сохранения", self.last_message())
        # вернуть вперёд: отменить отмену
        _p, redone = self.plan_and_apply("rollback", project=NAME, revision=revision + 1)
        self.assertTrue(redone.ok, redone.text)
        self.assertEqual((self.mine / "чертёж 1.txt").read_text(encoding="utf-8"), "изменено помощником")
        status = svn("svn", "status", str(self.mine))
        self.assertEqual(status.strip(), "")

    def test_rollback_of_added_and_deleted_files(self):
        added = self.save("новый.txt", "x", "Добавление нового файла")
        _p, undone = self.plan_and_apply("rollback", project=NAME, revision=added.data["number"])
        self.assertTrue(undone.ok, undone.text)
        self.assertFalse((self.mine / "новый.txt").exists())
        os.remove(self.mine / "docs" / "b.txt")
        self.use(policy(max_deletes_per_save=10))
        removed = self.plan_and_apply("save", project=NAME, message="Удаление файла b из проекта")[1]
        self.assertTrue(removed.ok, removed.text)
        _p, restored = self.plan_and_apply("rollback", project=NAME, revision=removed.data["number"])
        self.assertTrue(restored.ok, restored.text)
        self.assertEqual((self.mine / "docs" / "b.txt").read_text(encoding="utf-8"), "b")

    def test_rollback_refused_when_colleague_changed_file_later(self):
        done = self.save("чертёж 1.txt", "мой вариант", "Правка чертежа по просьбе")
        svn("svn", "update", str(self.other))
        self.colleague_commit("чертёж 1.txt", "вариант коллеги")
        svn("svn", "update", str(self.mine))
        before = self.head()
        result = self.call("rollback_plan", project=NAME, revision=done.data["number"])
        self.assertEqual((result.exit_code, result.error.kind), (7, "newer_changes"))
        self.assertEqual(self.head(), before)

    def test_rollback_refuses_foreign_revision_and_project_creation(self):
        (self.other / "x.txt").write_text("x", encoding="utf-8")
        svn("svn", "add", str(self.other / "x.txt") + "@")
        svn("svn", "commit", "-m", "чужое", str(self.other), "--username", "petrov")
        svn("svn", "update", str(self.mine))
        foreign = self.call("rollback_plan", project=NAME, revision=self.head())
        self.assertEqual(foreign.exit_code, 6)
        first = self.call("rollback_plan", project=NAME, revision=1)
        self.assertNotEqual(first.exit_code, 0)
        self.assertEqual(self.call("rollback_plan", project=NAME, revision=9999).exit_code, 1)

    def test_rollback_needs_clean_folder_and_permission(self):
        done = self.save("чертёж 1.txt", "мой вариант", "Правка чертежа по просьбе")
        (self.mine / "ещё.txt").write_text("e", encoding="utf-8")
        result = self.call("rollback_plan", project=NAME, revision=done.data["number"])
        self.assertEqual(result.exit_code, 7)
        os.remove(self.mine / "ещё.txt")
        self.use(policy(allow=("save_server",)))
        self.assertEqual(self.call("rollback_plan", project=NAME, revision=done.data["number"]).exit_code, 6)


class PlanStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_fingerprint_ignores_token_none_and_order(self):
        a = plans.fingerprint("save", "C:\\p", {"project": "x", "message": "m", "files": None, "token": "t"})
        b = plans.fingerprint("save", "C:\\p", {"message": "m", "project": "x"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, plans.fingerprint("save", "C:\\p", {"project": "x", "message": "other"}))

    def test_token_format_and_single_claim(self):
        made = plans.create("save", "C:\\p", {"a": 1}, "sig", "summary", 10)
        self.assertRegex(made.id, r"^[0-9a-f]{32}$")
        self.assertIsNotNone(plans.claim(made.id))
        self.assertIsNone(plans.claim(made.id))             # второй раз взять нельзя
        plans.finish(made, result={"data": {}})
        self.assertEqual(plans.load(made.id).state, plans.APPLIED)
        self.assertIsNone(plans.load("../x"))
        self.assertIsNone(plans.load(None))

    def test_prune_removes_old_plans(self):
        made = plans.create("save", "C:\\p", {}, "sig", "s", 10)
        old = plans.plans_dir() / f"{made.id}.json"
        os.utime(old, (1, 1))
        plans.prune()
        self.assertFalse(old.exists())


class WindowLockTests(WriteOpsBase):
    """AG.0d: окно держит межпроцессный замок на время своей работы; помощник в это время отказывает кодом 8."""

    def test_window_hold_blocks_agent_write_and_agent_busy_blocks_window(self):
        from svnhelper.gui.projects_tab import ProjectsTabMixin
        (self.mine / "из окна.txt").write_text("x", encoding="utf-8")
        before = self.head()
        planned = self.call("save_plan", project=NAME, message="Пробное сохранение файла")
        seen = {}

        def window_work():
            seen["held"] = folderlock.held_here(self.mine)
            seen["busy_for_window"] = publish.is_busy(str(self.mine))         # свой замок занятостью не считается
            # пока окно работает, помощник той же программы отказывает: папка занята
            with mock.patch.object(folderlock, "held_by_other", return_value=True):
                seen["agent"] = self.call("save_apply", project=NAME, message="Пробное сохранение файла",
                                          token=planned.data["plan_id"])
            return "готово"

        self.assertEqual(ProjectsTabMixin._held(str(self.mine), window_work)(), "готово")
        self.assertTrue(seen["held"] and not seen["busy_for_window"])
        self.assertEqual(seen["agent"].exit_code, 8)
        self.assertEqual(self.head(), before)
        self.assertFalse(folderlock.held_here(self.mine))                       # после работы замок снят
        # и наоборот: пока папку держит помощник (mark_busy), окно получает FolderBusy
        with publish.mark_busy(str(self.mine), "агент"):
            with self.assertRaises(folderlock.FolderBusy):
                ProjectsTabMixin._held(str(self.mine), lambda: None)()

    def test_agent_change_mark_is_seen_once(self):
        from svnhelper.gui.projects_tab import ProjectsTabMixin

        class Window(ProjectsTabMixin):
            def __init__(self):
                pass
        window = Window()
        self.assertFalse(window._agent_changed_projects())                      # файла нет
        (self.mine / "из помощника.txt").write_text("x", encoding="utf-8")
        self.plan_and_apply("save", project=NAME, message="Пробное сохранение файла")
        self.assertFalse(window._agent_changed_projects())                      # первое чтение запоминает метку
        (self.mine / "ещё.txt").write_text("y", encoding="utf-8")
        self.plan_and_apply("save", project=NAME, message="Второе сохранение файла")
        self.assertTrue(window._agent_changed_projects())
        self.assertFalse(window._agent_changed_projects())


class ProjectOpsTests(WriteOpsBase):
    """create_project и publish на настоящем svn: шаблон лежит на «сервере» file:///, проект создаётся на компьютере
    (с историей, если есть svnadmin) и сразу на сервере; готовая папка отправляется на сервер."""

    def setUp(self):
        super().setUp()
        import test_templates as tt
        from urllib.parse import quote
        self.template_id = "Шаблон проекта"
        src = tt.make_template(self.root / "tpl_src" / self.template_id, parent="test/projects")
        init = self.root / "init"
        shutil.copytree(src, init / "templates" / self.template_id)
        svn("svn", "add", "--force", ".", cwd=init)
        svn("svn", "commit", "-m", "шаблон", cwd=init)
        self.config.templates_root = (self.root / "repos" / "test").as_uri() + "/templates"
        self.config.project_folder_format = "{{date}}_{{name}}"
        self.new_base = self.root / "Новые"
        self.new_base.mkdir()
        self.store.settings.project_dirs = [str(self.base), str(self.new_base)]
        self.fields = ["name=вафли", "date=261028"]

    def test_create_local_project_plan_and_apply(self):
        listing = self.call("templates")
        self.assertTrue(listing.ok, listing.text)
        planned = self.call("create_project_plan", template=self.template_id, fields=self.fields,
                            base_dir=str(self.new_base))
        self.assertTrue(planned.ok, planned.text)
        self.assertFalse((self.new_base / planned.data["folder"]).exists())          # план ничего не создаёт
        applied = self.call("create_project_apply", template=self.template_id, fields=self.fields,
                            base_dir=str(self.new_base), token=planned.data["plan_id"])
        self.assertTrue(applied.ok, applied.text)
        folder = self.new_base / planned.data["folder"]
        self.assertTrue((folder / "template.json").exists() is False)
        self.assertTrue(folder.is_dir())
        self.assertTrue((folder / "project.json").is_file())
        # и после этого проект виден помощнику
        names = [row["name"] for row in self.call("projects").data["projects"]]
        self.assertIn(planned.data["folder"], names)
        # повтор не создаёт второй раз и не падает
        again = self.call("create_project_apply", template=self.template_id, fields=self.fields,
                          base_dir=str(self.new_base), token=planned.data["plan_id"])
        self.assertTrue(again.data.get("already_applied"))

    def test_create_refused_without_permission_or_for_unknown_template_or_outside_folder(self):
        self.use(policy(allow=("save_server",)))
        self.assertEqual(self.call("create_project_plan", template=self.template_id, fields=self.fields).exit_code, 6)
        self.use(policy())
        self.assertEqual(self.call("create_project_plan", template="нет такого", fields=self.fields,
                                   base_dir=str(self.new_base)).exit_code, 1)
        outside = self.root / "вне"
        outside.mkdir()
        self.assertEqual(self.call("create_project_plan", template=self.template_id, fields=self.fields,
                                   base_dir=str(outside)).exit_code, 6)
        self.assertEqual(self.call("create_project_plan", template=self.template_id, fields=["нет=поля"],
                                   base_dir=str(self.new_base)).exit_code, 2)

    def test_create_server_needs_its_own_permission_and_works(self):
        self.use(policy(allow=("create_local",)))
        self.assertEqual(self.call("create_project_plan", template=self.template_id, fields=self.fields,
                                   storage="server", place="test/projects", base_dir=str(self.new_base)).exit_code, 6)
        self.use(policy())
        planned = self.call("create_project_plan", template=self.template_id, fields=self.fields, storage="server",
                            place="test/projects", base_dir=str(self.new_base))
        self.assertTrue(planned.ok, planned.text)
        done = self.call("create_project_apply", template=self.template_id, fields=self.fields, storage="server",
                         place="test/projects", base_dir=str(self.new_base), token=planned.data["plan_id"])
        self.assertTrue(done.ok, done.text)
        self.assertTrue(done.data["on_server"], done.data)
        listing = self.ls_names((self.root / "repos" / "test").as_uri() + "/projects")
        self.assertIn(planned.data["folder"], listing)

    def test_publish_local_folder_to_server(self):
        folder = self.new_base / "моя папка"
        folder.mkdir()
        (folder / "файл.txt").write_text("привет", encoding="utf-8")
        planned = self.call("publish_plan", folder=str(folder), place="test/projects")
        self.assertTrue(planned.ok, planned.text)
        self.assertFalse((folder / ".svn").exists())
        done = self.call("publish_apply", folder=str(folder), place="test/projects", token=planned.data["plan_id"])
        self.assertTrue(done.ok, done.text)
        self.assertTrue((folder / ".svn").exists())
        self.assertIn("моя папка", self.ls_names((self.root / "repos" / "test").as_uri() + "/projects"))
        # повторная отправка той же папки: уже на сервере
        second = self.call("publish_plan", folder=str(folder), place="test/projects")
        self.assertFalse(second.ok)
        # и без разрешения отказ
        self.use(policy(allow=("save_server",)))
        self.assertEqual(self.call("publish_plan", folder=str(folder), place="test/projects").exit_code, 6)


if __name__ == "__main__":
    unittest.main()
