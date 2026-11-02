"""Тесты слоёв без окна: ошибки, URL, конфиг, проверки, логика на фейковом svn."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svnhelper import checks, messages, server
from svnhelper.config import SettingsStore, load_config
from svnhelper.svn_core import (CancelToken, CheckoutProgress, ErrorKind, SvnClient,
                                SvnError, classify_error, decode_output)

FAKE = [sys.executable, str(Path(__file__).resolve().parent / "fake_svn.py")]


class ClassifyTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("svn: E215004: No more credentials\nAuthentication failed", False, ErrorKind.AUTH),
            ("svn: E170001: Authentication required for '<svn://h> realm'", False, ErrorKind.AUTH),
            ("svn: E170001: Authorization failed", False, ErrorKind.AUTHZ),
            ("svn: E170001: что-то на русском", True, ErrorKind.AUTHZ),
            ("svn: E170001: что-то на русском", False, ErrorKind.AUTH),
            ("svn: E170013: Unable to connect to a repository", False, ErrorKind.NO_CONNECTION),
            ("svn: Unable to connect to a repository", False, ErrorKind.NO_CONNECTION),
            ("svn: E170013: Unable...\nsvn: E730060: connection timed out", False, ErrorKind.TIMEOUT),
            ("svn: E170013: Unable...\nsvn: E210005: No repository found", False, ErrorKind.NOT_FOUND),
            ("svn: warning: W160013: non-existent\nsvn: E200009: Could not list", False, ErrorKind.NOT_FOUND),
            ("svn: E155004: working copy locked", False, ErrorKind.OTHER),
            ("svn: warning: W160042: Lock failed: newer version of '/f' exists\n"
             "svn: E200009: One or more locks could not be obtained", False, ErrorKind.OUT_OF_DATE),
            ("svn: E155011: File '/f' is out of date", False, ErrorKind.OUT_OF_DATE),
            ("svn: E160028: Out of date", False, ErrorKind.OUT_OF_DATE),
            ("svn: warning: W160035: already locked\nsvn: E200009: locks", False, ErrorKind.LOCKED),
            ("svn: E165001: Commit blocked by pre-commit hook\nA version (tag) is read-only.", False,
             ErrorKind.READ_ONLY_LINE),
            ("svn: E165001: Commit blocked\nВерсия (тег) только для чтения.", False, ErrorKind.READ_ONLY_LINE),
            ("svn: E165001: Commit blocked by pre-commit hook\nдругая причина", False, ErrorKind.OTHER),
            ("svn: E160037: Cannot verify lock on path; no matching lock-token available", False,
             ErrorKind.LOCKED),
            ("", False, ErrorKind.OTHER),
        ]
        for text, creds, expected in cases:
            with self.subTest(text=text, creds=creds):
                self.assertEqual(classify_error(text, creds), expected)

    def test_messages_cover_all_kinds(self):
        for kind in ErrorKind:
            self.assertTrue(messages.short_message(kind))


class LocalHistoryClassifyTests(unittest.TestCase):
    FILE_LOST = ("svn: E170013: Unable to connect to a repository at URL 'file:///C:/h/abc'\n"
                 "svn: E180001: Unable to open repository 'file:///C:/h/abc'")

    def test_missing_local_repository(self):
        self.assertEqual(classify_error(self.FILE_LOST), ErrorKind.LOCAL_HISTORY_MISSING)
        self.assertEqual(classify_error(self.FILE_LOST, had_credentials=True), ErrorKind.LOCAL_HISTORY_MISSING)

    def test_server_errors_are_unchanged(self):
        self.assertEqual(classify_error("svn: E170013: Unable to connect to a repository at URL 'svn://x'\n"
                                        "svn: E731001: Этот хост неизвестен."), ErrorKind.NO_CONNECTION)
        self.assertEqual(classify_error("svn: E180001: Unable to open repository 'svn://x'"), ErrorKind.OTHER)


class MessagesTests(unittest.TestCase):
    def test_redact(self):
        text = 'svn --username u --password hunter2 list\nPassword=abc123 password: "a b" --password="x y"'
        out = messages.redact(text)
        for secret in ("hunter2", "abc123", "a b", "x y"):
            self.assertNotIn(secret, out)
        self.assertIn("--password-from-stdin", messages.redact("--password-from-stdin"))

    def test_admin_report(self):
        raw = "\n".join(f"строка {i}" for i in range(100)) + "\nsvn: E170013: x --password secret"
        report = messages.admin_report("Обновление проекта", ErrorKind.NO_CONNECTION, raw,
                                       "C:\\log.txt", {"Проект": "p1"})
        self.assertNotIn("secret", report)
        for part in ("Обновление проекта", "no_connection", "E170013", "C:\\log.txt", "Проект: p1",
                     "строка 99", "Windows:"):
            self.assertIn(part, report)
        self.assertNotIn("строка 10\n", report)  # только последние ~30 строк

    def test_format_age(self):
        got = [messages.format_age(d) for d in (0, 1, 2, 3, 5, 7, 14, 21, 30, 70, 400)]
        self.assertEqual(got, ["сегодня", "вчера", "2 дня назад", "3 дня назад", "5 дней назад",
                               "неделю назад", "2 недели назад", "3 недели назад", "месяц назад",
                               "2 месяца назад", "год назад"])

    def test_status_line(self):
        from svnhelper.svn_core import LockInfo, WcStatus
        sl = messages.status_line
        self.assertEqual(sl("plain", None, False)[1], "plain")
        self.assertEqual(sl("broken", None, False), ("Папка повреждена", "bad", "Нажмите «Починить папку»"))
        self.assertEqual(sl("svn", None, False)[2], "Нажмите «⟳»")
        self.assertEqual(sl("svn", None, True)[2], "Идёт проверка…")
        self.assertEqual(sl("svn", WcStatus(conflicts=1, modified=2), True)[1:],
                         ("bad", "Нажмите «Разобрать конфликт»"))
        self.assertEqual(sl("svn", WcStatus(incoming=1, modified=1), True),
                         ("Есть новое на сервере и ваши правки", "new",
                          "Сначала «Получить новое», потом «Сохранить на сервер»"))
        self.assertEqual(sl("svn", WcStatus(incoming=1), True)[0], "Есть новое на сервере")
        self.assertEqual(sl("svn", WcStatus(modified=1), True)[0], "Есть несохранённые правки")
        self.assertEqual(sl("svn", WcStatus(), True), ("Актуален", "ok", "Ничего делать не нужно"))
        locked = WcStatus(my_locks=[LockInfo("a", "me"), LockInfo("b", "me")])
        self.assertTrue(sl("svn", locked, True)[2].endswith("Не забудьте освободить занятые файлы (2)"))


class LockInfoTests(unittest.TestCase):
    XML = """<?xml version="1.0"?><status><target path="."><entry path="a.bin"><wc-status item="normal" props="none">
    <lock><token>t</token><owner>ivanov</owner><comment>правлю</comment><created>2026-10-19T08:00:00.123456Z</created></lock>
    </wc-status></entry><entry path="b.bin"><wc-status item="normal" props="none"/><repos-status item="none" props="none">
    <lock><token>t2</token><owner>petrov</owner><created>2026-10-01T09:30:00.000000Z</created></lock></repos-status></entry>
    <entry path="c.bin"><wc-status item="normal" props="none"><lock><owner>x</owner><created>мусор</created></lock></wc-status></entry>
    <against revision="5"/></target></status>""".encode("utf-8")

    def test_parse_locks(self):
        from datetime import datetime, timezone
        from svnhelper.svn_core import parse_status_xml
        st = parse_status_xml(self.XML)
        self.assertEqual(st.locked_by_me, ["a.bin", "c.bin"])
        self.assertEqual(st.locked_by_others, [("b.bin", "petrov")])
        mine = st.my_locks[0]
        self.assertEqual((mine.path, mine.owner, mine.comment), ("a.bin", "ivanov", "правлю"))
        self.assertEqual(mine.created, datetime(2026, 10, 19, 8, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertIsNone(st.my_locks[1].created)
        self.assertEqual((st.other_locks[0].owner, st.other_locks[0].comment), ("petrov", ""))

    def test_age_days(self):
        from datetime import datetime, timedelta, timezone
        from svnhelper.svn_core import LockInfo
        now = datetime.now(timezone.utc)
        self.assertEqual(LockInfo("a", "o", now - timedelta(days=3, hours=1)).age_days, 3)
        self.assertEqual(LockInfo("a", "o", now + timedelta(hours=1)).age_days, 0)
        self.assertIsNone(LockInfo("a", "o").age_days)


class HelperTests(unittest.TestCase):
    def test_build_url_encodes_segments(self):
        url = server.build_url("svn://h/", ["test", "a b", "100%", "x@y", "папка"])
        self.assertEqual(url, "svn://h/test/a%20b/100%25/x%40y/%D0%BF%D0%B0%D0%BF%D0%BA%D0%B0")

    def test_decode_output(self):
        self.assertEqual(decode_output("Привет".encode("utf-8")), "Привет")
        self.assertIsInstance(decode_output("Привет".encode("cp1251")), str)  # не падает
        self.assertIsInstance(decode_output(bytes([0xff, 0xfe, 0x98])), str)

    def test_plural_and_size(self):
        self.assertEqual([messages.plural(n, ("файл", "файла", "файлов")) for n in (1, 2, 5, 11, 21, 112)],
                         ["файл", "файла", "файлов", "файлов", "файл", "файлов"])
        self.assertIn("МБ", messages.format_size(5 * 1024 * 1024))
        self.assertIn("ГБ", messages.format_size(5 * 1024 ** 3))

    def test_safe_local_name(self):
        self.assertEqual(checks.safe_local_name('a:b?c*'), "a_b_c_")
        self.assertEqual(checks.safe_local_name("name. "), "name")
        self.assertEqual(checks.safe_local_name("261019_отбраковщик и делитель_тест"),
                         "261019_отбраковщик и делитель_тест")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_utf8_bom(self):
        p = self.dir / "c.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"server": "svn://x/", "repos": ["a", "б"], "work_dir": "D:\\Проекты"}, ensure_ascii=False).encode())
        cfg = load_config(p)
        self.assertEqual((cfg.server, cfg.repos, cfg.work_dir), ("svn://x", ["a", "б"], "D:\\Проекты"))
        self.assertEqual(cfg.warnings, [])

    def test_broken_file_gives_defaults_and_warning(self):
        p = self.dir / "c.json"
        p.write_text("{ not json", encoding="utf-8")
        cfg = load_config(p)
        self.assertEqual(cfg.server, "svn://192.168.11.11")
        self.assertEqual(len(cfg.warnings), 1)

    def test_missing_file_warns(self):
        self.assertTrue(load_config(self.dir / "none.json").warnings)

    def test_bad_field_types(self):
        p = self.dir / "c.json"
        p.write_text(json.dumps({"server": 5, "repos": "x", "work_dir": ""}), encoding="utf-8")
        cfg = load_config(p)
        self.assertEqual(len(cfg.warnings), 3)
        self.assertTrue(cfg.repos)

    def test_settings_have_no_password(self):
        store = SettingsStore(self.dir / "s.json")
        store.settings.username = "ivanov"
        store.settings.last_dir = "C:\\П"
        store.save()
        data = json.loads((self.dir / "s.json").read_text(encoding="utf-8"))
        self.assertEqual(data["password_enc"], "")  # без явного «Запомнить» пароля в файле нет
        self.assertEqual(SettingsStore(self.dir / "s.json").settings.username, "ivanov")


class ParseUrlTests(unittest.TestCase):
    SERVER = "svn://192.168.11.11"

    def parse(self, text):
        return server.parse_project_url(text, self.SERVER)

    def test_variants_give_same_project(self):
        raw = "svn://192.168.11.11/test/projects/Russia/261019_отбраковщик и делитель_тест"
        enc = ("svn://192.168.11.11/test/projects/Russia/261019_%D0%BE%D1%82%D0%B1%D1%80%D0%B0%D0%BA%D0%BE%D0%B2"
               "%D1%89%D0%B8%D0%BA%20%D0%B8%20%D0%B4%D0%B5%D0%BB%D0%B8%D1%82%D0%B5%D0%BB%D1%8C_%D1%82%D0%B5%D1%81%D1%82")
        refs = [self.parse(t) for t in (raw, raw + "/", raw + "/trunk", raw + "/trunk/", f'  "{raw}"  ',
                                        "test/projects/Russia/261019_отбраковщик и делитель_тест", enc)]
        for ref in refs:
            self.assertEqual(ref, refs[0])
        self.assertEqual(refs[0].name, "261019_отбраковщик и делитель_тест")
        self.assertTrue(refs[0].trunk_url.startswith("svn://192.168.11.11/test/projects/Russia/261019_%D0%BE"))
        self.assertTrue(refs[0].trunk_url.endswith("/trunk"))
        self.assertIn("%20", refs[0].trunk_url)

    def test_errors(self):
        for bad in ("", "   ", "ftp://x/y", "svn://host", "svn://host/", "svn://host/trunk", "http:/oops"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.parse(bad)

    def test_file_url_keeps_drive_colon(self):
        ref = self.parse("file:///C:/repos/test/p/trunk")
        self.assertEqual(ref.trunk_url, "file:///C:/repos/test/p/trunk")


class LinesUrlTests(unittest.TestCase):
    SERVER = "svn://192.168.11.11"

    def parse(self, text):
        return server.parse_project_url(text, self.SERVER)

    def test_branch_and_tag_urls(self):
        base = "svn://192.168.11.11/test/проекты/мой проект"
        enc = ("svn://192.168.11.11/test/%D0%BF%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B/"
               "%D0%BC%D0%BE%D0%B9%20%D0%BF%D1%80%D0%BE%D0%B5%D0%BA%D1%82")
        enc_name = "%D0%9D%D0%BE%D0%B2%D0%B0%D1%8F%20%D0%B2%D0%B5%D1%82%D0%BA%D0%B0"
        for text in (base + "/branches/Новая ветка", base + "/branches/Новая ветка/",
                     enc + "/branches/" + enc_name, base + "/branches/Новая ветка/docs/sub"):
            with self.subTest(text=text):
                ref = self.parse(text)
                self.assertEqual(ref.segments, ("test", "проекты", "мой проект"))
                self.assertEqual(ref.line, server.Line("branch", "Новая ветка"))
        ref = self.parse(base + "/tags/2026-10-19/a/b")
        self.assertEqual(ref.line, server.Line("tag", "2026-10-19"))
        self.assertEqual(ref.line_url(), enc + "/tags/2026-10-19")
        self.assertEqual(ref.line_url(server.TRUNK_LINE), ref.trunk_url)
        self.assertEqual(ref.line_url(server.Line("branch", "б@1")), enc + "/branches/%D0%B1%401")

    def test_old_behaviour_is_kept(self):
        ref = self.parse("svn://h/a/b/trunk/sub/dir")
        self.assertEqual((ref.segments, ref.line), (("a", "b"), server.TRUNK_LINE))
        self.assertEqual(server.ProjectRef("svn://h", ("a", "b")).line, server.TRUNK_LINE)
        self.assertEqual(server.ProjectRef("svn://h", ("a", "b")).trunk_url, "svn://h/a/b/trunk")

    def test_branches_without_name_is_project(self):
        for text in ("svn://h/a/b/branches", "svn://h/a/b/tags/"):
            ref = self.parse(text)
            self.assertEqual((ref.segments, ref.line), (("a", "b"), server.TRUNK_LINE))
        with self.assertRaises(ValueError):
            self.parse("svn://h/branches/x")  # проекта перед branches нет

    def test_line_from_url(self):
        self.assertEqual(server.line_from_url("svn://h/test/p/trunk"), (("test", "p"), server.TRUNK_LINE))
        self.assertEqual(server.line_from_url("svn://h/test/%D0%BF/branches/%D0%B2%20%D1%8B/x"),
                         (("test", "п"), server.Line("branch", "в ы")))
        self.assertEqual(server.line_from_url("svn://h/test/п/tags/v1"), (("test", "п"), server.Line("tag", "v1")))
        self.assertIsNone(server.line_from_url("svn://h/test/p"))
        self.assertIsNone(server.line_from_url("мусор"))
        # корень репозитория с именем «tags» не принимается за линию
        self.assertEqual(server.line_from_url("svn://h/tags/p/trunk", "svn://h/tags"),
                         (("tags", "p"), server.TRUNK_LINE))
        self.assertIsNone(server.line_from_url("svn://h/tags/p", "svn://h/tags"))

    def test_rightmost_marker_rule(self):
        T, B = server.TRUNK_LINE, lambda n: server.Line("branch", n)
        V = lambda n: server.Line("tag", n)
        cases = [
            ("g/tags/proj/trunk", ("g", "tags", "proj"), T),
            ("g/tags/proj/trunk/sub", ("g", "tags", "proj"), T),
            ("g/tags/proj", ("g",), V("proj")),          # неустранимая неоднозначность: читается как версия
            ("p/tags/v1", ("p",), V("v1")),
            ("p/tags/v1/docs", ("p",), V("v1")),
            ("p/branches/b", ("p",), B("b")),
            ("p/trunk/docs", ("p",), T),
            ("p/trunk/tags/x", ("p",), T),               # tags внутри trunk - обычная папка
            ("p/branches/b/docs/tags", ("p",), B("b")),
        ]
        for tail, segs, line in cases:
            for prefix in ("svn://h/test/", "svn://h/"):
                with self.subTest(url=prefix + tail):
                    ref = self.parse(prefix + tail)
                    base = tuple(prefix[len("svn://h/"):].split("/")[:-1]) if prefix.endswith("test/") else ()
                    self.assertEqual((ref.segments, ref.line), (base + segs, line))
        # то же для адреса рабочей копии, с корнем репозитория и без
        self.assertEqual(server.line_from_url("svn://h/r/g/tags/proj/trunk", "svn://h/r"),
                         (("r", "g", "tags", "proj"), T))
        self.assertEqual(server.line_from_url("svn://h/g/tags/proj/trunk/docs"), (("g", "tags", "proj"), T))
        self.assertEqual(server.line_from_url("svn://h/p/tags/v1/docs"), (("p",), V("v1")))
        self.assertEqual(server.line_from_url("svn://h/p/trunk/docs"), (("p",), T))
        self.assertIsNone(server.line_from_url("svn://h/tags/v1", "svn://h"))   # слева от маркера проекта нет

    def test_project_from_segments(self):
        ref = server.project_from_segments("svn://h", ["g", "tags", "proj"])
        self.assertEqual((ref.segments, ref.line, ref.trunk_url), (("g", "tags", "proj"), server.TRUNK_LINE,
                                                                   "svn://h/g/tags/proj/trunk"))
        with self.assertRaises(ValueError):
            server.project_from_segments("svn://h", [])

    def test_line_label_and_flags(self):
        self.assertEqual(server.TRUNK_LINE.label, "Основная линия")
        self.assertEqual(server.Line("branch", "x").label, "Ветка «x»")
        self.assertEqual(server.Line("tag", "y").label, "Версия «y»")
        self.assertEqual([server.TRUNK_LINE.read_only, server.Line("branch", "x").read_only,
                          server.Line("tag", "y").read_only], [False, False, True])
        self.assertEqual(server.Line("tag", "y").segments(), ["tags", "y"])

    def test_validate_line_name(self):
        self.assertEqual(server.validate_line_name("  Новая ветка 2  "), "Новая ветка 2")
        self.assertEqual(server.validate_line_name("v1.0"), "v1.0")
        self.assertEqual(server.validate_line_name("x" * 80), "x" * 80)
        for bad in ("", "   ", "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "a\tb", "a\x00b",
                    ".", "..", ".hidden", "name.", "trunk", "Branches", "tags", "x" * 81):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as cm:
                    server.validate_line_name(bad)
                self.assertTrue(str(cm.exception))

    def test_suggest_tag_name(self):
        from datetime import date
        today = date(2026, 10, 19)
        self.assertEqual(server.suggest_tag_name(today, []), "2026-10-19")
        self.assertEqual(server.suggest_tag_name(today, ["2026-10-19"]), "2026-10-19_2")
        self.assertEqual(server.suggest_tag_name(today, ["2026-10-19", "2026-10-19_2", "x"]), "2026-10-19_3")
        self.assertEqual(server.suggest_tag_name(today, ["2026-10-18"]), "2026-10-19")


LIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<lists><list path="file:///r/p/branches">
<entry kind="dir"><name>старая</name><commit revision="5"><author>ivanov</author><date>2026-10-01T08:00:00.123456Z</date></commit></entry>
<entry kind="file"><name>a b.txt</name><size>12</size><commit revision="7"><author>petrov</author><date>2026-10-02T09:30:00.000000Z</date></commit>
<lock><token>opaquelocktoken:1</token><owner>petrov</owner><comment>правлю</comment><created>2026-10-03T10:00:00.000000Z</created></lock></entry>
<entry kind="dir"><name>без коммита</name></entry>
</list></lists>""".encode("utf-8")

