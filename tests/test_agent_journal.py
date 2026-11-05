"""Task AG.8: лента журнала действий помощника и отмена из окна (логика без tkinter, отмена на настоящем svn)."""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from svnhelper import agent_journal as journal, agent_policy                       # noqa: E402
from svnhelper.ops import audit                                                    # noqa: E402
from svnhelper.ops.result import ErrorInfo, Result                                 # noqa: E402
from svnhelper.svn_core import SvnClient                                           # noqa: E402

import test_ops_write as write_tests                                               # noqa: E402

NOW = datetime(2026, 11, 5, 12, 0, 0)


def raw(operation="save_apply", **fields) -> dict:
    """Строка журнала так, как её пишет audit.build_entry (поля, которых нет, берутся из умолчаний)."""
    row = {"v": 1, "ts": "2026-11-05T10:30:00+03:00", "pid": 1, "client": "mcp", "client_info": "Claude Code 2.0",
           "user": "ivanov", "operation": operation, "read_only": False, "project": "пробный проект", "files": [],
           "files_total": 0, "plan_id": "", "outcome": "ok", "exit_code": 0, "rev_before": None, "rev_after": None,
           "op_id": "", "comment": "", "comment_truncated": False}
    row.update(fields)
    return row


def entries(*rows) -> list:
    return [journal.parse_entry(row) for row in rows]


