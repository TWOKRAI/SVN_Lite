"""Тесты ядра шаблонов (svnhelper/templates.py): манифест, имена, копирование, кэш, поиск на сервере."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import date
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svnhelper import config, templates
from svnhelper.server import build_url
from svnhelper.svn_core import CancelToken, ErrorKind, ListEntry, SvnClient, SvnError
from svnhelper.templates import (ManifestError, TemplateError, TemplateInfo, TemplatePaths, apply_plan, build_plan,
                                 fetch_template, list_template_paths, list_templates, load_manifest,
                                 load_templates, parse_manifest)

TODAY = date(2026, 10, 28)
EMPTY_DIRS = ["01 Заказчик", "02 Конструктор", "03 Схемы", "04 Печатные платы", "05 Закупки", "06 Сборка",
              "07 Испытания", "docs/Specification"]


def manifest_json(**extra) -> str:
    data = {"format": 1, "title": "Электроника: проект", "description": "Папки, спецификация",
            "fields": [{"id": "date", "label": "Дата", "default": "{{today:yyMMdd}}"},
                       {"id": "name", "label": "Название", "required": True, "hint": "например, вафли"}],
            "folder_name": "{{date}}_{{name}}", "parent": "konstruktor/projects"}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


def make_template(root: Path, **extra) -> Path:
    """Шаблон как в примере: пустые папки, docx, xlsx (содержимое - произвольные байты), README."""
    root.mkdir(parents=True)
    for rel in EMPTY_DIRS:
        (root / rel).mkdir(parents=True)
    (root / "Шаблон ТЗ.docx").write_bytes(bytes(range(256)) * 20 + b"\r\n\x00\xff")
    (root / "docs" / "Specification" / "Шаблон каталог спецификация электроника.xlsx").write_bytes(b"PK\x03\x04" + b"\x00\x01" * 500)
    (root / "README.txt").write_bytes("Текст\r\nс переводом строки\n".encode("utf-8"))
    extra.setdefault("renames", {
        "README.txt": "README_{{folder}}.txt",
        "docs/Specification/Шаблон каталог спецификация электроника.xlsx": "{{folder}}_спецификация.xlsx"})
    (root / "template.json").write_text(manifest_json(**extra), encoding="utf-8")
    return root


def tree(root: Path) -> dict:
    """{относительный путь: байты или None для папки}."""
    result = {}
    for base, dirs, files in os.walk(root):
        for name in dirs:
            result[os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/")] = None
        for name in files:
            result[os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/")] = \
                Path(base, name).read_bytes()
    return result


class ManifestTests(unittest.TestCase):
    def test_example_parses_with_builtin_fields(self):
        m = parse_manifest(manifest_json(), repos=["konstruktor", "common"])
        self.assertEqual([f.id for f in m.fields], ["date", "name"])
        self.assertTrue(m.fields[1].required)
        self.assertEqual((m.title, m.parent, m.folder_name), ("Электроника: проект", "konstruktor/projects",
                                                              "{{date}}_{{name}}"))

    def test_minimal_manifest_works(self):
        m = parse_manifest('{"format": 1}', default_folder_format="{{name}}", fallback_title="tpl")
        self.assertEqual((m.title, m.folder_name, m.renames, m.parent), ("tpl", "{{name}}", {}, ""))
        self.assertEqual([f.id for f in m.fields], ["date", "name"])

    def test_custom_fields_added_and_builtin_overridden(self):
        m = parse_manifest(manifest_json(fields=[{"id": "customer", "label": "Заказчик"},
                                                  {"id": "name", "label": "Имя", "required": False}]))
        self.assertEqual([f.id for f in m.fields], ["date", "name", "customer"])
        self.assertFalse(m.fields[1].required)

    def test_broken_files(self):
        for data in (b"{not json", b"[]", b'{"title": "x"}', b'{"format": "1"}', b'{"format": 2}', b'{"format": 0}'):
            with self.assertRaises(ManifestError, msg=data):
                parse_manifest(data)
        with self.assertRaisesRegex(ManifestError, "новой версии"):
            parse_manifest('{"format": 2}')

    def test_utf8_bom_is_accepted(self):
        self.assertEqual(parse_manifest(b"\xef\xbb\xbf" + manifest_json().encode("utf-8")).title,
                         "Электроника: проект")

    def test_wrong_types(self):
        for extra in ({"title": 5}, {"fields": {}}, {"fields": [{"label": "x"}]}, {"renames": []},
                      {"fields": [{"id": "a", "required": "yes"}]}, {"folder_name": 3}, {"folder_name": " "}):
            with self.assertRaises(ManifestError, msg=extra):
                parse_manifest(manifest_json(**extra))

    def test_unknown_placeholder_everywhere(self):
        for extra in ({"folder_name": "{{date}}_{{nope}}"},
                      {"renames": {"a": "{{nope}}"}},
                      {"fields": [{"id": "x", "default": "{{name}}"}]},
                      {"folder_name": "{{today}}"},          # маска обязательна
                      {"folder_name": "{{date}}_{{name"}):
            with self.assertRaises(ManifestError, msg=extra):
                parse_manifest(manifest_json(**extra))

    def test_folder_inside_folder_name_forbidden(self):
        with self.assertRaisesRegex(ManifestError, "folder"):
            parse_manifest(manifest_json(folder_name="{{folder}}_x"))
        with self.assertRaisesRegex(ManifestError, "folder"):
            parse_manifest(manifest_json(folder_name="{{ folder }}"))

    def test_builtin_field_ids_forbidden(self):
        for bad in ("folder", "author", "today"):
            with self.assertRaisesRegex(ManifestError, bad):
                parse_manifest(manifest_json(fields=[{"id": bad}]))

    def test_duplicate_field_id(self):
        with self.assertRaises(ManifestError):
            parse_manifest(manifest_json(fields=[{"id": "x"}, {"id": "x"}]))

    def test_date_masks(self):
        self.assertEqual(templates.format_date("yyyy-MM-dd", TODAY), "2026-10-28")
        self.assertEqual(templates.format_date("yyMMdd", TODAY), "261028")
        for bad in ("yyMMmm", "yyMMdd HH", "", "yyyyy", "y/M"):
            with self.assertRaises(ValueError, msg=bad):
                templates.format_date(bad, TODAY)
        with self.assertRaises(ManifestError):
            parse_manifest(manifest_json(folder_name="{{today:hhmm}}_{{name}}"))

    def test_parent_must_start_with_repo_root(self):
        parse_manifest(manifest_json(parent="konstruktor"), repos=["konstruktor"])
        parse_manifest(manifest_json(parent="/konstruktor/projects/"), repos=["konstruktor"])
        for parent in ("other/projects", "konstruktor2/x"):
            with self.assertRaisesRegex(ManifestError, "parent"):
                parse_manifest(manifest_json(parent=parent), repos=["konstruktor"])
        parse_manifest(manifest_json(parent="anything"))           # корни не заданы - не проверяется

    def test_renames_rules(self):
        paths = ["a.txt", "docs", "docs/Spec", "docs/Spec/B.xlsx", "docs/Other"]
        ok = parse_manifest(manifest_json(renames={"A.TXT": "x_{{name}}.txt", "docs\\Spec/B.xlsx": "b.xlsx"}), paths)
        self.assertEqual(set(ok.renames), {"a.txt", "docs/Spec/B.xlsx"})   # ключи приводятся к виду из шаблона
        bad = [{"missing.txt": "x"},                                      # нет в шаблоне
               {"a.txt": "dir/x.txt"}, {"a.txt": "dir\\x.txt"},           # путь вместо имени
               {"a.txt": "  "}, {"a.txt": 5},
               {"a.txt": "Same.txt", "docs": "same.TXT"},                 # два источника в одно имя, регистр
               {"docs/Spec": "same", "docs/Other": "SAME"},
               {"a.txt": "x", "A.txt": "y"},                              # один путь дважды
               {"../a.txt": "x"}]
        for renames in bad:
            with self.assertRaises(ManifestError, msg=renames):
                parse_manifest(manifest_json(renames=renames), paths)

    def test_same_name_in_different_folders_is_fine(self):
        parse_manifest(manifest_json(renames={"docs/Spec": "same", "a.txt": "same"}), ["a.txt", "docs", "docs/Spec"])

    def test_load_manifest_from_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_template(Path(tmp) / "t")
            m = load_manifest(root)
            self.assertEqual(len(m.renames), 2)
            self.assertNotIn("template.json", list_template_paths(root))
            with self.assertRaises(ManifestError):
                load_manifest(Path(tmp) / "nothing")
            (root / "template.json").write_text(manifest_json(renames={"нет такого": "x"}), encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "нет в шаблоне"):
                load_manifest(root)


class NameTests(unittest.TestCase):
    def test_valid_names(self):
        for name in ("отбраковщик вафель", "a@b", "100%", "файл.docx", "COM10", "CONSOLE", "a b c"):
            self.assertEqual(templates.validate_file_name(name), name)

    def test_invalid_names(self):
        for name in ("", "  ", "a:b", 'a"b', "a|b", "a<b", "a?b", "a*b", "a/b", "a\\b", "x.", "x ", " x", "a\tb",
                     "CON", "con", "NUL", "nul.txt", "Com1", "LPT9.log", "x" * 256):
            with self.assertRaises(ValueError, msg=name):
                templates.validate_file_name(name)


class PlanTests(unittest.TestCase):
    def manifest(self, **extra):
        return parse_manifest(manifest_json(**extra))

    def test_folder_and_renames_with_awkward_characters(self):
        for name in ("отбраковщик вафель", "изделие @1", "скидка 100%", "a   b"):
            plan = build_plan(self.manifest(), {"date": "261028", "name": name}, today=TODAY)
            self.assertTrue(plan.ok, plan.errors)
            self.assertEqual(plan.folder, f"261028_{name}")

    def test_date_default_and_trim(self):
        plan = build_plan(self.manifest(), {"name": "  вафли  "}, author="ivanov", today=TODAY)
        self.assertEqual(plan.folder, "261028_вафли")
        self.assertEqual(templates.default_values(self.manifest(), "ivanov", TODAY), {"date": "261028", "name": ""})

    def test_required_empty(self):
        plan = build_plan(self.manifest(), {"date": "261028", "name": "  "}, today=TODAY)
        self.assertFalse(plan.ok)
        self.assertIn("Название", plan.errors[0])

    def test_forbidden_characters_in_field(self):
        for bad in ("a/b", "a\\b"):
            plan = build_plan(self.manifest(), {"name": bad}, today=TODAY)
            self.assertFalse(plan.ok, bad)
        for bad in ("a:b", "a*b", 'a"b', "a?b", "a<b", "a|b"):
            plan = build_plan(self.manifest(), {"name": bad}, today=TODAY)
            self.assertFalse(plan.ok, bad)
            self.assertIn("символ", " ".join(plan.errors))

    def test_reserved_windows_names(self):
        for folder_name, name in (("{{name}}", "CON"), ("{{name}}", "nul"), ("{{name}}", "Lpt1")):
            plan = build_plan(self.manifest(folder_name=folder_name), {"name": name}, today=TODAY)
            self.assertFalse(plan.ok, name)
            self.assertIn("зарезервировано", plan.errors[0])
        plan = build_plan(self.manifest(renames={"a.txt": "{{name}}.txt"}), {"name": "NUL"}, today=TODAY)
        self.assertTrue(any("зарезервировано" in e for e in plan.errors))

    def test_trailing_dot_space_and_long_name(self):
        for name in ("вафли.", "вафли "):
            plan = build_plan(self.manifest(folder_name="{{name}}"), {"name": name}, today=TODAY)
            # пробел по краям срезается при вводе, точка - нет
            self.assertEqual(plan.ok, name.endswith(" "), name)
        plan = build_plan(self.manifest(), {"name": "я" * 90}, today=TODAY)
        self.assertFalse(plan.ok)
        self.assertIn("80", " ".join(plan.errors))

    def test_dot_start_rejected_for_folder(self):
        self.assertFalse(build_plan(self.manifest(folder_name="{{name}}"), {"name": ".скрытая"}, today=TODAY).ok)

    def test_author_and_today_in_renames(self):
        m = self.manifest(renames={"a.txt": "{{author}}_{{today:yyyy-MM-dd}}_{{name}}.txt"})
        plan = build_plan(m, {"name": "x"}, author="ivanov", today=TODAY)
        self.assertEqual(plan.renames, [("a.txt", "ivanov_2026-10-28_x.txt")])

    def test_author_with_backslash_is_error(self):
        m = self.manifest(renames={"a.txt": "{{author}}.txt"})
        self.assertFalse(build_plan(m, {"name": "x"}, author="DOM\\ivanov", today=TODAY).ok)

    def test_renames_sorted_deepest_first(self):
        m = self.manifest(renames={"docs": "Документы", "docs/Spec/a.xlsx": "a2.xlsx", "docs/Spec": "Спец", "z.txt": "y"})
        plan = build_plan(m, {"name": "x"}, today=TODAY)
        depths = [p.count("/") for p, _ in plan.renames]
        self.assertEqual(depths, sorted(depths, reverse=True))

    def test_duplicate_after_substitution(self):
        m = self.manifest(renames={"a.txt": "{{name}}.txt", "b.txt": "{{date}}.txt"})
        plan = build_plan(m, {"date": "same", "name": "SAME"}, today=TODAY)
        self.assertFalse(plan.ok)
        self.assertIn("одно и то же имя", plan.errors[0])

    def test_collision_with_untouched_sibling(self):
        m = self.manifest(renames={"a.txt": "b.txt"})
        plan = build_plan(m, {"name": "x"}, template_paths=["a.txt", "B.TXT"], today=TODAY)
        self.assertFalse(plan.ok)
        self.assertIn("такое уже есть", plan.errors[0])

    def test_swap_of_names_is_not_a_collision(self):
        m = self.manifest(renames={"a.txt": "b.txt", "b.txt": "c.txt"})
        self.assertTrue(build_plan(m, {"name": "x"}, template_paths=["a.txt", "b.txt"], today=TODAY).ok)

    def test_path_length_warning_and_refusal(self):
        paths = ["docs", "docs/" + "ф" * 140 + ".xlsx"]
        m = self.manifest()
        base = "C:\\Проекты"
        plan = build_plan(m, {"name": "x"}, template_paths=paths, base_dir=base, today=TODAY)
        self.assertTrue(plan.ok and not plan.warnings, plan.errors)
        self.assertEqual(plan.longest_path, len(base) + 1 + len("261028_x") + 1 + len(paths[1]))
        plan = build_plan(m, {"name": "я" * 60}, template_paths=paths, base_dir=base, today=TODAY)
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("200", plan.warnings[0])
        self.assertEqual(len(build_plan(m, {"name": "я" * 60}, template_paths=paths, base_dir=base, warn_length=300,
                                        today=TODAY).warnings), 0)
        long_base = "C:\\" + "п" * 120
        plan = build_plan(m, {"name": "я" * 60}, template_paths=paths, base_dir=long_base, today=TODAY)
        self.assertFalse(plan.ok)
        self.assertIn("Windows", plan.errors[0])
        self.assertGreaterEqual(plan.longest_path, 260)

    def test_length_uses_renamed_paths(self):
        m = self.manifest(renames={"docs": "д", "docs/" + "ф" * 100: "{{name}}"})
        paths = ["docs", "docs/" + "ф" * 100]
        plan = build_plan(m, {"name": "x"}, template_paths=paths, base_dir="C:\\a", today=TODAY)
        self.assertEqual(plan.longest_path, len("C:\\a") + 1 + len("261028_x") + 1 + len("д/x"))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def plan_for(self, template: Path, name="отбраковщик вафель", **extra):
        m = load_manifest(template)
        return build_plan(m, {"date": "261028", "name": name}, template_paths=list_template_paths(template),
                          today=TODAY)

    def test_copy_one_to_one_without_renames(self):
        template = make_template(self.root / "Шаблон проекта", renames={})
        plan = self.plan_for(template)
        target = self.root / "out" / plan.folder
        apply_plan(plan, template, target)
        expected = tree(template)
        del expected["template.json"]
        self.assertEqual(tree(target), expected)
        for empty in EMPTY_DIRS:
            self.assertTrue((target / empty).is_dir(), empty)

    def test_rename_files_and_bytes_unchanged(self):
        template = make_template(self.root / "t")
        plan = self.plan_for(template)
        self.assertEqual(plan.folder, "261028_отбраковщик вафель")
        target = self.root / plan.folder
        apply_plan(plan, template, target)
        result = tree(target)
        src = tree(template)
        self.assertNotIn("template.json", result)
        self.assertNotIn("README.txt", result)
        self.assertEqual(result["README_261028_отбраковщик вафель.txt"], src["README.txt"])
        spec = "docs/Specification/261028_отбраковщик вафель_спецификация.xlsx"
        self.assertEqual(result[spec], src["docs/Specification/Шаблон каталог спецификация электроника.xlsx"])
        self.assertEqual(result["Шаблон ТЗ.docx"], src["Шаблон ТЗ.docx"])
        self.assertEqual(len(result), len(src) - 1)

    def test_nested_and_case_only_renames(self):
        template = make_template(self.root / "t", renames={
            "docs": "Документы", "docs/Specification": "Спец", "docs/Specification/" +
            "Шаблон каталог спецификация электроника.xlsx": "Каталог.xlsx", "README.txt": "readme.TXT",
            "01 Заказчик": "01 заказчик"})
        plan = self.plan_for(template)
        self.assertTrue(plan.ok, plan.errors)
        target = self.root / "out"
        apply_plan(plan, template, target)
        names = {p for p in tree(target)}
        self.assertIn("Документы/Спец/Каталог.xlsx", names)
        self.assertIn("readme.TXT", names)
        self.assertIn("01 заказчик", names)
        self.assertNotIn("docs", names)

    def test_swap_chain_renames(self):
        template = make_template(self.root / "t", renames={"README.txt": "Шаблон ТЗ.docx", "Шаблон ТЗ.docx": "README.txt"})
        plan = self.plan_for(template)
        self.assertTrue(plan.ok, plan.errors)
        target = self.root / "out"
        apply_plan(plan, template, target)
        src = tree(template)
        self.assertEqual(tree(target)["Шаблон ТЗ.docx"], src["README.txt"])
        self.assertEqual(tree(target)["README.txt"], src["Шаблон ТЗ.docx"])

    def test_refuses_plan_with_errors_and_busy_target(self):
        template = make_template(self.root / "t")
        bad = self.plan_for(template, name="a:b")
        with self.assertRaises(TemplateError):
            apply_plan(bad, template, self.root / "x")
        self.assertFalse((self.root / "x").exists())
        good = self.plan_for(template)
        busy = self.root / "busy"
        busy.mkdir()
        (busy / "f.txt").write_text("x")
        with self.assertRaises(FileExistsError):
            apply_plan(good, template, busy)
        self.assertEqual([p.name for p in busy.iterdir()], ["f.txt"])
        empty = self.root / "empty"
        empty.mkdir()
        apply_plan(good, template, empty)      # пустая папка допустима
        self.assertTrue((empty / "01 Заказчик").is_dir())

    def test_failure_removes_created_folder(self):
        template = make_template(self.root / "t")
        plan = self.plan_for(template)
        plan.renames.append(("нет такого файла", "x"))   # переименовать нечего: сбой после копирования
        target = self.root / "out"
        with self.assertRaises(OSError):
            apply_plan(plan, template, target)
        self.assertFalse(target.exists())


# --------------------------------------------------------------------------
# Сервер: заглушка клиента и настоящий svn
# --------------------------------------------------------------------------

ROOT_URL = "svn://srv/konstruktor/templates"


class FakeClient:
    """Минимум SvnClient для templates: шаблоны лежат в памяти, связь включается и выключается."""

    def __init__(self):
        self.online = True
        self.templates = {}      # имя -> {"revision": int, "files": {rel: bytes}, "dirs": [rel], "manifest": bytes|None}
        self.calls = []
        self.exports = []        # (имя, запрошенная версия)
        self.export_delay = 0.0

    def add(self, name, revision=10, title="Электроника", manifest="auto", files=None):
        if manifest == "auto":
            manifest = manifest_json(title=title).encode("utf-8")
        self.templates[name] = {"revision": revision, "files": files or {"a.txt": b"hello"}, "dirs": ["пусто"],
                                "manifest": manifest}

    def _down(self):
        if not self.online:
            raise SvnError(ErrorKind.NO_CONNECTION, "E170013 Unable to connect")

    def list_entries(self, url, recursive=False, cancel=None, timeout=None):
        self.calls.append("list")
        self._down()
        return [ListEntry("dir", n, revision=t["revision"]) for n, t in self.templates.items()] + \
               [ListEntry("file", "readme.txt", size=3)]

    def cat(self, url, revision=None, cancel=None, timeout=None):
        self.calls.append("cat")
        self._down()
        name = unquote(url.rstrip("@")).split("/")[-2]
        manifest = self.templates[name]["manifest"]
        if manifest is None:
            raise SvnError(ErrorKind.OTHER, "svn: E170000: URL doesn't exist")
        return manifest

    def export(self, url, dest, progress=None, cancel=None, revision=None, timeout=None):
        self.calls.append("export")
        self._down()
        name = unquote(url).split("/")[-1]
        t = self.templates.get(name)
        if t is None:
            raise SvnError(ErrorKind.OTHER, "svn: E170000: URL doesn't exist")
        self.exports.append((name, revision))
        if revision != t["revision"]:
            raise AssertionError(f"запрошена версия {revision}, на сервере {t['revision']}")
        time.sleep(self.export_delay)
        os.makedirs(dest)
        for rel in t["dirs"]:
            os.makedirs(os.path.join(dest, rel))
        for rel, data in t["files"].items():
            Path(dest, rel).write_bytes(data)
        if t["manifest"] is not None:
            Path(dest, "template.json").write_bytes(t["manifest"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        self.client = FakeClient()
        self.client.add("electronics", 10, "Электроника")
        self.client.add("Папка @1", 7, "Яблоко")
        self.client.templates["просто папка"] = {"revision": 3, "files": {}, "dirs": [], "manifest": None}

    def listing(self):
        return list_templates(self.client, ROOT_URL, self.cache)

    def test_lists_only_folders_with_manifest(self):
        infos = self.listing()
        self.assertEqual([i.id for i in infos], ["electronics", "Папка @1"])
        self.assertEqual(infos[0].title, "Электроника")
        self.assertEqual(infos[0].revision, 10)
        self.assertEqual(infos[1].url, build_url(ROOT_URL, ["Папка @1"]))
        self.assertIn("%40", infos[1].url)

    def test_broken_manifest_skipped_and_empty_root(self):
        self.client.add("broken", 5, manifest=b"{oops")
        self.assertNotIn("broken", [i.id for i in self.listing()])
        self.assertEqual(list_templates(self.client, "  ", self.cache), [])
        self.assertEqual(load_templates(self.client, "", self.cache).templates, [])

    def test_manifest_read_only_for_changed(self):
        infos = self.listing()
        for info in infos:
            fetch_template(self.client, info, self.cache)
        self.client.calls.clear()
        self.listing()
        self.assertEqual(self.client.calls.count("cat"), 1)     # шаблон без кэша: «просто папка» (не шаблон)
        self.client.templates["electronics"]["revision"] = 11
        self.client.calls.clear()
        self.listing()
        self.assertEqual(self.client.calls.count("cat"), 2)     # + изменившийся electronics

    def test_fetch_caches_and_skips_unchanged(self):
        info = self.listing()[0]
        got = fetch_template(self.client, info, self.cache)
        self.assertFalse(got.offline)
        self.assertEqual(got.revision, 10)
        self.assertIsNotNone(got.fetched)
        self.assertEqual((got.path / "a.txt").read_bytes(), b"hello")
        self.assertTrue((got.path / "пусто").is_dir())
        self.assertEqual(self.client.calls.count("export"), 1)
        again = fetch_template(self.client, info, self.cache)
        self.assertEqual(self.client.calls.count("export"), 1)
        self.assertEqual(again.path, got.path)

    def test_fetch_replaces_cache_when_template_changed(self):
        info = self.listing()[0]
        fetch_template(self.client, info, self.cache)
        self.client.templates["electronics"].update(revision=11, files={"b.txt": b"new"})
        info = [i for i in self.listing() if i.id == "electronics"][0]
        got = fetch_template(self.client, info, self.cache)
        self.assertEqual(got.revision, 11)
        self.assertEqual(sorted(p.name for p in got.path.iterdir() if p.is_file()), ["b.txt", "template.json"])
        self.assertEqual([p.name for p in self.cache.iterdir() if p.name.startswith(".")], [])   # временное убрано

    def test_offline_with_cache(self):
        infos = self.listing()
        fetch_template(self.client, infos[0], self.cache)
        self.client.online = False
        stale = TemplateInfo(infos[0].id, infos[0].title, "", 99, infos[0].url)   # на сервере ушло вперёд
        got = fetch_template(self.client, stale, self.cache)
        self.assertTrue(got.offline)
        self.assertEqual(got.revision, 10)
        self.assertEqual((got.path / "a.txt").read_bytes(), b"hello")
        listing = load_templates(self.client, ROOT_URL, self.cache)
        self.assertTrue(listing.offline)
        self.assertEqual([t.id for t in listing.templates], ["electronics"])
        self.assertEqual(listing.templates[0].title, "Электроника")

    def test_offline_without_cache(self):
        infos = self.listing()
        self.client.online = False
        with self.assertRaises(TemplateError) as cm:
            fetch_template(self.client, infos[0], self.cache)
        self.assertEqual(str(cm.exception), templates.NO_CACHE_MESSAGE)
        self.assertEqual(list(self.cache.iterdir()), [])
        listing = load_templates(self.client, ROOT_URL, self.cache)
        self.assertEqual((listing.templates, listing.offline), ([], True))

    def test_other_list_errors_are_not_hidden_as_offline(self):
        def deny(*args, **kwargs):
            raise SvnError(ErrorKind.AUTHZ, "E170001")
        self.client.list_entries = deny
        with self.assertRaises(SvnError):
            load_templates(self.client, ROOT_URL, self.cache)

    def test_removed_template_gives_clear_error(self):
        info = self.listing()[0]
        del self.client.templates["electronics"]
        with self.assertRaisesRegex(TemplateError, "не найден"):
            fetch_template(self.client, info, self.cache)

    def test_failed_export_keeps_old_cache(self):
        info = self.listing()[0]
        first = fetch_template(self.client, info, self.cache)
        self.client.online = False
        newer = TemplateInfo(info.id, info.title, "", 11, info.url)
        fetch_template(self.client, newer, self.cache)
        self.assertEqual((first.path / "a.txt").read_bytes(), b"hello")

    def test_warm_cache(self):
        self.assertEqual(templates.warm_cache(self.client, ROOT_URL, self.cache), 2)
        self.assertEqual(templates.warm_cache(self.client, ROOT_URL, self.cache), 0)
        self.client.online = False
        self.assertEqual(templates.warm_cache(self.client, ROOT_URL, self.cache), 0)
        self.assertEqual(len(templates.cached_templates(self.cache)), 2)

    def test_default_cache_dir_moves_out_of_roaming(self):
        env = {"APPDATA": "C:\\Users\\u\\AppData\\Roaming", "LOCALAPPDATA": "C:\\Users\\u\\AppData\\Local"}
        with unittest.mock.patch.dict(os.environ, env):
            os.environ.pop("SVNLITE_DATA_DIR", None)
            self.assertEqual(templates.default_cache_dir(),
                             Path("C:\\Users\\u\\AppData\\Local") / "SvnHelper" / "templates_cache")
        with unittest.mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": self.tmp.name}):
            self.assertEqual(templates.default_cache_dir(), Path(self.tmp.name) / "templates_cache")


class ConfigParseTests(unittest.TestCase):
    def apply(self, data):
        cfg = config.Config()
        config.apply_config_data(cfg, data)
        return cfg

    def test_defaults(self):
        cfg = config.Config()
        self.assertEqual((cfg.templates_root, cfg.project_folder_format), ("", "{{date}}_{{name}}"))

    def test_values_are_read_and_trimmed(self):
        cfg = self.apply({"templates_root": "  svn://srv/konstruktor/templates/ ",
                          "project_folder_format": " {{name}}_{{date}} "})
        self.assertEqual((cfg.templates_root, cfg.project_folder_format),
                         ("svn://srv/konstruktor/templates", "{{name}}_{{date}}"))
        self.assertEqual(cfg.warnings, [])

    def test_empty_format_keeps_default_and_empty_root_clears(self):
        cfg = self.apply({"templates_root": "", "project_folder_format": "  "})
        self.assertEqual((cfg.templates_root, cfg.project_folder_format), ("", "{{date}}_{{name}}"))

    def test_wrong_types_give_warnings(self):
        cfg = self.apply({"templates_root": 5, "project_folder_format": ["x"]})
        self.assertEqual((cfg.templates_root, cfg.project_folder_format), ("", "{{date}}_{{name}}"))
        self.assertEqual(len(cfg.warnings), 2)


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def manifest(self, **extra):
        return parse_manifest(manifest_json(**extra))

    # -- разбор манифеста
    def test_bare_today_has_its_own_message(self):
        with self.assertRaisesRegex(ManifestError, "нужна маска"):
            parse_manifest(manifest_json(folder_name="{{today}}_{{name}}"))

    def test_constant_parts_are_validated_with_sample_values(self):
        for extra in ({"folder_name": "a:{{name}}"}, {"folder_name": "{{name}}."}, {"folder_name": "CON"},
                      {"folder_name": ".{{name}}"}, {"renames": {"a.txt": "x:{{name}}"}},
                      {"renames": {"a.txt": "{{name}}."}}, {"renames": {"a.txt": "NUL"}}):
            with self.assertRaises(ManifestError, msg=extra):
                parse_manifest(manifest_json(**extra), ["a.txt"])

    def test_constant_rename_colliding_with_untouched_sibling(self):
        with self.assertRaisesRegex(ManifestError, "уже есть"):
            parse_manifest(manifest_json(renames={"a.txt": "B.txt"}), ["a.txt", "b.txt"])
        parse_manifest(manifest_json(renames={"a.txt": "b.txt", "b.txt": "c.txt"}), ["a.txt", "b.txt"])

    def test_parent_with_dots_is_error(self):
        for parent in ("konstruktor/../x", "konstruktor/./x", "./konstruktor"):
            with self.assertRaises(ManifestError, msg=parent):
                parse_manifest(manifest_json(parent=parent), repos=["konstruktor"])

    def test_parent_without_repos_is_logged(self):
        with self.assertLogs("svnhelper.templates", "WARNING"):
            parse_manifest(manifest_json(parent="anything/here"))

    def test_console_device_names(self):
        for name in ("CONIN$", "conout$"):
            with self.assertRaises(ValueError):
                templates.validate_file_name(name)

    def test_percent_and_at_in_rename_keys_and_values(self):
        paths = ["100% @1", "100% @1/a@b%.txt"]
        m = parse_manifest(manifest_json(renames={"100% @1/a@b%.txt": "{{name}} @2 100%.txt", "100% @1": "п@%"}),
                           paths)
        self.assertEqual(set(m.renames), set(paths))
        template = self.root / "t"
        (template / "100% @1").mkdir(parents=True)
        (template / "100% @1" / "a@b%.txt").write_bytes(b"data")
        plan = build_plan(m, {"name": "x"}, template_paths=paths, today=TODAY)
        self.assertTrue(plan.ok, plan.errors)
        apply_plan(plan, template, self.root / "out")
        self.assertEqual((self.root / "out" / "п@%" / "x @2 100%.txt").read_bytes(), b"data")

    # -- длина пути
    def plan_for_length(self, paths, base_len, staging_extra=0):
        base = "C:\\" + "п" * (base_len - 3)
        return build_plan(self.manifest(), {"name": "x"}, template_paths=paths, base_dir=base, today=TODAY,
                          staging_extra=staging_extra)

    def test_directory_limit_is_248(self):
        paths = TemplatePaths(["d"], {"d"})           # путь папки = база + 1 + 8 + 1 + 1
        self.assertTrue(self.plan_for_length(paths, 236).ok)
        plan = self.plan_for_length(paths, 237)
        self.assertFalse(plan.ok)
        self.assertIn("папка", plan.errors[0])

    def test_file_limit_is_260(self):
        ok = self.plan_for_length(TemplatePaths(["ф" * 19], ()), 230)
        self.assertEqual(ok.longest_path, 259)
        self.assertTrue(ok.ok, ok.errors)
        bad = self.plan_for_length(TemplatePaths(["ф" * 20], ()), 230)
        self.assertEqual(bad.longest_path, 260)
        self.assertFalse(bad.ok)

    def test_staging_extra_counts_temporary_folder(self):
        paths = TemplatePaths(["d"], {"d"})
        self.assertTrue(self.plan_for_length(paths, 236).ok)
        self.assertFalse(self.plan_for_length(paths, 236, staging_extra=len(".") + len(".creating")).ok)

    def test_empty_template_paths_give_warning(self):
        plan = build_plan(self.manifest(), {"name": "x"}, today=TODAY)
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.warnings), 1)

    def test_list_template_paths_knows_directories(self):
        template = make_template(self.root / "t")
        paths = list_template_paths(template)
        self.assertIn("docs", paths.dirs)
        self.assertIn("docs/Specification", paths.dirs)
        self.assertNotIn("README.txt", paths.dirs)

    # -- apply_plan: отмена и откат
    def simple_plan(self, template):
        return build_plan(load_manifest(template), {"name": "x"}, template_paths=list_template_paths(template),
                          today=TODAY)

    def test_cancelled_before_copy_leaves_nothing(self):
        template = make_template(self.root / "t")
        token = CancelToken()
        token.cancel()
        with self.assertRaises(SvnError) as cm:
            apply_plan(self.simple_plan(template), template, self.root / "out", token)
        self.assertEqual(cm.exception.kind, ErrorKind.CANCELLED)
        self.assertFalse((self.root / "out").exists())

    def test_cancel_in_the_middle_of_copying(self):
        template = make_template(self.root / "t")
        token = CancelToken()
        real_copy = templates.shutil.copy2
        copied = []

        def copy_then_cancel(src, dst, **kw):
            copied.append(src)
            if len(copied) == 2:
                token.cancel()
            return real_copy(src, dst, **kw)
        with unittest.mock.patch.object(templates.shutil, "copy2", copy_then_cancel):
            with self.assertRaises(SvnError):
                apply_plan(self.simple_plan(template), template, self.root / "out", token)
        self.assertLess(len(copied), 4)
        self.assertFalse((self.root / "out").exists())

    def test_rollback_into_empty_existing_folder_keeps_the_folder(self):
        template = make_template(self.root / "t")
        plan = self.simple_plan(template)
        plan.renames.append(("нет такого", "y"))
        target = self.root / "mine"
        target.mkdir()
        with self.assertRaises(OSError):
            apply_plan(plan, template, target)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_rollback_removes_read_only_files(self):
        template = make_template(self.root / "t")
        plan = self.simple_plan(template)
        for path in template.rglob("*"):
            if path.is_file():
                os.chmod(path, stat.S_IREAD)
        self.addCleanup(lambda: [os.chmod(p, stat.S_IWRITE) for p in template.rglob("*") if p.is_file()])
        plan.renames.append(("нет такого", "y"))
        for target, existed in ((self.root / "new", False), (self.root / "old", True)):
            if existed:
                target.mkdir()
            with self.assertRaises(OSError):
                apply_plan(plan, template, target)
            self.assertEqual(target.exists(), existed)
            if existed:
                self.assertEqual(list(target.iterdir()), [])

    def test_too_long_copy_error_is_translated(self):
        template = make_template(self.root / "t")
        error = OSError(0, "filename too long", None, 206)
        with unittest.mock.patch.object(templates.shutil, "copy2", side_effect=error):
            with self.assertRaisesRegex(TemplateError, "слишком длинным"):
                apply_plan(self.simple_plan(template), template, self.root / "out")
        self.assertFalse((self.root / "out").exists())


class CacheHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        self.client = FakeClient()
        self.client.add("electronics", 10, "Электроника")

    def info(self):
        return list_templates(self.client, ROOT_URL, self.cache)[0]

    def test_requested_revision_is_passed_to_export(self):
        fetch_template(self.client, self.info(), self.cache)
        self.assertEqual(self.client.exports, [("electronics", 10)])

    def test_cache_names_are_short_and_id_is_in_meta(self):
        got = fetch_template(self.client, self.info(), self.cache)
        entry = got.path.parent.parent
        self.assertEqual(len(entry.name), 8)
        self.assertEqual(json.loads((entry / "meta.json").read_text(encoding="utf-8"))["id"], "electronics")
        self.assertEqual(got.path.parent.name, "r10")

    def test_parallel_fetches_download_once(self):
        self.client.export_delay = 0.3
        info = self.info()
        results, errors = [], []

        def worker():
            try:
                results.append(fetch_template(self.client, info, self.cache).path)
            except Exception as exc:       # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.client.exports), 1)
        self.assertEqual(len(set(results)), 1)

    def test_old_version_path_stays_valid_after_update(self):
        old = fetch_template(self.client, self.info(), self.cache)
        self.client.templates["electronics"].update(revision=11, files={"b.txt": b"new"})
        new = fetch_template(self.client, self.info(), self.cache)
        self.assertNotEqual(old.path, new.path)
        self.assertEqual((old.path / "a.txt").read_bytes(), b"hello")       # прежняя копия цела
        self.assertEqual((new.path / "b.txt").read_bytes(), b"new")
        self.assertEqual(templates.cached_template(self.cache, "electronics").path, new.path)

    def test_old_versions_are_removed_lazily(self):
        old = fetch_template(self.client, self.info(), self.cache)
        self.client.templates["electronics"].update(revision=11)
        new = fetch_template(self.client, self.info(), self.cache)
        self.assertTrue(old.path.exists())                                   # сразу не удаляется
        past = time.time() - 2 * 24 * 3600
        os.utime(old.path.parent, (past, past))
        templates.cleanup_cache(self.cache)
        self.assertFalse(old.path.parent.exists())
        self.assertTrue(new.path.is_dir())

    def test_stale_staging_folders_removed_fresh_kept(self):
        self.cache.mkdir()
        stale, fresh = self.cache / ".nabc123", self.cache / ".ndef456"
        for folder in (stale, fresh):
            (folder / "files").mkdir(parents=True)
        past = time.time() - 2 * 24 * 3600
        os.utime(stale, (past, past))
        templates.warm_cache(self.client, ROOT_URL, self.cache)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual([t.id for t in templates.cached_templates(self.cache)], ["electronics"])

    def test_cached_templates_skip_dot_folders(self):
        fetch_template(self.client, self.info(), self.cache)
        (self.cache / ".nabc123").mkdir()
        self.assertEqual(len(templates.cached_templates(self.cache)), 1)

    def test_authz_failure_of_one_template_does_not_break_the_list(self):
        self.client.add("secret", 5, "Закрытый")
        real_cat = self.client.cat

        def cat(url, **kw):
            if "secret" in url:
                raise SvnError(ErrorKind.AUTHZ, "E170001 Authorization failed")
            return real_cat(url, **kw)
        self.client.cat = cat
        self.assertEqual([t.id for t in list_templates(self.client, ROOT_URL, self.cache)], ["electronics"])

    def test_cancel_while_listing_propagates(self):
        def cat(url, **kw):
            raise SvnError(ErrorKind.CANCELLED)
        self.client.cat = cat
        with self.assertRaises(SvnError) as cm:
            list_templates(self.client, ROOT_URL, self.cache)
        self.assertEqual(cm.exception.kind, ErrorKind.CANCELLED)

    def test_cancel_while_exporting_leaves_no_trace(self):
        info = self.info()

        def export(*args, **kw):
            raise SvnError(ErrorKind.CANCELLED)
        self.client.export = export
        with self.assertRaises(SvnError):
            fetch_template(self.client, info, self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_too_long_export_error_is_translated(self):
        info = self.info()

        def export(*args, **kw):
            raise SvnError(ErrorKind.OTHER, "svn: E720206: Не удалось создать файл: имя слишком длинное")
        self.client.export = export
        with self.assertRaisesRegex(TemplateError, "слишком длинным"):
            fetch_template(self.client, info, self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])


def _run(*args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(f"{args}: {proc.stderr.decode(errors='replace')}")


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "нет svn/svnadmin")
class RealSvnTemplateTests(unittest.TestCase):
    """Шаблоны в настоящем хранилище (file:///): список, скачивание, побайтовая копия, svn:keywords."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        repo = root / "repo"
        _run("svnadmin", "create", str(repo))
        wc = root / "wc"
        _run("svn", "checkout", repo.as_uri(), str(wc))
        cls.template = make_template(wc / "templates" / "Шаблон проекта @1")
        (wc / "templates" / "обычная папка").mkdir()
        (wc / "templates" / "обычная папка" / "f.txt").write_text("x")
        cls.keyword_file = cls.template / "версия.txt"
        cls.keyword_file.write_bytes(b"$Id$ and $Date$\n")
        _run("svn", "add", "--force", ".", cwd=wc)
        _run("svn", "propset", "svn:keywords", "Id Date", str(cls.keyword_file) + "@")
        _run("svn", "commit", "-m", "init", cwd=wc)
        _run("svn", "update", cwd=wc)       # после commit файл с keywords в рабочей копии раскрыт: читаем байты как в хранилище
        cls.root_url = repo.as_uri() + "/templates"
        cls.client = SvnClient("svn")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_list_fetch_and_apply_byte_for_byte(self):
        cache = Path(self.tmp.name) / "cache"
        infos = list_templates(self.client, self.root_url, cache)
        self.assertEqual([i.id for i in infos], ["Шаблон проекта @1"])      # «обычная папка» без template.json не шаблон
        info = infos[0]
        self.assertEqual(info.title, "Электроника: проект")
        self.assertIsInstance(info.revision, int)
        got = fetch_template(self.client, info, cache)
        # экспорт содержит то же, что закоммичено: пустые папки на месте, байты те же
        source = tree(self.template)
        source.pop("версия.txt")
        exported = tree(got.path)
        self.assertEqual(exported.pop("версия.txt"), b"$Id$ and $Date$\n")   # ключевые слова не раскрыты
        self.assertEqual(exported, source)
        manifest = load_manifest(got.path)
        plan = build_plan(manifest, {"date": "261028", "name": "вафли 100%"}, template_paths=list_template_paths(got.path),
                          today=TODAY)
        self.assertTrue(plan.ok, plan.errors)
        target = Path(self.tmp.name) / "result" / plan.folder
        apply_plan(plan, got.path, target)
        for rel in EMPTY_DIRS:
            self.assertTrue((target / rel).is_dir(), rel)
        self.assertEqual((target / "Шаблон ТЗ.docx").read_bytes(), source["Шаблон ТЗ.docx"])
        self.assertEqual((target / ("README_" + plan.folder + ".txt")).read_bytes(), source["README.txt"])
        self.assertEqual((target / "версия.txt").read_bytes(), b"$Id$ and $Date$\n")
        self.assertEqual(fetch_template(self.client, info, cache).path, got.path)   # та же версия: берётся кэш

    def test_missing_manifest_url_is_not_a_template_and_missing_root_fails(self):
        with self.assertRaises(SvnError):
            list_templates(self.client, self.root_url + "/нет такой", Path(self.tmp.name) / "c2")


if __name__ == "__main__":
    unittest.main()