LOG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="12"><author>petrov</author><date>2026-10-10T10:00:00.000000Z</date>
<paths><path action="M" kind="file">/p/branches/x/a.txt</path></paths><msg>правка</msg></logentry>
<logentry revision="11"><author>ivanov</author><date>2026-10-09T10:00:00.000000Z</date>
<paths><path action="M" kind="file">/p/branches/x/b.txt</path></paths><msg>ещё</msg></logentry>
<logentry revision="10"><author>petrov</author><date>2026-10-08T10:00:00.000000Z</date>
<paths><path action="M" kind="file">/p/branches/x/c.txt</path></paths><msg>раз</msg></logentry>
<logentry revision="9"><author>ivanov</author><date>2026-10-07T10:00:00.000000Z</date>
<paths><path action="A" kind="dir">/p/branches</path>
<path action="A" kind="dir" copyfrom-path="/p/trunk" copyfrom-rev="345">/p/branches/x</path></paths>
<msg>Ветка для задачи
вторая строка</msg></logentry>
</log>""".encode("utf-8")


class BranchParsersTests(unittest.TestCase):
    def test_parse_list_xml_with_commit_and_lock(self):
        from svnhelper.svn_core import parse_list_xml, parse_locks_xml
        entries = parse_list_xml(LIST_XML)
        self.assertEqual([e.name for e in entries], ["старая", "a b.txt", "без коммита"])
        first = entries[0]
        self.assertEqual((first.author, first.revision, first.date.year, first.date.utcoffset().total_seconds()),
                         ("ivanov", 5, 2026, 0))
        self.assertIsNone(first.lock)
        self.assertEqual((entries[2].author, entries[2].revision, entries[2].date), ("", None, None))
        self.assertEqual(entries[1].size, 12)
        locks = parse_locks_xml(LIST_XML)
        self.assertEqual([(l.path, l.owner, l.comment, l.created.day) for l in locks],
                         [("a b.txt", "petrov", "правлю", 3)])

    def test_parse_log_xml_and_history_from_trunk_copy(self):
        from svnhelper.svn_core import build_branch_history, parse_log_xml
        entries = parse_log_xml(LOG_XML)
        self.assertEqual([e.revision for e in entries], [12, 11, 10, 9])
        self.assertEqual(entries[3].paths[1].copyfrom_rev, 345)
        hist = build_branch_history(entries)
        self.assertEqual((hist.created_by, hist.created_revision, hist.copied_from, hist.copied_from_revision),
                         ("ivanov", 9, "/p/trunk", 345))
        self.assertEqual(hist.comment, "Ветка для задачи\nвторая строка")
        self.assertEqual((hist.last_revision, hist.last_date.day), (12, 10))
        self.assertEqual([(a.name, a.commits, a.last_date.day) for a in hist.authors],
                         [("petrov", 2, 10), ("ivanov", 2, 9)])
        self.assertEqual(hist.from_label, "из основной линии (версия 345)")

    def test_history_of_trunk_without_copyfrom(self):
        from svnhelper.svn_core import build_branch_history, parse_log_xml
        xml = ('<log><logentry revision="2"><author>b</author><date>2026-01-02T00:00:00Z</date><msg>m</msg></logentry>'
               '<logentry revision="1"><author>a</author><date>2026-01-01T00:00:00Z</date>'
               '<paths><path action="A" kind="dir">/p/trunk</path></paths><msg>init</msg></logentry></log>')
        hist = build_branch_history(parse_log_xml(xml.encode()))
        self.assertEqual((hist.created_by, hist.copied_from, hist.copied_from_revision, hist.from_label),
                         ("a", "", None, ""))
        self.assertEqual(build_branch_history([]).authors, [])

    def test_from_label(self):
        from svnhelper.svn_core import BranchHistory
        cases = [("/p/trunk", 5, "из основной линии (версия 5)"), ("/p/trunk", None, "из основной линии"),
                 ("/a/p/branches/мой/", 7, "из ветки «мой»"), ("/p/tags/v1", 7, "из версии «v1»"), ("", None, "")]
        for path, rev, label in cases:
            with self.subTest(path=path):
                self.assertEqual(BranchHistory(copied_from=path, copied_from_revision=rev).from_label, label)

    def test_new_error_kinds(self):
        self.assertEqual(classify_error("svn: E160020: Path already exists"), ErrorKind.EXISTS)
        self.assertEqual(classify_error("svn: E195012: shares no common ancestry"), ErrorKind.UNRELATED)
        self.assertEqual(messages.short_message(ErrorKind.EXISTS), "Такая ветка или версия уже есть на сервере.")

    def test_tortoise_merge_command(self):
        from svnhelper.tortoise import build_args
        self.assertEqual(build_args("T.exe", "merge", ["D:\\w"])[1], "/command:merge")
        for removed in ("switch", "copy"):
            with self.assertRaises(ValueError):
                build_args("T.exe", removed, ["D:\\w"])

    def test_busy_error_kind(self):
        text = "svn: E155009: Failed to run the WC DB work queue\nsvn: E720005: Can't move 'a' to 'b'"
        self.assertEqual(classify_error(text), ErrorKind.BUSY)
        # занятость не перебивает «файл занят другим пользователем» и «устарел»
        self.assertEqual(classify_error("E160035 E720005"), ErrorKind.LOCKED)
        self.assertEqual(classify_error("W160042 E720032"), ErrorKind.OUT_OF_DATE)
        self.assertEqual(classify_error("E720032"), ErrorKind.BUSY)
        self.assertEqual(classify_error("E720033"), ErrorKind.BUSY)
        self.assertIn("Компас", messages.short_message(ErrorKind.BUSY))

    def test_status_revisions_and_switched(self):
        from svnhelper.svn_core import parse_status_xml
        xml = """<?xml version="1.0"?><status><target path="w">