class ParsingTests(unittest.TestCase):
    def test_old_row_without_new_fields_is_read(self):
        entry = journal.parse_entry(raw(rev_after=12))
        self.assertIsNone(entry.undone_rev)
        self.assertEqual((entry.rev_after, entry.operation, entry.ok), (12, "save_apply", True))

    def test_broken_values_do_not_raise(self):
        for bad in ({}, {"ts": 5, "files": "x", "rev_after": "7", "exit_code": None}, {"files": [1, None, {"path": 3}]},
                    {"read_only": "no", "project": None}):
            entry = journal.parse_entry(bad)
            self.assertIsInstance(entry.operation, str)
            self.assertEqual(journal.when_text(entry), "—" if entry.when is None else journal.when_text(entry))
            journal.build_rows([entry], journal.FILTER_ALL)

    def test_audit_entry_has_undone_rev_and_old_callers_still_work(self):
        ctx = mock.Mock(client_name="window", client_info="")
        ctx.settings.username = "ivanov"
        entry = audit.build_entry(ctx, "rollback_apply", read_only=False, rev_after=13, undone_rev=12)
        self.assertEqual((entry["rev_after"], entry["undone_rev"]), (13, 12))
        self.assertIsNone(audit.build_entry(ctx, "save_apply")["undone_rev"])

    def test_months_and_loading_by_month_files(self):
        self.assertEqual([m.strftime("%Y-%m") for m in journal.month_starts(3, NOW)],
                         ["2026-11", "2026-10", "2026-09"])
        self.assertEqual([m.strftime("%Y-%m") for m in journal.month_starts(3, datetime(2026, 1, 20))],
                         ["2026-01", "2025-12", "2025-11"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": tmp}):
            folder = Path(tmp) / "agent"
            folder.mkdir()
            for name, number in (("2026-11", 3), ("2026-10", 2), ("2026-05", 1)):
                text = "\n".join(json.dumps(raw(rev_after=number, ts=f"{name}-02T10:00:00+03:00")) for _ in range(2))
                (folder / f"audit_{name}.jsonl").write_text(text + "\nне json\n", encoding="utf-8")
            self.assertEqual(audit.list_months(), [(2026, 11), (2026, 10), (2026, 5)])
            self.assertEqual([e.rev_after for e in journal.load_entries(1, NOW)], [3, 3])
            self.assertEqual([e.rev_after for e in journal.load_entries(3, NOW)], [2, 2, 3, 3])   # старые первыми
            self.assertEqual(len(journal.load_entries(12, NOW)), 6)


class FeedTests(unittest.TestCase):
    def feed(self, rows, flt=journal.FILTER_ALL):
        return journal.build_rows(entries(*rows), flt)

    def test_labels_in_plain_russian(self):
        rows = self.feed([
            raw("save_apply", files=[{"path": "docs/чертёж 1.txt", "action": "modified"}] * 1, files_total=1,
                rev_after=5, client="cli", client_info=""),
            raw("lock_apply", files=[{"path": "a.txt", "action": "занят"}]),
            raw("branch_apply"), raw("create_project_apply"), raw("publish_apply"),
            raw("status", read_only=True), raw("save_plan", read_only=True),
        ])
        what = {row.entry.operation: row.what for row in rows}
        self.assertEqual(what["save_apply"], "сохранил изменения")
        self.assertEqual(what["lock_apply"], "занял файлы")
        self.assertEqual(what["branch_apply"], "создал ветку или версию")
        self.assertEqual(what["create_project_apply"], "создал проект")
        self.assertEqual(what["publish_apply"], "отправил папку на сервер")
        self.assertTrue(what["status"].startswith("посмотрел: "))
        self.assertTrue(what["save_plan"].startswith("составил план: "))
        by_op = {row.entry.operation: row for row in rows}
        self.assertEqual(by_op["save_apply"].who, "Консоль (CLI)")
        self.assertEqual(by_op["lock_apply"].who, "MCP: Claude Code 2.0")
        self.assertEqual((by_op["save_apply"].files, by_op["save_apply"].number), ("чертёж 1.txt", "№ 5"))
        self.assertEqual(by_op["save_apply"].when, "05.11.2026 10:30")
        self.assertEqual(by_op["save_apply"].outcome, "выполнено")

    def test_unknown_operation_and_failed_outcome(self):
        rows = self.feed([raw("новая_операция", read_only=True),
                          raw("save_apply", outcome="policy_denied", exit_code=6),
                          raw("save_apply", outcome="что-то_неизвестное", exit_code=1)])
        texts = {row.entry.outcome: row for row in rows}
        self.assertIn("«новая_операция»", [r.what for r in rows if r.entry.operation == "новая_операция"][0])
        self.assertEqual(texts["policy_denied"].outcome, "Это действие помощнику сейчас не разрешено")
        self.assertEqual(texts["что-то_неизвестное"].outcome, "не выполнено")
        self.assertTrue(texts["policy_denied"].failed)
        self.assertFalse(texts["policy_denied"].can_undo)

    def test_files_cell_shortens_and_project_path_gets_a_name(self):
        many = [{"path": f"f{n}.txt", "action": "modified"} for n in range(5)]
        row = self.feed([raw(files=many, files_total=8, project="D:\\Проекты\\Мой проект")])[0]
        self.assertEqual(row.files, "f0.txt, f1.txt, f2.txt и ещё 5")
        self.assertEqual(row.project, "Мой проект")

    def test_filters(self):
        rows = [raw("save_apply", rev_after=5), raw("status", read_only=True), raw("save_plan", read_only=True)]
        ops = lambda flt: sorted(r.entry.operation for r in self.feed(rows, flt))          # noqa: E731
        self.assertEqual(ops(journal.FILTER_CHANGES), ["save_apply"])
        self.assertEqual(ops(journal.FILTER_READ), ["save_plan", "status"])
        self.assertEqual(len(ops(journal.FILTER_ALL)), 3)
        self.assertEqual(journal.DEFAULT_FILTER, journal.FILTER_CHANGES)
        self.assertIn("не менял", journal.empty_text(journal.FILTER_CHANGES))

    def test_newest_first(self):
        rows = self.feed([raw(ts="2026-11-05T09:00:00+03:00", rev_after=1), raw(ts="2026-11-05T11:00:00+03:00", rev_after=2),
                          raw(ts="2026-11-04T23:00:00+03:00", rev_after=3)])
        self.assertEqual([r.entry.rev_after for r in rows], [2, 1, 3])

    def test_undo_links_and_buttons(self):
        rows = self.feed([
            raw("save_apply", rev_after=10, ts="2026-11-05T09:00:00+03:00"),
            raw("save_apply", rev_after=11, ts="2026-11-05T09:10:00+03:00"),
            raw("rollback_apply", rev_after=12, undone_rev=10, client="window", client_info="",
                ts="2026-11-05T09:20:00+03:00"),
        ])
        by_rev = {r.entry.rev_after: r for r in rows}
        self.assertEqual(by_rev[10].link, "отменено сохранением № 12")
        self.assertFalse(by_rev[10].can_undo)                               # уже отменено
        self.assertEqual(by_rev[11].link, "")
        self.assertTrue(by_rev[11].can_undo)
        self.assertEqual(by_rev[12].link, "отменяет сохранение № 10")
        self.assertTrue(by_rev[12].can_redo and not by_rev[12].can_undo)
        self.assertEqual(by_rev[12].what, "отменил сохранение")
        self.assertEqual(by_rev[12].who, "Окно SVN-lite (вы)")

    def test_redo_chain(self):
        rows = self.feed([
            raw("save_apply", rev_after=10, ts="2026-11-05T09:00:00+03:00"),
            raw("rollback_apply", rev_after=11, undone_rev=10, ts="2026-11-05T09:10:00+03:00"),
            raw("rollback_apply", rev_after=12, undone_rev=11, ts="2026-11-05T09:20:00+03:00"),
        ])
        by_rev = {r.entry.rev_after: r for r in rows}
        self.assertIn("снова действует", by_rev[10].link)                   # отмену вернули: сохранение в силе
        self.assertEqual(by_rev[11].link, "отменяет сохранение № 10; отменено сохранением № 12")
        self.assertFalse(by_rev[11].can_redo)                               # эту отмену уже вернули
        self.assertEqual(by_rev[12].what, "вернул отменённое сохранение")
        self.assertEqual(by_rev[12].link, "отменяет сохранение № 11")
        self.assertTrue(by_rev[12].can_redo)                                # и её можно отменить снова

    def test_failed_rollback_does_not_mark_anything_undone(self):
        rows = self.feed([raw("save_apply", rev_after=10),
                          raw("rollback_apply", outcome="newer_changes", exit_code=7)])
        save = [r for r in rows if r.entry.operation == "save_apply"][0]
        self.assertEqual(save.link, "")
        self.assertTrue(save.can_undo)
        failed = [r for r in rows if r.entry.operation == "rollback_apply"][0]
        self.assertFalse(failed.can_redo)
        self.assertEqual(failed.outcome, "После этого сохранения файлы менял кто-то ещё")

    def test_not_undoable_operations_are_marked(self):
        rows = self.feed([raw("create_project_apply"), raw("publish_apply"), raw("lock_apply"),
                          raw("save_apply", rev_after=4)])
        links = {r.entry.operation: r.link for r in rows}
        self.assertEqual(links["create_project_apply"], "не отменяется")
        self.assertEqual(links["publish_apply"], "не отменяется")
        self.assertEqual(links["save_apply"], "")
        for row in rows:
            if row.entry.operation != "save_apply":
                self.assertFalse(row.can_undo or row.can_redo, row.entry.operation)

    def test_save_without_number_and_plans_cannot_be_undone(self):
        rows = self.feed([raw("save_apply", rev_after=None), raw("rollback_plan", read_only=True, rev_after=7)])
        self.assertFalse(any(r.can_undo or r.can_redo for r in rows))

    def test_filter_does_not_break_links(self):
        rows = self.feed([raw("save_apply", rev_after=10, ts="2026-11-05T09:00:00+03:00"),
                          raw("rollback_apply", rev_after=11, undone_rev=10, ts="2026-11-05T09:10:00+03:00")],
                         journal.FILTER_CHANGES)
        self.assertEqual({r.entry.rev_after: r.link for r in rows},
                         {10: "отменено сохранением № 11", 11: "отменяет сохранение № 10"})

    def test_detail_has_all_files_and_no_password(self):
        row = self.feed([raw(files=[{"path": "a/b.txt", "action": "modified"}], files_total=3, rev_after=9,
                             comment="Правка чертежа")])[0]
        text = journal.detail_text(row)
        self.assertIn("a/b.txt", text)
        self.assertIn("и ещё 2", text)
        self.assertIn("Сохранение № 9", text)
        self.assertIn("Правка чертежа", text)
        self.assertNotIn("password", text.lower())


class TextsTests(unittest.TestCase):
    def failure(self, kind, data=None, svn=None, message="Сообщение.", step="Что делать."):
        return Result.failure("rollback_plan", ErrorInfo(kind, message, step, False, 7, svn=svn or {}), data)

    def test_colleague_changed_file(self):
        one = journal.refusal_text(self.failure("newer_changes", {"authors": ["petrov"]}))
        self.assertIn("файл менял petrov", one)
        self.assertIn("Отменить автоматически нельзя", one)
        many = journal.refusal_text(self.failure("newer_changes", {"authors": ["petrov", "sidorov"]}))
        self.assertIn("petrov, sidorov", many)
        self.assertIn("кто-то ещё", journal.refusal_text(self.failure("newer_changes", {})))

    def test_other_refusals_use_ops_texts(self):
        text = journal.refusal_text(self.failure("local_changes", message="Есть несохранённые правки.", step="Сохраните."))
        self.assertEqual(text, "Есть несохранённые правки. Сохраните.")
        foreign = journal.refusal_text(self.failure("policy_denied", svn={"reason": "rollback_not_mine"}))
        self.assertIn("сделано не вами", foreign)

    def test_confirm_text_lists_files(self):
        row = journal.build_rows(entries(raw(rev_after=7)))[0]
        plan = Result("rollback_plan", data={"count": 20, "files": [{"path": f"f{n}.txt", "action": "M"}
                                                                    for n in range(20)]})
        text = journal.confirm_text(row, plan)
        self.assertIn("Отменить сохранение № 7 в проекте «пробный проект»?", text)
        self.assertIn("f0.txt (был изменён, вернётся прежний вид)", text)
        self.assertIn("и ещё 5", text)
        redo = journal.build_rows(entries(raw("rollback_apply", rev_after=8, undone_rev=7)))[0]
        self.assertIn("Вернуть отмену № 8", journal.confirm_text(redo, plan))

    def test_no_project_in_row(self):
        entry = journal.parse_entry(raw(project="", rev_after=3))
        result = journal.plan_undo(mock.Mock(), entry)
        self.assertFalse(result.ok)
        self.assertIn("проект", journal.refusal_text(result))


class WindowPolicyTests(unittest.TestCase):
    def test_only_rollback_allowed_and_no_counting(self):
        policy = journal.window_policy()
        self.assertEqual(policy.mode, "write")
        self.assertTrue(policy.confirm)
        self.assertFalse(policy.mark_comments)
        policy.check("rollback", "D:\\где-угодно", base_dirs=[], consume=True)       # папки и лимит не проверяются
        for action in ("save_server", "publish", "create_server", "lock"):
            with self.assertRaises(Exception) as caught:
                policy.check(action, None, consume=False)
            self.assertEqual(caught.exception.exit_code if hasattr(caught.exception, "exit_code") else 6, 6)

    def test_version_is_read_only(self):
        with self.assertRaises(Exception):
            journal.window_policy().check("rollback", None, line="tag", consume=False)


class WindowUndoTests(write_tests.WriteOpsBase):
    """Отмена и возврат из окна на настоящем svn: помощник сохраняет, окно отменяет и возвращает."""

    def setUp(self):
        super().setUp()
        self.window_ctx = journal.window_context(self.store, self.config, SvnClient("svn"))

    def saved(self, name="чертёж 1.txt", text="изменено помощником", message="Правка чертежа по просьбе"):
        (self.mine / name).write_text(text, encoding="utf-8")
        return self.plan_and_apply("save", project=write_tests.NAME, message=message)[1].data["number"]

    def rows(self, flt=journal.FILTER_CHANGES):
        return journal.build_rows(journal.load_entries(1), flt)

    def row_for(self, revision, operation):
        return [r for r in self.rows() if r.entry.rev_after == revision and r.entry.operation == operation][0]

    def undo(self, row):
        write_tests.ops_projects.clear_status_cache()
        plan = journal.plan_undo(self.window_ctx, row.entry)
        if not plan.ok:
            return plan, None
        return plan, journal.apply_undo(self.window_ctx, row.entry, plan)

    def test_undo_then_redo_with_links_in_feed(self):
        number = self.saved()
        row = self.row_for(number, "save_apply")
        self.assertTrue(row.can_undo)
        self.assertEqual(row.who, "Консоль (CLI)")
        before = self.head()
        plan, undone = self.undo(row)
        self.assertTrue(plan.ok, plan.text)
        self.assertEqual(self.head(), before + 1)
        self.assertIn("чертёж 1.txt", journal.confirm_text(row, plan))
        self.assertTrue(undone.ok, undone.text)
        self.assertEqual((self.mine / "чертёж 1.txt").read_text(encoding="utf-8"), "hello")
        self.assertEqual(self.last_message().count("по просьбе ИИ-помощника"), 1)       # метки отмены нет: только из исходного
        # лента: связь видна с обеих сторон, кнопки перешли к отмене
        saved_row, undo_row = self.row_for(number, "save_apply"), self.row_for(undone.data["number"], "rollback_apply")
        self.assertEqual(saved_row.link, f"отменено сохранением № {undone.data['number']}")
        self.assertFalse(saved_row.can_undo)
        self.assertEqual(undo_row.link, f"отменяет сохранение № {number}")
        self.assertEqual((undo_row.who, undo_row.entry.undone_rev), ("Окно SVN-lite (вы)", number))
        self.assertTrue(undo_row.can_redo)
        # вернуть вперёд
        _plan, redone = self.undo(undo_row)
        self.assertTrue(redone.ok, redone.text)
        self.assertEqual((self.mine / "чертёж 1.txt").read_text(encoding="utf-8"), "изменено помощником")
        self.assertEqual(svn_status(self.mine), "")
        again = self.row_for(redone.data["number"], "rollback_apply")
        self.assertEqual(again.what, "вернул отменённое сохранение")
        self.assertIn("отмена № ", self.row_for(number, "save_apply").link)
        self.assertIn("снова действует", self.row_for(number, "save_apply").link)
        self.assertFalse(self.row_for(undone.data["number"], "rollback_apply").can_redo)

    def test_window_undo_does_not_use_up_the_assistants_hourly_limit(self):
        number = self.saved()
        counters = write_tests.agent_policy.counters_path()
        stamps = json.loads(counters.read_text(encoding="utf-8"))["writes"]
        _plan, undone = self.undo(self.row_for(number, "save_apply"))
        self.assertTrue(undone.ok, undone.text)
        self.assertEqual(json.loads(counters.read_text(encoding="utf-8"))["writes"], stamps)

    def test_undo_works_when_the_assistant_is_switched_off_by_the_administrator(self):
        number = self.saved()
        off = dataclasses.replace(self.config, agent_max_mode="off")
        self.window_ctx = journal.window_context(self.store, off, SvnClient("svn"))
        _plan, undone = self.undo(self.row_for(number, "save_apply"))
        self.assertTrue(undone.ok, undone.text)

    def test_colleague_changed_file_refuses_with_name_and_touches_nothing(self):
        number = self.saved(text="мой вариант")
        write_tests.svn("svn", "update", str(self.other))
        self.colleague_commit("чертёж 1.txt", "вариант коллеги")
        write_tests.svn("svn", "update", str(self.mine))
        before = self.head()
        plan, undone = self.undo(self.row_for(number, "save_apply"))
        self.assertFalse(plan.ok)
        self.assertIsNone(undone)
        text = journal.refusal_text(plan)
        self.assertIn("файл менял petrov", text)
        self.assertIn("Отменить автоматически нельзя", text)
        self.assertEqual(self.head(), before)
        self.assertEqual((self.mine / "чертёж 1.txt").read_text(encoding="utf-8"), "вариант коллеги")
        failed = [r for r in self.rows(journal.FILTER_ALL) if r.entry.operation == "rollback_plan"][0]
        self.assertTrue(failed.failed)
        self.assertEqual(failed.who, "Окно SVN-lite (вы)")

    def test_foreign_save_is_refused(self):
        self.colleague_commit("чужой.txt", "x")
        write_tests.svn("svn", "update", str(self.mine))
        entry = journal.parse_entry({"operation": "save_apply", "project": write_tests.NAME, "rev_after": self.head(),
                                     "read_only": False, "outcome": "ok", "exit_code": 0})
        plan = journal.plan_undo(self.window_ctx, entry)
        self.assertFalse(plan.ok)
        self.assertIn("сделано не вами", journal.refusal_text(plan))

    def test_unsaved_changes_refuse_with_ops_text(self):
        number = self.saved()
        (self.mine / "ещё.txt").write_text("e", encoding="utf-8")
        plan = journal.plan_undo(self.window_ctx, self.row_for(number, "save_apply").entry)
        self.assertFalse(plan.ok)
        self.assertIn("несохранённые", journal.refusal_text(plan))

    def test_password_of_window_client_never_reaches_the_journal(self):
        number = self.saved()
        client = SvnClient("svn")
        client.set_credentials("ivanov", write_tests.CANARY)
        ctx = journal.window_context(self.store, self.config, client)
        self.assertEqual(ctx.config.password, "")
        plan = journal.plan_undo(ctx, self.row_for(number, "save_apply").entry)
        self.assertTrue(plan.ok, plan.text)
        for path in (write_tests.audit.audit_dir()).glob("*.jsonl"):
            self.assertNotIn(write_tests.CANARY, path.read_text(encoding="utf-8", errors="replace"))


def svn_status(path) -> str:
    return write_tests.svn("svn", "status", str(path)).strip()


if __name__ == "__main__":
    unittest.main()
