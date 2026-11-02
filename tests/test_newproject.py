"""newproject.py (Task AG.0c): ядро «Нового проекта» не тянет tkinter и svnhelper.gui; окно берёт из него прежние имена."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svnhelper import newproject  # noqa: E402
from svnhelper.svn_core import ErrorKind  # noqa: E402


class NewProjectImportTests(unittest.TestCase):
    def test_import_does_not_load_tkinter_or_gui(self):
        """Чистый процесс: после `import svnhelper.newproject` нет ни tkinter, ни svnhelper.gui (в общем процессе тестов
        tkinter уже загружен другими тестами, поэтому проверка идёт в дочернем процессе)."""
        code = (f"import sys, json; sys.path.insert(0, {str(ROOT)!r}); import svnhelper.newproject; "
                "print(json.dumps(sorted(m for m in sys.modules "
                "if m == 'tkinter' or m.startswith('tkinter.') or m == 'svnhelper.gui' or m.startswith('svnhelper.gui.'))))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [])

    def test_dialog_reexports_same_objects(self):
        from svnhelper.gui import new_project_dialog as npd
        for name in ("CreateCancelled", "CreateJob", "creating_path", "recover_create_jobs", "create_project_folder",
                     "LocalCreateResult", "create_local_project", "ServerCreateResult", "ServerCreateFailed",
                     "ServerCreatedNotDownloaded", "create_server_project", "download_project", "publish_summary",
                     "check_base_dir", "LoadedTemplate", "load_template", "undo_text", "_save_job", "ACTION",
                     "NO_LINK_KEEP_TEXT", "NO_RIGHTS_TEXT", "NO_HISTORY_TEXT", "STAGE_DONE", "CREATING_SUFFIX"):
            self.assertIs(getattr(npd, name), getattr(newproject, name), name)


class ErrorToDictTests(unittest.TestCase):
    def test_server_create_failed(self):
        exc = newproject.ServerCreateFailed("Нет связи.", ErrorKind.NO_CONNECTION, "password=SECRET", False, "boom")
        self.assertEqual(exc.to_dict(), {"kind": "no_connection", "rolled_back": False, "message": "Нет связи."})
        self.assertNotIn("SECRET", json.dumps(exc.to_dict(), ensure_ascii=False))

    def test_server_created_not_downloaded(self):
        exc = newproject.ServerCreatedNotDownloaded("svn://srv/k/projects/p", False, OSError("password=SECRET"))
        data = exc.to_dict()
        self.assertEqual((data["kind"], data["rolled_back"], data["message"]),
                         ("created_not_downloaded", None, exc.text))
        self.assertNotIn("SECRET", json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