<entry path="w"><wc-status item="normal" revision="5" props="none"><commit revision="5"/></wc-status></entry>
<entry path="w/a.txt"><wc-status item="modified" revision="9" props="none"><commit revision="7"/></wc-status></entry>
<entry path="w/b"><wc-status item="normal" revision="7" switched="true" props="none"/></entry>
<entry path="w/new"><wc-status item="added" props="none"/></entry>
<entry path="w/junk"><wc-status item="unversioned" props="none"/></entry>
</target></status>""".encode("utf-8")
        st = parse_status_xml(xml)
        self.assertEqual((st.min_revision, st.max_revision, st.mixed, st.switched), (5, 9, True, 1))
        self.assertEqual((st.modified, st.unversioned), (2, 1))
        same = parse_status_xml(b'<status><target path="w"><entry path="w"><wc-status item="normal" revision="4"/>'
                                b'</entry></target></status>')
        self.assertEqual((same.min_revision, same.max_revision, same.mixed, same.switched), (4, 4, False, 0))
        empty = parse_status_xml(b'<status><target path="w"/></status>')
        self.assertEqual((empty.min_revision, empty.max_revision, empty.mixed), (None, None, False))


class AuditTests(unittest.TestCase):
    def entries(self):
        from svnhelper.svn_core import ListEntry
        return [
            ListEntry("dir", "backup"), ListEntry("file", "backup/old.txt", 10),
            ListEntry("file", "Thumbs.DB", 5), ListEntry("file", "~$doc.docx", 5),
            ListEntry("file", "a.BAK", 7), ListEntry("file", "ok.txt", 100),
            ListEntry("file", "docs/Archive/x.txt", 3), ListEntry("file", "big.bin", 60 * 1024 * 1024),
            ListEntry("file", "d/" + "x" * 80 + ".txt", 1),
        ]

    def test_default_rules(self):
        from svnhelper.audit import analyze
        from svnhelper.config import FilterRules
        res = analyze(self.entries(), FilterRules(junk_dirs=["backup"], junk_globs=["docs/archive/*"]),
                      "C:\\" + "p" * 150)
        self.assertEqual(res.junk_paths, ["a.BAK", "backup", "docs/Archive/x.txt", "Thumbs.DB", "~$doc.docx"])
        self.assertEqual(res.junk_files, 5)  # old.txt внутри мусорной папки тоже посчитан
        self.assertEqual([p for p, _ in res.big_files], ["big.bin"])
        self.assertEqual(len(res.long), 1)
        self.assertTrue(res.needs_attention)
        self.assertIn("НЕ попадут", "\n".join(res.report_lines()))

    def test_too_long_and_clean(self):
        from svnhelper.audit import analyze
        from svnhelper.config import FilterRules
        res = analyze(self.entries(), FilterRules(), "C:\\" + "p" * 200)
        self.assertEqual(len(res.too_long), 1)
        clean = analyze([], FilterRules(), "C:\\p")
        self.assertFalse(clean.needs_attention)
        self.assertEqual(clean.report_lines(), [])


class FilterConfigTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.json"
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return load_config(p)

    def test_defaults_without_section(self):
        cfg = self.load({})
        self.assertTrue(cfg.filter.enabled)
        self.assertIn(".bak", cfg.filter.junk_extensions)

    def test_custom_rules_normalized(self):
        cfg = self.load({"username": " ivanov ", "password": " pw ", "filter": {
            "junk_extensions": ["BAK", ".Tmp"], "junk_dirs": ["Backup/"], "junk_globs": ["Docs\\Old\\*"],
            "skip_junk": False, "big_file_mb": 10}})
        self.assertEqual(cfg.filter.junk_extensions, [".bak", ".tmp"])
        self.assertEqual(cfg.filter.junk_dirs, ["backup"])
        self.assertEqual(cfg.filter.junk_globs, ["docs/old/*"])
        self.assertFalse(cfg.filter.skip_junk)
        self.assertEqual((cfg.username, cfg.password), ("ivanov", " pw "))
        self.assertEqual(cfg.warnings, [])

    def test_bad_values_warn(self):
        cfg = self.load({"password": 5, "filter": {"skip_junk": "yes", "big_file_mb": -1, "junk_names": "x"}})
        self.assertEqual(len(cfg.warnings), 4)
        self.assertTrue(cfg.filter.skip_junk)
        self.assertEqual(cfg.password, "")

    def test_shipped_example_is_valid(self):
        cfg = load_config(Path(__file__).resolve().parent.parent / "svn_helper.json")
        self.assertEqual(cfg.warnings, [])
        self.assertEqual((cfg.support, cfg.latest_version, cfg.update_path, cfg.help_path), ("", "", "", ""))

    def test_new_text_fields(self):
        from svnhelper.config import Config, apply_config_data
        os.environ["SVN_TEST_SRV"] = "\\\\srv"
        try:
            cfg = Config()
            apply_config_data(cfg, {"support": " Иванов И., доб. 123 ", "latest_version": "1.1.0",
                                    "update_path": "%SVN_TEST_SRV%\\new", "help_path": 5})
        finally:
            del os.environ["SVN_TEST_SRV"]
        self.assertEqual((cfg.support, cfg.latest_version, cfg.update_path, cfg.help_path),
                         ("Иванов И., доб. 123", "1.1.0", "\\\\srv\\new", ""))
        self.assertEqual(len(cfg.warnings), 1)
        self.assertIn("help_path", cfg.warnings[0])

    def test_tortoise_installer_path_field(self):
        from svnhelper.config import Config, apply_config_data
        cfg = Config()
        self.assertEqual(cfg.tortoise_installer_path, "")
        apply_config_data(cfg, {"tortoise_installer_path": " \\srv\common\TortoiseSVN.msi "}, shared=True)
        self.assertEqual(cfg.tortoise_installer_path, "\\srv\common\TortoiseSVN.msi")

    def test_installer_exit_codes(self):
        from svnhelper import installer
        self.assertTrue(installer.result_for_exit_code(0).ok)
        reboot = installer.result_for_exit_code(3010)
        self.assertTrue(reboot.ok and reboot.reboot)
        self.assertFalse(installer.result_for_exit_code(1602).ok)
        self.assertFalse(installer.result_for_exit_code(1603).ok)
        self.assertIn("ADDLOCAL=ALL", installer.msiexec_params("C:\t\a.msi"))

    def test_install_missing_file_reports_error(self):
        from svnhelper import installer
        with mock.patch.object(installer, "IS_WINDOWS", True):
            result = installer.install_tortoise(os.path.join(tempfile.gettempdir(), "no_such_dir_xyz", "a.msi"))
        self.assertFalse(result.ok)
        self.assertIn("не найден", result.message)

    def test_is_newer_version(self):
        from svnhelper.config import is_newer_version
        cases = [("1.2.10", "1.2.9", True), ("1.2.9", "1.2.10", False), ("1.1.0", "1.1.0", False),
                 ("1.1", "1.0.9", True), ("1.1", "1.1.0", False), ("v2.0", "1.9.9", True),
                 ("", "1.0.0", False), ("abc", "1.0.0", False), ("1.0.0", "x", False),
                 ("1.0.0-beta", "1.0.0", False), (None, "1.0.0", False)]
        for latest, current, expected in cases:
            with self.subTest(latest=latest, current=current):
                self.assertEqual(is_newer_version(latest, current), expected)


class SharedConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(self.dir / "appdata")

    def tearDown(self):
        if self._appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._appdata
        self.tmp.cleanup()

    def test_fetch_from_file_with_bom_and_errors(self):
        from svnhelper.config import SharedConfigError, fetch_shared
        p = self.dir / "shared.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"server": "svn://s"}).encode())
        self.assertEqual(fetch_shared(str(p), None), {"server": "svn://s"})
        with self.assertRaises(SharedConfigError):
            fetch_shared(str(self.dir / "none.json"), None)
        p.write_text("{oops", encoding="utf-8")
        with self.assertRaises(SharedConfigError):
            fetch_shared(str(p), None)

    def test_fetch_by_svn_url_uses_cat(self):
        from svnhelper.config import fetch_shared
        calls = []

        def cat(url):
            calls.append(url)
            return b'{"work_dir": "D:\\\\P"}'
        self.assertEqual(fetch_shared("svn://h/common/svn_helper.json", cat), {"work_dir": "D:\\P"})
        self.assertEqual(calls, ["svn://h/common/svn_helper.json"])

    def test_cache_roundtrip(self):
        from svnhelper.config import load_shared_cache, save_shared_cache
        self.assertIsNone(load_shared_cache())
        save_shared_cache({"server": "svn://x"})
        self.assertEqual(load_shared_cache(), {"server": "svn://x"})

    def test_shared_config_cannot_set_password(self):
        from svnhelper.config import (Config, Settings, apply_config_data, build_config,
                                      load_shared_cache, save_shared_cache)
        cfg = Config(password="local")
        with self.assertLogs("svnhelper", "WARNING"):
            apply_config_data(cfg, {"password": "shared", "username": "u"}, shared=True)
        self.assertEqual((cfg.password, cfg.username, cfg.warnings), ("local", "u", []))
        apply_config_data(cfg, {"password": "own"})                    # локальный файл — можно
        self.assertEqual(cfg.password, "own")
        local = self.dir / "local.json"
        local.write_text("{}", encoding="utf-8")
        with self.assertLogs("svnhelper", "WARNING"):
            self.assertEqual(build_config(Settings(), {"password": "x"}, local).password, "")
        save_shared_cache({"server": "svn://s", "password": "x"})
        self.assertEqual(load_shared_cache(), {"server": "svn://s"})

    def test_tree_cache(self):
        from svnhelper.config import load_tree_cache, save_tree_cache, tree_cache_path
        self.assertEqual(load_tree_cache(), {})
        data = {"server": "svn://s", "folders": {"repo/а б": {"dirs": [["trunk", True]], "ts": 1.5}}}
        save_tree_cache(data)
        self.assertEqual(load_tree_cache(), data)
        tree_cache_path().write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(load_tree_cache(), {})
        tree_cache_path().write_text("{мусор", encoding="utf-8")
        self.assertEqual(load_tree_cache(), {})

    def test_recent_favorites_roundtrip_and_garbage(self):
        from svnhelper.config import Settings, SettingsStore, add_recent
        st = Settings()
        for i in range(12):
            add_recent(st, f"svn://s/p{i}", f"p{i}", f"D:\\p{i}")
        add_recent(st, "svn://s/p5", "p5", "D:\\new")
        self.assertEqual(len(st.recent), 10)
        self.assertEqual(st.recent[0], {"url": "svn://s/p5", "name": "p5", "dest": "D:\\new"})
        self.assertEqual([r["url"] for r in st.recent].count("svn://s/p5"), 1)
        store = SettingsStore(self.dir / "s.json")
        store.settings = st
        st.favorites = ["svn://s/p1"]
        st.remember_password = False
        store.save()
        loaded = SettingsStore(self.dir / "s.json").settings
        self.assertEqual((loaded.recent, loaded.favorites, loaded.remember_password),
                         (st.recent, ["svn://s/p1"], False))
        (self.dir / "bad.json").write_text(json.dumps({
            "recent": [5, {"url": 1}, {"url": "svn://ok", "name": 7}, {"url": "svn://ok"}, "x"],
            "favorites": [1, "svn://f", "svn://f", None], "remember_password": "yes"}), encoding="utf-8")
        bad = SettingsStore(self.dir / "bad.json").settings
        self.assertEqual(bad.recent, [{"url": "svn://ok", "name": "", "dest": ""}])
        self.assertEqual((bad.favorites, bad.remember_password), (["svn://f"], None))
        (self.dir / "bad2.json").write_text(json.dumps({"recent": "text", "favorites": 3}), encoding="utf-8")
        bad2 = SettingsStore(self.dir / "bad2.json").settings
        self.assertEqual((bad2.recent, bad2.favorites), ([], []))

    def test_project_dirs_and_columns_settings(self):
        from svnhelper.config import add_project_dir, same_dir
        # старые настройки: одна папка last_dir - переносится в список папок
        (self.dir / "old.json").write_text(json.dumps({"last_dir": "D:\\Work"}), encoding="utf-8")
        old = SettingsStore(self.dir / "old.json").settings
        self.assertEqual((old.project_dirs, old.last_dir), (["D:\\Work"], "D:\\Work"))
        self.assertEqual((old.mine_col2_collapsed, old.mine_col3_collapsed), (False, False))
        # список без повторов (регистр и косые черты не важны), мусор отброшен, текущая добавлена в конец
        (self.dir / "dirs.json").write_text(json.dumps({
            "project_dirs": ["D:\\A", "d:/a", 5, "", None, "E:\\B"], "last_dir": "F:\\C",
            "mine_col2_collapsed": True, "mine_col3_collapsed": "да"}), encoding="utf-8")
        st = SettingsStore(self.dir / "dirs.json").settings
        self.assertEqual(st.project_dirs, ["D:\\A", "E:\\B", "F:\\C"])
        self.assertEqual((st.mine_col2_collapsed, st.mine_col3_collapsed), (True, False))
        self.assertTrue(same_dir("D:\\A\\", "d:/a"))
        self.assertFalse(same_dir("", ""))
        self.assertEqual(add_project_dir(["D:\\A"], "  "), ["D:\\A"])
        self.assertEqual(add_project_dir(["D:\\A"], "e:/b/"), ["D:\\A", "e:\\b"])

    def test_layering_order(self):
        from svnhelper.config import Settings, build_config
        local = self.dir / "local.json"
        local.write_text(json.dumps({"server": "svn://local", "work_dir": "D:\\L", "filter": {"big_file_mb": 5}}),
                         encoding="utf-8")
        shared = {"server": "svn://shared", "repos": ["r1"], "filter": {"skip_junk": False}}
        cfg = build_config(Settings(), shared, local)
        self.assertEqual((cfg.server, cfg.work_dir, cfg.repos), ("svn://shared", "D:\\L", ["r1"]))
        self.assertEqual((cfg.filter.big_file_mb, cfg.filter.skip_junk), (5, False))
        mine = Settings(server="svn://mine", skip_junk=True)
        cfg = build_config(mine, shared, local)
        self.assertEqual((cfg.server, cfg.filter.skip_junk, cfg.filter.big_file_mb), ("svn://mine", True, 5))
        self.assertEqual(build_config(Settings(), None, self.dir / "missing.json").warnings, [])

    @unittest.skipUnless(sys.platform == "win32", "DPAPI только в Windows")
    def test_password_encryption_and_credentials(self):
        from svnhelper.config import Config, Settings, effective_credentials, protect_text, unprotect_text
        token = protect_text("пароль 123")
        self.assertNotIn("123", token)
        self.assertEqual(unprotect_text(token), "пароль 123")
        self.assertEqual(unprotect_text("мусор"), "")
        cfg = Config(username="cfg_user", password="cfg_pw")
        self.assertEqual(effective_credentials(Settings(), cfg), ("cfg_user", "cfg_pw"))
        self.assertEqual(effective_credentials(Settings(username="me", password_enc=token), cfg), ("me", "пароль 123"))
        self.assertIsNone(effective_credentials(Settings(username="me"), Config()))


class TortoiseTests(unittest.TestCase):
    def test_build_args(self):
        from svnhelper.tortoise import build_args
        exe = "C:\\TortoiseSVN\\bin\\TortoiseProc.exe"
        self.assertEqual(build_args(exe, "commit", ["D:\\Мои проекты\\копия@2"]),
                         [exe, "/command:commit", "/path:D:\\Мои проекты\\копия@2"])
        self.assertEqual(build_args(exe, "update", ["D:\\a b", "D:\\ц"], closeonend=0, logmsg="Правка")[1:],
                         ["/command:update", "/path:D:\\a b*D:\\ц", "/closeonend:0", "/logmsg:Правка"])
        for cmd in ("log", "cleanup", "repostatus", "lock", "unlock", "diff", "resolve"):
            self.assertEqual(build_args(exe, cmd, ["D:\\x"])[1], f"/command:{cmd}")
        with self.assertRaises(ValueError):
            build_args(exe, "format", ["D:\\x"])
        with self.assertRaises(ValueError):
            build_args(exe, "commit", [])

    def test_command_line_quotes_values(self):
        from svnhelper.tortoise import build_args, command_line
        args = build_args("C:\\T\\TortoiseProc.exe", "commit", ["D:\\Мои проекты\\копия@2", "D:\\x"],
                          logmsg='Правка "А"')
        self.assertEqual(command_line(args),
                         '"C:\\T\\TortoiseProc.exe" /command:commit '
                         '/path:"D:\\Мои проекты\\копия@2*D:\\x" /logmsg:"Правка \'А\'"')

    def test_resolve_file_opens_conflict_editor(self):
        from svnhelper.tortoise import build_args
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, "чертёж 1.txt")
            f.write_text("x")
            self.assertEqual(build_args("T.exe", "resolve", [str(f)])[1], "/command:conflicteditor")
            self.assertEqual(build_args("T.exe", "resolve", [tmp])[1], "/command:resolve")

    def test_find_tortoiseproc(self):
        from unittest import mock
        from svnhelper.tortoise import find_tortoiseproc
        with tempfile.TemporaryDirectory() as tmp:
            beside = Path(tmp, "bin")
            beside.mkdir()
            Path(beside, "TortoiseProc.exe").write_text("")
            no_which = mock.patch("shutil.which", return_value=None)
            with no_which:
                self.assertEqual(find_tortoiseproc(str(beside / "svn.exe")), str(beside / "TortoiseProc.exe"))
                pf = Path(tmp, "pf", "TortoiseSVN", "bin")
                pf.mkdir(parents=True)
                Path(pf, "TortoiseProc.exe").write_text("")
                with mock.patch.dict(os.environ, {"ProgramFiles": str(Path(tmp, "pf")), "ProgramW6432": ""}):
                    self.assertEqual(find_tortoiseproc(), str(pf / "TortoiseProc.exe"))
                with mock.patch.dict(os.environ, {"ProgramFiles": str(Path(tmp, "none")), "ProgramW6432": ""}):
                    self.assertIsNone(find_tortoiseproc())
            with mock.patch("shutil.which", return_value="X:\\p\\TortoiseProc.exe"):
                self.assertEqual(find_tortoiseproc(), "X:\\p\\TortoiseProc.exe")


class OverridesTests(unittest.TestCase):
    def test_overrides_win_over_config(self):
        from svnhelper.config import Config, Settings, apply_overrides
        cfg = Config()
        apply_overrides(cfg, Settings(server="svn://other/", repos=["a"], svn_path="X:\svn.exe"))
        self.assertEqual((cfg.server, cfg.repos, cfg.svn_path), ("svn://other", ["a"], "X:\svn.exe"))
        cfg2 = Config()
        apply_overrides(cfg2, Settings())
        self.assertEqual(cfg2.server, Config().server)


class TargetCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_path(self):
        self.assertIn("полный путь", checks.check_target("проекты", "p").error)

    def test_unc_path(self):
        self.assertIn("сетевом", checks.check_target("\\\\srv\\share\\x", "p").error)

    def test_empty_ok_and_missing_base_ok(self):
        self.assertIsNone(checks.check_target(self.base, "p").error)
        os.mkdir(os.path.join(self.base, "p"))
        res = checks.check_target(self.base, "p")
        self.assertIsNone(res.error)
        self.assertEqual(res.dest, os.path.join(os.path.normpath(self.base), "p"))

    def test_nonempty(self):
        d = os.path.join(self.base, "p")
        os.mkdir(d)
        Path(d, "f.txt").write_text("x")
        self.assertIn("не пуста", checks.check_target(self.base, "p").error)

    def test_existing_working_copy(self):
        d = os.path.join(self.base, "p")
        os.makedirs(os.path.join(d, ".svn"))
        err = checks.check_target(self.base, "p").error
        self.assertIn("Получить новое", err)

    def test_long_path_warns_but_allows(self):
        res = checks.check_target(self.base, "x" * 120)
        self.assertIsNone(res.error)
        self.assertIn("260", res.warning)

    def test_free_space(self):
        self.assertIsNone(checks.check_free_space(self.base, 1024))
        self.assertIn("не хватает", checks.check_free_space(self.base, 10 ** 18))

    def test_network_drive_by_type(self):
        orig = checks.drive_type
        checks.drive_type = lambda p: checks.DRIVE_REMOTE
        try:
            self.assertIn("сетевом", checks.check_target(self.base, "p").error)
        finally:
            checks.drive_type = orig


class FakeSvnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "calls.jsonl")
        os.environ["FAKE_SVN_LOG"] = self.log
        os.environ.pop("FAKE_SVN_VERSION", None)
        self.client = SvnClient(FAKE)

    def tearDown(self):
        for k in ("FAKE_SVN_MODE", "FAKE_SVN_LOG", "FAKE_SVN_VERSION"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def mode(self, name):
        os.environ["FAKE_SVN_MODE"] = name

    def calls(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_list_and_estimate(self):
        self.mode("ok")
        self.assertEqual(self.client.list_dirs("svn://x/p"), ["trunk", "tags"])
        est = self.client.estimate("svn://x/p/trunk")
        self.assertEqual((est.files, est.dirs, est.size, est.entries), (3, 1, 6000, 4))
        self.assertIn("--non-interactive", self.calls()[0]["args"])

    def test_checkout_progress_and_revision(self):
        self.mode("ok")
        progress = CheckoutProgress()
        res = self.client.checkout("svn://x/p/trunk", os.path.join(self.tmp.name, "p"), progress)
        self.assertEqual(progress.count, 4)
        self.assertEqual(res.revision, 42)
        self.assertIn("c.bin", progress.last_path)

    def test_auth_error_then_success_with_stdin_password(self):
        self.mode("auth")
        with self.assertRaises(SvnError) as cm:
            self.client.list_dirs("svn://x/p")
        self.assertEqual(cm.exception.kind, ErrorKind.AUTH)
        gen = self.client.cred_generation
        self.client.set_credentials("ivanov", "good")
        self.assertEqual(self.client.cred_generation, gen + 1)
        self.assertEqual(self.client.list_dirs("svn://x/p"), ["trunk", "tags"])
        last = self.calls()[-1]
        self.assertIn("--password-from-stdin", last["args"])
        self.assertNotIn("good", last["args"])
        self.assertEqual(last["password"], "good")

    def test_wrong_password_is_auth_not_authz(self):
        self.mode("auth")
        self.client.set_credentials("ivanov", "bad")
        with self.assertRaises(SvnError) as cm:
            self.client.list_dirs("svn://x/p")
        self.assertEqual(cm.exception.kind, ErrorKind.AUTH)

    def test_old_svn_uses_password_option(self):
        self.mode("ok")
        os.environ["FAKE_SVN_VERSION"] = "1.9.7"
        client = SvnClient(FAKE)
        client.set_credentials("ivanov", "secret")
        client.list_dirs("svn://x/p")
        args = self.calls()[-1]["args"]
        self.assertIn("--password", args)
        self.assertNotIn("--password-from-stdin", args)

    def test_unknown_version_is_treated_as_modern(self):
        self.mode("ok")
        os.environ["FAKE_SVN_VERSION"] = "что-то странное"
        client = SvnClient(FAKE)
        client.set_credentials("ivanov", "secret")
        client.list_dirs("svn://x/p")
        args = self.calls()[-1]["args"]
        self.assertIn("--password-from-stdin", args)
        self.assertNotIn("secret", args)
        self.assertEqual(client.version(), (1, 10))

    def test_store_auth_flag(self):
        self.mode("ok")
        self.assertTrue(self.client.store_auth)
        self.client.list_dirs("svn://x/p")
        self.assertNotIn("--no-auth-cache", self.calls()[-1]["args"])
        self.client.store_auth = False
        self.client.list_dirs("svn://x/p")
        self.assertIn("--no-auth-cache", self.calls()[-1]["args"])

    def test_authz_with_credentials(self):
        self.mode("authz")
        self.client.set_credentials("ivanov", "x")
        with self.assertRaises(SvnError) as cm:
            self.client.estimate("svn://x/p/trunk")
        self.assertEqual(cm.exception.kind, ErrorKind.AUTHZ)

    def test_no_connection_and_not_found(self):
        self.mode("noconn")
        with self.assertRaises(SvnError) as cm:
            self.client.list_dirs("svn://x/p")
        self.assertEqual(cm.exception.kind, ErrorKind.NO_CONNECTION)
        self.mode("notfound")
        with self.assertRaises(SvnError) as cm:
            self.client.list_dirs("svn://x/p")
        self.assertEqual(cm.exception.kind, ErrorKind.NOT_FOUND)

    def test_checkout_cancel_kills_process(self):
        self.mode("hang")
        token = CancelToken()
        progress = CheckoutProgress()
        threading.Timer(1.0, token.cancel).start()
        started = time.monotonic()
        with self.assertRaises(SvnError) as cm:
            self.client.checkout("svn://x/p/trunk", os.path.join(self.tmp.name, "p"), progress, token)
        self.assertEqual(cm.exception.kind, ErrorKind.CANCELLED)
        self.assertLess(time.monotonic() - started, 20)
        self.assertGreater(progress.count, 0)

    def test_checkout_failure_reports_kind(self):
        self.mode("authz")
        with self.assertRaises(SvnError) as cm:
            self.client.checkout("svn://x/p/trunk", os.path.join(self.tmp.name, "p"))
        self.assertEqual(cm.exception.kind, ErrorKind.AUTHZ)

    def test_missing_svn_is_other_error(self):
        with self.assertRaises(SvnError) as cm:
            SvnClient("Z:\\no\\such\\svn.exe").list_dirs("svn://x")
        self.assertEqual(cm.exception.kind, ErrorKind.OTHER)

    def test_password_not_in_repr(self):
        self.client.set_credentials("u", "topsecret")
        self.assertNotIn("topsecret", repr(self.client.credentials))

    def test_load_folder_marks_projects_and_tolerates_failures(self):
        from concurrent.futures import ThreadPoolExecutor
        self.mode("ok")  # любая папка содержит trunk → корень сам проект
        with ThreadPoolExecutor(2) as pool:
            listing = server.load_folder(self.client, "svn://x", ["r"], pool)
        self.assertTrue(listing.is_project)


class FakeTemplateCoreTests(unittest.TestCase):
    """Task T.0 на фейковом svn: export, cat, checkout --force, таймаут и отмена, пропавшее локальное хранилище."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "calls.jsonl")
        os.environ["FAKE_SVN_LOG"] = self.log
        os.environ["FAKE_SVN_MODE"] = "ok"
        self.client = SvnClient(FAKE)

    def tearDown(self):
        for k in ("FAKE_SVN_MODE", "FAKE_SVN_LOG"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def calls(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_export_flags_progress_and_files(self):
        dest = os.path.join(self.tmp.name, "шаблон @ 100%")
        progress = CheckoutProgress()
        res = self.client.export("svn://x/t/%D1%88", dest, progress)
        args = self.calls()[-1]["args"]
        self.assertEqual(args[0], "export")
        self.assertIn("--ignore-keywords", args)
        self.assertIn("--ignore-externals", args)
        self.assertIn("svn://x/t/%D1%88@", args)      # '@' в конце адреса
        self.assertIn(dest + "@", args)               # и в конце локального пути
        self.assertEqual((res.revision, res.entries, progress.count), (42, 3, 3))
        self.assertEqual(Path(dest, "docs", "б в.txt").read_text(encoding="utf-8"), "fake docs/б в.txt")

    def test_export_with_revision(self):
        self.client.export("svn://x/t", os.path.join(self.tmp.name, "e"), revision=7)
        self.assertIn("svn://x/t@7", self.calls()[-1]["args"])

    def test_export_cancel_and_timeout(self):
        os.environ["FAKE_SVN_MODE"] = "hang"
        token = CancelToken()
        threading.Timer(1.0, token.cancel).start()
        started = time.monotonic()
        with self.assertRaises(SvnError) as cm:
            self.client.export("svn://x/t", os.path.join(self.tmp.name, "e1"), cancel=token)
        self.assertEqual(cm.exception.kind, ErrorKind.CANCELLED)
        self.assertLess(time.monotonic() - started, 20)
        with self.assertRaises(SvnError) as cm:
            self.client.export("svn://x/t", os.path.join(self.tmp.name, "e2"), timeout=1.5)
        self.assertEqual(cm.exception.kind, ErrorKind.TIMEOUT)
        self.assertLess(time.monotonic() - started, 30)

    def test_checkout_timeout_when_svn_goes_silent(self):
        os.environ["FAKE_SVN_MODE"] = "hang"
        progress = CheckoutProgress()
        with self.assertRaises(SvnError) as cm:
            self.client.checkout("svn://x/p", os.path.join(self.tmp.name, "p"), progress, timeout=1.5)
        self.assertEqual(cm.exception.kind, ErrorKind.TIMEOUT)
        self.assertGreater(progress.count, 0)

    def test_checkout_force_counts_existing_files(self):
        dest = os.path.join(self.tmp.name, "p")
        os.makedirs(os.path.join(dest, "docs"))
        Path(dest, "a.txt").write_text("x")
        Path(dest, "docs", "б в.txt").write_text("x")
        progress = CheckoutProgress()
        res = self.client.checkout("svn://x/p", dest, progress, force=True)
        self.assertIn("--force", self.calls()[0]["args"])
        self.assertEqual((progress.count, res.entries), (4, 4))   # две строки «E» и две «A»
        self.client.checkout("svn://x/p", os.path.join(self.tmp.name, "q"))
        self.assertNotIn("--force", self.calls()[-2]["args"])      # последний вызов - info

    def test_cat_returns_bytes(self):
        data = self.client.cat("svn://x/t/template.json")
        self.assertEqual(data, "fake content of svn://x/t/template.json@".encode("utf-8"))
        self.client.cat("svn://x/t/template.json", revision=5)
        self.assertEqual(self.calls()[-1]["args"][1], "svn://x/t/template.json@5")

    def test_propget_needs_lock_lists_paths(self):
        paths = self.client.propget_needs_lock(os.path.join(self.tmp.name, "wc"))
        self.assertEqual([os.path.basename(p) for p in paths], ["a.txt", "занят.txt"])

    def test_has_connection(self):
        self.assertTrue(self.client.has_connection("svn://x/templates"))
        self.assertIn("svn://x/templates@", self.calls()[-1]["args"])
        os.environ["FAKE_SVN_MODE"] = "noconn"
        self.assertFalse(self.client.has_connection("svn://x/templates"))
        os.environ["FAKE_SVN_MODE"] = "authz"
        with self.assertRaises(SvnError) as cm:    # связь есть, нет прав: причину нельзя прятать за «нет связи»
            self.client.has_connection("svn://x/templates")
        self.assertEqual(cm.exception.kind, ErrorKind.AUTHZ)

    def test_has_connection_times_out(self):
        # таймаут самого листинга проверен в run(); здесь важно, что он передаётся и даёт «нет связи»
        with mock.patch.object(SvnClient, "list_entries", side_effect=SvnError(ErrorKind.TIMEOUT)) as listing:
            self.assertFalse(self.client.has_connection("svn://x/templates", timeout=1))
        self.assertEqual(listing.call_args.kwargs["timeout"], 1)

    def test_lost_local_history_is_not_no_connection(self):
        os.environ["FAKE_SVN_MODE"] = "lost_local"
        with self.assertRaises(SvnError) as cm:
            self.client.status(os.path.join(self.tmp.name, "lost_project"), check_server=True)
        self.assertEqual(cm.exception.kind, ErrorKind.LOCAL_HISTORY_MISSING)

    def test_lost_local_project_does_not_stop_check_or_mark_offline(self):
        from svnhelper.notifier import check_projects
        os.environ["FAKE_SVN_MODE"] = "lost_local"
        lost, fine = os.path.join(self.tmp.name, "lost_project"), os.path.join(self.tmp.name, "server_project")
        result = check_projects(self.client, [(lost, "локальный"), (fine, "серверный")])
        self.assertFalse(result.offline or result.auth_failed)
        self.assertEqual([(p.name, p.error) for p in result.projects],
                         [("локальный", "local_history_missing"), ("серверный", "")])


class FakeCommitTests(unittest.TestCase):
    """status/commit/incoming_log/diff_summary на фейковом svn: разбор XML и состав команд."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wc = os.path.join(self.tmp.name, "копия")
        os.makedirs(os.path.join(self.wc, "backup"))
        self.log = os.path.join(self.tmp.name, "calls.jsonl")
        os.environ["FAKE_SVN_LOG"] = self.log
        os.environ["FAKE_SVN_MODE"] = "ok"
        self.client = SvnClient(FAKE)

    def tearDown(self):
        for k in ("FAKE_SVN_MODE", "FAKE_SVN_LOG"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def calls(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def p(self, name):
        return os.path.join(self.wc, name)

    def names(self, changes):
        return [(os.path.basename(c.path), c.kind) for c in changes]

    def test_status_local_changes_and_incoming(self):
        from svnhelper.config import FilterRules
        st = self.client.status(self.wc, check_server=True, rules=FilterRules(junk_dirs=["backup"]))
        self.assertEqual(self.names(st.local_changes), [
            ("a.txt", "modified"), ("свойства.txt", "modified"), ("новый файл.txt", "unversioned"),
            ("добавлен.txt", "added"), ("удалён.txt", "deleted"), ("потерян.txt", "missing"),
            ("спор.txt", "conflicted"), ("занят.txt", "modified")])      # Thumbs.db и backup - мусор
        by_name = {os.path.basename(c.path): c for c in st.local_changes}
        self.assertEqual((by_name["a.txt"].needs_lock, by_name["a.txt"].locked_by_me), (True, False))
        self.assertEqual((by_name["занят.txt"].needs_lock, by_name["занят.txt"].locked_by_me), (True, True))
        self.assertEqual((by_name["удалён.txt"].needs_lock, by_name["новый файл.txt"].needs_lock), (False, False))
        self.assertEqual([os.path.basename(p) for p in st.incoming_files], ["чистый.txt"])
        # прежние поля и счётчики не изменились: мусор входит в unversioned
        self.assertEqual((st.modified, st.unversioned, st.conflicts, st.incoming, st.head), (6, 3, 1, 1, 44))
        self.assertEqual([os.path.basename(p) for p in st.locked_by_me], ["занят.txt"])

    def test_junk_filter_can_be_disabled(self):
        from svnhelper.config import FilterRules
        st = self.client.status(self.wc, rules=FilterRules(enabled=False))
        self.assertIn("Thumbs.db", [os.path.basename(c.path) for c in st.local_changes])

    def test_commit_adds_removes_and_commits_with_files_in_utf8(self):
        files = [self.p("новый файл.txt"), self.p("потерян.txt"), self.p("удалён.txt"),
                 self.p("занят.txt"), self.p("добавлен.txt")]
        res = self.client.commit(self.wc, files, "Правка чертежа\nвторая строка")
        self.assertEqual(res.revision, 45)
        self.assertEqual(res.committed, files)
        calls = [c for c in self.calls() if c["args"][0] in ("add", "rm", "commit")]
        self.assertEqual([c["args"][0] for c in calls], ["add", "rm", "commit"])
        self.assertEqual(calls[0]["args"][:2], ["add", "--parents"])
        self.assertEqual(calls[0]["targets"], self.p("новый файл.txt") + "@\n")
        self.assertEqual(calls[1]["args"][:2], ["rm", "--keep-local"])
        self.assertEqual(calls[1]["targets"], self.p("потерян.txt") + "@\n")
        commit = calls[2]
        self.assertIn("--encoding", commit["args"])
        self.assertEqual(commit["message"], "Правка чертежа\nвторая строка")
        self.assertEqual(commit["targets"].splitlines(), [f + "@" for f in files])

    def test_resolve_passes_accept_choice(self):
        self.client.resolve(self.p("спор.txt"), "mine-full")
        self.client.resolve(self.p("спор.txt"), "theirs-full")
        calls = [c["args"] for c in self.calls() if c["args"][0] == "resolve"]
        self.assertEqual([a[a.index("--accept") + 1] for a in calls], ["mine-full", "theirs-full"])
        self.assertTrue(all(self.p("спор.txt") + "@" in a for a in calls))
        with self.assertRaises(ValueError):
            self.client.resolve(self.p("спор.txt"), "working")

    def test_new_parents_lists_only_new_folders(self):
        from svnhelper.svn_core import LocalChange
        known = {os.path.normcase(os.path.abspath(self.p(n))): LocalChange(self.p(n), k)
                 for n, k in (("a", "unversioned"), (os.path.join("a", "b"), "added"), ("c", "modified"))}
        got = SvnClient._new_parents(os.path.join(self.wc, "a", "b", "f.txt"), self.wc, known)
        self.assertEqual(got, [self.p(os.path.join("a", "b")), self.p("a")])
        self.assertEqual(SvnClient._new_parents(self.p("f.txt"), self.wc, known), [])

    def test_commit_needs_lock_without_lock_is_refused_before_any_action(self):
        with self.assertRaises(SvnError) as cm:
            self.client.commit(self.wc, [self.p("a.txt"), self.p("новый файл.txt")], "m")
        self.assertEqual(cm.exception.kind, ErrorKind.LOCK_REQUIRED)
        self.assertEqual(cm.exception.raw, self.p("a.txt"))
        self.assertFalse([c for c in self.calls() if c["args"][0] in ("add", "rm", "commit")])

    def test_commit_errors(self):
        for mode, kind in (("commit_ood", ErrorKind.OUT_OF_DATE), ("commit_tag", ErrorKind.READ_ONLY_LINE)):
            with self.subTest(mode=mode):
                os.environ["FAKE_SVN_MODE"] = mode
                with self.assertRaises(SvnError) as cm:
                    self.client.commit(self.wc, [self.p("удалён.txt")], "m")
                self.assertEqual(cm.exception.kind, kind)

    def test_incoming_log_parses_entries_from_newest_known_revision(self):
        entries = self.client.incoming_log(self.wc)
        log_call = [c for c in self.calls() if c["args"][0] == "log"][0]["args"]
        self.assertEqual(log_call[log_call.index("-r") + 1], "43:HEAD")   # max_revision копии 42, +1
        self.assertIn("-v", log_call)
        self.assertEqual([(e.revision, e.author, e.message) for e in entries],
                         [(43, "иванов", "Правка чертежа"), (44, "петров", "Убрал лишнее")])
        self.assertEqual([(p.action, p.path) for p in entries[0].paths],
                         [("M", "/p/trunk/чертёж 1.txt"), ("A", "/p/trunk/новый.txt")])
        self.assertEqual(entries[0].date.year, 2026)

    def test_recent_log_asks_for_limited_verbose_log(self):
        entries = self.client.recent_log(self.wc, 50)
        log_call = [c for c in self.calls() if c["args"][0] == "log"][0]["args"]
        self.assertEqual(log_call[log_call.index("-l") + 1], "50")
        self.assertIn("-v", log_call)
        self.assertNotIn("-r", log_call)
        self.assertEqual([(e.revision, e.author, len(e.paths)) for e in entries],
                         [(43, "иванов", 2), (44, "петров", 1)])

    def test_diff_summary_returns_relative_paths_and_kinds(self):
        got = self.client.diff_summary("svn://x/p/trunk", "svn://x/p/branches/b")
        self.assertEqual(got, [("чертёж 1.txt", "modified"), ("docs/new.txt", "added"), ("old.txt", "deleted")])
        diff_call = [c for c in self.calls() if c["args"][0] == "diff"][0]["args"]
        self.assertEqual(diff_call[:3], ["diff", "--summarize", "--xml"])


if __name__ == "__main__":
    unittest.main()


class DiskPathTests(unittest.TestCase):
    """Папка проекта на диске Z по адресу в хранилище (обратное к map_path)."""

    MIRRORS = [{"path": "Z:\Specific_projects", "repo": "test/projects/Specific_projects"}]

    def test_maps_project_to_disk(self):
        from svnhelper import mirrors
        got = mirrors.disk_path("svn://h", ("test", "projects", "Specific_projects", "A", "b c"), self.MIRRORS,
                                "svn://h")
        self.assertEqual(got, "Z:\Specific_projects\A\\" + "b c")

    def test_outside_mirror_is_empty(self):
        from svnhelper import mirrors
        self.assertEqual(mirrors.disk_path("svn://h", ("other", "a"), self.MIRRORS, "svn://h"), "")
        self.assertEqual(mirrors.disk_path("svn://x", ("test", "projects", "Specific_projects", "A"),
                                           self.MIRRORS, "svn://h"), "")
