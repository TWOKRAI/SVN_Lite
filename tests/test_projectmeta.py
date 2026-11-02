"""Тесты project.json: запись, чтение, неизвестные поля, битый файл, автор."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from svnhelper import projectmeta
from svnhelper.projectmeta import ProjectMeta


def _sample() -> ProjectMeta:
    return ProjectMeta(
        name="отбраковщик вафель", folder="261028_отбраковщик вафель",
        created="2026-10-28T14:05:00", author="ivanov",
        template={"id": "electronics", "title": "Электроника: проект", "revision": 412},
        fields={"date": "261028", "name": "отбраковщик вафель"})


class ProjectMetaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_roundtrip(self):
        meta = _sample()
        projectmeta.write(self.dir, meta)
        self.assertEqual(projectmeta.read(self.dir), meta)

    def test_roundtrip_with_svn(self):
        meta = _sample()
        meta.svn = {"url": "svn://srv/p/trunk", "published": "2026-10-29T10:00:00"}
        projectmeta.write(self.dir, meta)
        self.assertEqual(projectmeta.read(self.dir), meta)

    def test_file_is_utf8_with_format_and_readable_cyrillic(self):
        projectmeta.write(self.dir, _sample())
        raw = (self.dir / "project.json").read_bytes().decode("utf-8")
        data = json.loads(raw)
        self.assertEqual(data["format"], 1)
        self.assertIsNone(data["svn"])
        self.assertIn("отбраковщик вафель", raw)

    def test_missing_file_returns_none(self):
        self.assertIsNone(projectmeta.read(self.dir))

    def test_broken_file_returns_none_and_logs(self):
        (self.dir / "project.json").write_text("{не json", encoding="utf-8")
        with self.assertLogs("svnhelper.projectmeta", level="WARNING"):
            self.assertIsNone(projectmeta.read(self.dir))

    def test_not_an_object_returns_none(self):
        (self.dir / "project.json").write_text("[1, 2]", encoding="utf-8")
        with self.assertLogs("svnhelper.projectmeta", level="WARNING"):
            self.assertIsNone(projectmeta.read(self.dir))

    def test_missing_required_fields_returns_none(self):
        (self.dir / "project.json").write_text('{"format": 1}', encoding="utf-8")
        with self.assertLogs("svnhelper.projectmeta", level="WARNING"):
            self.assertIsNone(projectmeta.read(self.dir))

    def test_unknown_fields_survive_rewrite(self):
        data = {"format": 1, "name": "n", "folder": "f", "future": {"a": 1}}
        (self.dir / "project.json").write_text(json.dumps(data), encoding="utf-8")
        meta = projectmeta.read(self.dir)
        self.assertEqual(meta.extra, {"future": {"a": 1}})
        meta.author = "petrov"
        projectmeta.write(self.dir, meta)
        saved = json.loads((self.dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["future"], {"a": 1})
        self.assertEqual(saved["author"], "petrov")

    def test_unknown_fields_in_existing_file_kept_when_meta_is_fresh(self):
        data = {"format": 1, "name": "n", "folder": "f", "future": 7}
        (self.dir / "project.json").write_text(json.dumps(data), encoding="utf-8")
        projectmeta.write(self.dir, _sample())
        saved = json.loads((self.dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["future"], 7)

    def test_newer_format_read_with_warning(self):
        data = {"format": 2, "name": "n", "folder": "f", "novelty": True}
        (self.dir / "project.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertLogs("svnhelper.projectmeta", level="WARNING"):
            meta = projectmeta.read(self.dir)
        self.assertEqual(meta.name, "n")
        self.assertEqual(meta.extra, {"novelty": True})
        projectmeta.write(self.dir, meta)  # версия формата не понижается
        saved = json.loads((self.dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["format"], 2)

    def test_no_tmp_file_left(self):
        projectmeta.write(self.dir, _sample())
        self.assertEqual([p.name for p in self.dir.iterdir()], ["project.json"])

    def test_author_prefers_svn_login(self):
        self.assertEqual(projectmeta.default_author("ivanov"), "ivanov")

    def test_author_falls_back_to_windows_user(self):
        with mock.patch("svnhelper.projectmeta.getpass.getuser", return_value="winuser"):
            self.assertEqual(projectmeta.default_author(""), "winuser")
            self.assertEqual(projectmeta.default_author("  "), "winuser")

    def test_new_meta_fills_author_and_created(self):
        meta = projectmeta.new_meta("n", "f", login="ivanov", fields={"name": "n"})
        self.assertEqual(meta.author, "ivanov")
        self.assertRegex(meta.created, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertIsNone(meta.svn)


if __name__ == "__main__":
    unittest.main()
