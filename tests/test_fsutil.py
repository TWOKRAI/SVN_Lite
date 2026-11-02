"""Удаление недокачанной папки: ссылки (junction/symlink) и повтор после отказа в доступе."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svnhelper import fsutil  # noqa: E402


def make_junction(link: str, target: str) -> bool:
    """Создаёт junction через mklink /J (права администратора не нужны). False, если не получилось."""
    if sys.platform != "win32":
        return False
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
    return proc.returncode == 0 and os.path.isdir(link)


class RemovePartialDirTests(unittest.TestCase):
    def test_junction_is_refused_and_target_survives(self):
        """На Python 3.10/3.11 os.path.isjunction нет: ссылку надо узнавать по reparse point."""
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "base")
            outside = os.path.join(tmp, "outside")
            os.makedirs(base)
            os.makedirs(outside)
            precious = Path(outside, "важный файл.txt")
            precious.write_text("не удалять")
            link = os.path.join(base, "proj")
            if not make_junction(link, outside):
                self.skipTest("не удалось создать junction (mklink /J)")
            try:
                with self.assertRaises(ValueError):
                    fsutil.remove_partial_dir(link, base)
                self.assertTrue(precious.exists())            # содержимое цели цело
                self.assertTrue(os.path.lexists(link))
            finally:
                os.rmdir(link)                                # удаляет только саму ссылку

    def test_readonly_files_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "base")
            dest = os.path.join(base, "proj")
            os.makedirs(os.path.join(dest, "sub"))
            locked = os.path.join(dest, "sub", "чертёж.dwg")
            Path(locked).write_text("x")
            os.chmod(locked, stat.S_IREAD)
            fsutil.remove_partial_dir(dest, base)
            self.assertFalse(os.path.exists(dest))

    def test_other_errors_are_not_swallowed(self):
        """Ошибка, которую повтором не исправить, должна дойти до вызывающего (и до сообщения пользователю)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "base")
            dest = os.path.join(base, "proj")
            os.makedirs(dest)
            Path(dest, "a.txt").write_text("x")
            err = PermissionError(13, "занято")
            with unittest.mock.patch("shutil.rmtree", side_effect=err), self.assertRaises(PermissionError):
                fsutil.remove_partial_dir(dest, base)
            # обработчик: чужая функция или не PermissionError — пробрасывается
            with self.assertRaises(OSError):
                fsutil._make_writable_and_retry(os.listdir, dest, OSError("диск"))
            with self.assertRaises(FileNotFoundError):
                fsutil._make_writable_and_retry(os.unlink, dest, FileNotFoundError())

    def test_leftover_after_rmtree_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "base")
            dest = os.path.join(base, "proj")
            os.makedirs(dest)
            with unittest.mock.patch("shutil.rmtree"), self.assertRaises(OSError):
                fsutil.remove_partial_dir(dest, base)         # rmtree «тихо» ничего не удалил


if __name__ == "__main__":
    unittest.main()
