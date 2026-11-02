"""Task AG.1: как помощник находит папку проекта и какие папки ему открывать нельзя (ops/resolve.py)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svnhelper import checks, projectmeta                                      # noqa: E402
from svnhelper.config import Config, SettingsStore                              # noqa: E402
from svnhelper.ops import make_context                                          # noqa: E402
from svnhelper.ops.errors import OpsError                                       # noqa: E402
from svnhelper.ops.resolve import from_git_bash, iter_folders, known_projects, resolve_project  # noqa: E402


def make_junction(link: Path, target: Path) -> None:
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if proc.returncode:
        raise unittest.SkipTest("junction создать не удалось: " + proc.stdout.decode("cp866", "replace"))


class Policy:
    mode = "read_only"

    def __init__(self, allowed_dirs=()):
        self.allowed_dirs = tuple(allowed_dirs)

    def describe(self):
        return {"mode": self.mode}


class ResolveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "Проекты с пробелом"
        self.base.mkdir()
        self.store = SettingsStore(self.root / "settings.json")
        self.store.settings.project_dirs = [str(self.base)]
        self.ctx = make_context(client=object(), store=self.store, config=Config())

    def project(self, name, base=None, svn=True, meta=None) -> Path:
        folder = Path(base or self.base) / name
        (folder / (".svn" if svn else "x")).mkdir(parents=True)
        if meta:
            projectmeta.write(folder, projectmeta.new_meta(meta, name))
        return folder

    def refused(self, ref, ctx=None):
        with self.assertRaises(OpsError) as cm:
            resolve_project(ctx or self.ctx, ref)
        self.assertEqual((cm.exception.kind, cm.exception.exit_code), ("path_refused", 6), ref)
        return cm.exception


class ResolveByNameTests(ResolveBase):
    def test_name_is_case_insensitive_and_cyrillic(self):
        folder = self.project("261028_Отбраковщик")
        found = resolve_project(self.ctx, "261028_отбраковщик")
        self.assertEqual((found.name, found.path), ("261028_Отбраковщик", str(folder)))

    def test_folder_with_project_json_only_is_known(self):
        folder = self.project("новый", svn=False, meta="Новый")
        self.assertEqual(resolve_project(self.ctx, "НОВЫЙ").path, str(folder))
        self.assertEqual([p.name for p in known_projects(self.ctx)], ["новый"])

    def test_plain_folder_is_not_a_project_by_name(self):
        (self.base / "просто папка").mkdir()
        with self.assertRaises(OpsError) as cm:
            resolve_project(self.ctx, "просто папка")
        self.assertEqual((cm.exception.kind, cm.exception.exit_code), ("project_not_found", 1))

    def test_name_without_date_prefix_and_part_of_name(self):
        folder = self.project("261028_отбраковщик")
        self.project("261101_делитель")
        self.assertEqual(resolve_project(self.ctx, "отбраковщик").path, str(folder))
        self.assertEqual(resolve_project(self.ctx, "браков").path, str(folder))

    def test_name_from_project_json(self):
        folder = self.project("261028_x", meta="Линия упаковки")
        self.assertEqual(resolve_project(self.ctx, "линия упаковки").path, str(folder))

    def test_same_name_in_two_folders_is_ambiguous(self):
        other = self.root / "Другая папка"
        other.mkdir()
        self.store.settings.project_dirs.append(str(other))
        self.project("отбраковщик")
        self.project("отбраковщик", base=other)
        with self.assertRaises(OpsError) as cm:
            resolve_project(self.ctx, "ОТБРАКОВЩИК")
        self.assertEqual((cm.exception.kind, cm.exception.exit_code), ("ambiguous", 1))
        self.assertEqual(len(cm.exception.data["candidates"]), 2)

    def test_special_characters_in_name(self):
        folder = self.project("Проект @ 100% тест")
        self.assertEqual(resolve_project(self.ctx, "проект @ 100% тест").path, str(folder))

    def test_empty_and_unknown(self):
        for ref in ("", "   "):
            with self.assertRaises(OpsError) as cm:
                resolve_project(self.ctx, ref)
            self.assertEqual(cm.exception.exit_code, 2)
        with self.assertRaises(OpsError) as cm:
            resolve_project(self.ctx, "нет такого")
        self.assertEqual(cm.exception.kind, "project_not_found")

    def test_no_project_dirs_says_so(self):
        self.store.settings.project_dirs = []
        with self.assertRaises(OpsError) as cm:
            resolve_project(self.ctx, "что-то")
        self.assertIn("Папки для проектов не заданы", cm.exception.next_step)

    def test_listing_skips_hidden_and_links(self):
        self.project("обычный")
        self.project(".svnlite")
        target = self.root / "вне"
        (target / ".svn").mkdir(parents=True)
        make_junction(self.base / "ссылка", target)
        self.assertEqual([name for _path, name in iter_folders([str(self.base)])], ["обычный"])
        self.assertEqual([p.name for p in known_projects(self.ctx)], ["обычный"])


class ResolvePathTests(ResolveBase):
    def test_full_path_inside_base(self):
        folder = self.project("261028_отбраковщик")
        for ref in (str(folder), str(folder).upper(), str(folder) + "\\", str(folder).replace("\\", "/"), f'"{folder}"'):
            self.assertEqual(os.path.normcase(resolve_project(self.ctx, ref).path), os.path.normcase(str(folder)), ref)

    def test_git_bash_path_is_converted(self):
        folder = self.project("проект")
        drive, rest = os.path.splitdrive(str(folder))
        bash = "/" + drive[0].lower() + rest.replace("\\", "/")
        self.assertEqual(from_git_bash(bash), drive[0].upper() + ":" + rest)
        self.assertEqual(os.path.normcase(resolve_project(self.ctx, bash).path), os.path.normcase(str(folder)))
        self.assertEqual(from_git_bash("/c/Users/me/проект"), "C:\\Users\\me\\проект")
        self.assertEqual(from_git_bash("/d"), "D:\\")
        self.assertEqual(from_git_bash("проект"), "проект")

    def test_parent_reference_refused(self):
        self.project("проект")
        outside = self.root / "вне"
        outside.mkdir()
        self.refused(str(self.base / "проект" / ".." / ".." / "вне"))
        self.refused(str(self.base / ".." / "вне"))

    def test_unc_and_extended_paths_refused(self):
        for ref in ("\\\\server\\share\\проект", "//server/share/проект", "\\\\?\\" + str(self.base / "проект"),
                    "\\\\?\\UNC\\server\\share\\x"):
            self.refused(ref)

    def test_network_drive_refused(self):
        folder = self.project("проект")
        with mock.patch.object(checks, "drive_type", return_value=checks.DRIVE_REMOTE):
            self.refused(str(folder))

    def test_relative_and_drive_relative_refused(self):
        self.project("проект")
        for ref in ("проект\\под", ".\\проект", "C:проект", "~/проект"):
            self.refused(ref)

    def test_outside_and_base_itself_refused(self):
        outside = self.root / "чужая папка"
        outside.mkdir()
        error = self.refused(str(outside))
        self.assertIn("не лежит в ваших папках", error.message)
        self.refused(str(self.base))
        self.refused(str(self.root))

    def test_nested_deeper_than_one_level_refused(self):
        deep = self.project("проект") / "подпапка"
        deep.mkdir()
        self.refused(str(deep))

    def test_missing_folder_refused(self):
        self.refused(str(self.base / "нет такой"))

    def test_junction_refused(self):
        target = self.root / "настоящая"
        (target / ".svn").mkdir(parents=True)
        link = self.base / "ссылка"
        make_junction(link, target)
        error = self.refused(str(link))
        self.assertIn("ссылкой", error.message)
        with self.assertRaises(OpsError):
            resolve_project(self.ctx, "ссылка")           # по имени тоже: ссылки в список не попадают

    def test_junction_inside_path_cannot_leave_base(self):
        """Путь через ссылку в середине: папка настоящая, но лежит вне папок для проектов."""
        outside = self.root / "вне"
        (outside / "проект").mkdir(parents=True)
        make_junction(self.base / "выход", outside)
        self.refused(str(self.base / "выход" / "проект"))

    def test_base_that_is_junction_is_allowed(self):
        real = self.root / "реальная"
        real.mkdir()
        link = self.root / "линк на папку проектов"
        make_junction(link, real)
        (real / "проект").mkdir()
        self.store.settings.project_dirs = [str(link)]
        self.assertEqual(resolve_project(self.ctx, str(link / "проект")).name, "проект")

    def test_allowed_dirs_from_policy_extend_the_list(self):
        extra = self.root / "дополнительная"
        folder = self.project("проект", base=self._make(extra))
        self.refused(str(folder))
        ctx = make_context(client=object(), store=self.store, config=Config(), policy=Policy([str(extra)]))
        self.assertEqual(resolve_project(ctx, str(folder)).name, "проект")
        self.assertEqual(resolve_project(ctx, "проект").path, str(folder))

    @staticmethod
    def _make(path: Path) -> Path:
        path.mkdir()
        return path


if __name__ == "__main__":
    unittest.main()
