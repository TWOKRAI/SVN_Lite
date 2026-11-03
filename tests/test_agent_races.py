"""Task AG.7, шаг 1: гонки между окном SVN-lite, консольной программой svnlite и вторым процессом.

Выдача сейчас M1 (только чтение), поэтому здесь то, что существует: замок папки (окно и CLI на одной папке),
одновременная запись settings.json, журнал действий помощника (ops/audit) и счётчик записей политики (agent_policy),
журнал svn_helper.log, который CLI не трогает, и сквозное чтение на настоящем svnserve.

Участники гонки - настоящие процессы (`subprocess`): CLI это `python -m svnhelper.cli` с SVNLITE_DATA_DIR во временной
папке, фейковый svn (tests/fake_svn.py) подключён через svn.bat в настройках, как в tests/test_cli.py. Ожиданий «на
удачу» нет: процессы синхронизируются строкой «ready» в stdout и строкой «go» в stdin; пауза в писателях настроек
нужна лишь затем, чтобы их работа шла дольше запуска CLI (на результат проверок она не влияет).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svnhelper import folderlock, publish                                    # noqa: E402
from svnhelper.config import SettingsStore                                   # noqa: E402
from svnhelper.ops import texts                                              # noqa: E402

FAKE_SCRIPT = ROOT / "tests" / "fake_svn.py"
NAME = "261028_отбраковщик"
WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------
# Дочерние процессы
# --------------------------------------------------------------------------

# Держит папку, пока ему не напишут строку в stdin или не убьют: mode = mark_busy (как окно) | hold
HOLDER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from svnhelper import folderlock, publish
mode, target = sys.argv[2], sys.argv[3]
guard = publish.mark_busy(target) if mode == "mark_busy" else folderlock.hold(target, owner="второй процесс")
with guard:
    print("ready", flush=True)
    sys.stdin.readline()
"""

# Берёт и отпускает замок папки много раз подряд; сообщает, сколько раз взять не удалось
FLAP = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from svnhelper import folderlock
target, cycles = sys.argv[2], int(sys.argv[3])
failed = 0
for _ in range(cycles):
    lock = folderlock.try_lock(target, owner="flap")
    if lock is None:
        failed += 1
    else:
        time.sleep(0.002)
        lock.release()
print(json.dumps({"cycles": cycles, "failed": failed}), flush=True)
"""

# Пишет в settings.json через SettingsStore.update (свои ключи local_histories); начинает по строке «go» в stdin
SETTINGS_WRITER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from svnhelper.config import SettingsStore
store = SettingsStore(Path(sys.argv[2]))
tag, count, pause = sys.argv[3], int(sys.argv[4]), float(sys.argv[5])
print("ready", flush=True)
sys.stdin.readline()
for i in range(count):
    key = f"c:\\{tag}\\{i}"
    store.update(lambda s, k=key, v=f"d:\\h\\{tag}{i}": s.local_histories.__setitem__(k, v))
    time.sleep(pause)
print("done", flush=True)
"""

# Пишет строки журнала действий помощника через ops.audit.record; начинает по строке «go»
AUDIT_WRITER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from types import SimpleNamespace
from svnhelper.ops import audit
tag, count = sys.argv[2], int(sys.argv[3])
ctx = SimpleNamespace(client_name="mcp", client_info="гонка", settings=SimpleNamespace(username="ivanov"))
print("ready", flush=True)
sys.stdin.readline()
written = 0
for i in range(count):
    written += bool(audit.record(ctx, "writer_" + tag, "ok", read_only=True, project=f"{tag}{i}",
                                 comment="комментарий\nс переводом строки и «кавычками»"))
print(json.dumps({"written": written}), flush=True)
"""

# Считает записи под лимитом через Policy.check(consume=True): сколько прошло и сколько отказано
COUNTER_WRITER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from svnhelper.agent_policy import Policy
from svnhelper.ops.errors import OpsError
limit, count = int(sys.argv[2]), int(sys.argv[3])
policy = Policy(file_mode="write", ceiling="write", allow=("save_local",), signature="ok", max_writes_per_hour=limit)
print("ready", flush=True)
sys.stdin.readline()
passed = denied = 0
for _ in range(count):
    try:
        policy.check("save_local", consume=True)
        passed += 1
    except OpsError as exc:
        assert exc.kind == "policy_denied", exc.kind
        denied += 1
print(json.dumps({"passed": passed, "denied": denied}), flush=True)
"""


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def stop(proc: subprocess.Popen) -> None:
    """Убивает процесс (если жив) и закрывает его каналы."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


@contextlib.contextmanager
def exclusive_open(path: Path):
    """Файл открыт без права делить (dwShareMode=0), как у программы, которая его сейчас записывает или проверяет:
    любое другое открытие даёт PermissionError (ошибка 32)."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "файл не открыт для проверки")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


class RaceBase(unittest.TestCase):
    """Своя папка данных, папка проектов с проектом NAME и настройки с фейковым svn; процесс тестов смотрит в ту же папку."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.base = self.root / "Проекты"
        self.proj = self.base / NAME
        (self.proj / ".svn").mkdir(parents=True)
        self.bat = self.root / "svn.bat"
        self.bat.write_text(f'@echo off\r\n"{sys.executable}" "{FAKE_SCRIPT}" %*\r\n', encoding="ascii")
        self.settings_path = self.data / "settings.json"
        self.svn_log = self.root / "calls.jsonl"
        env = mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(folderlock.set_strict, False)
        self.write_settings()

    def write_settings(self, svn_path: str = None, extra: dict = None) -> None:
        store = SettingsStore(self.settings_path)
        store.settings.project_dirs = [str(self.base)]
        store.settings.svn_path = svn_path or str(self.bat)
        for key, value in (extra or {}).items():
            setattr(store.settings, key, value)
        store.save()

    # -- процессы ---------------------------------------------------------------------------------

    def env(self, mode: str = "", svn_log: bool = False) -> dict:
        env = dict(os.environ, SVNLITE_DATA_DIR=str(self.data), PYTHONIOENCODING="utf-8")
        env.pop("FAKE_SVN_MODE", None)
        env.pop("FAKE_SVN_LOG", None)
        if mode:
            env["FAKE_SVN_MODE"] = mode
        if svn_log:
            env["FAKE_SVN_LOG"] = str(self.svn_log)
        return env

    def spawn(self, code: str, *args, ready: bool = True) -> subprocess.Popen:
        """Дочерний python с кодом `code`; argv[1] - корень проекта. ready: дождаться строки «ready» в stdout."""
        proc = subprocess.Popen([sys.executable, "-c", code, str(ROOT), *map(str, args)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        self.addCleanup(stop, proc)
        if ready:
            line = _decode(proc.stdout.readline()).strip()
            if line != "ready":
                stop(proc)
                self.fail(f"дочерний процесс не готов: {line!r}")
        return proc

    def holder(self, mode: str = "mark_busy") -> subprocess.Popen:
        return self.spawn(HOLDER, mode, self.proj)

    @staticmethod
    def go(*procs: subprocess.Popen) -> None:
        for proc in procs:
            proc.stdin.write(b"go\n")
            proc.stdin.flush()

    def finish(self, proc: subprocess.Popen, timeout: float = 60) -> dict:
        """Ждёт завершения писателя, возвращает его последнюю строку stdout как JSON (код возврата 0 обязателен)."""
        out, err = proc.communicate(timeout=timeout)
        self.assertEqual(proc.returncode, 0, _decode(err))
        lines = [line for line in _decode(out).splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines and lines[-1].startswith("{") else {}

    def start_cli(self, *argv, mode: str = "", svn_log: bool = False) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-m", "svnhelper.cli", *argv], cwd=ROOT,
                                env=self.env(mode, svn_log), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        self.addCleanup(stop, proc)
        return proc

    def cli_result(self, proc: subprocess.Popen, timeout: float = 120) -> tuple:
        """(код возврата, ответ как JSON). stderr в JSON-режиме обязан быть пустым."""
        out, err = proc.communicate(timeout=timeout)
        self.assertEqual(_decode(err), "")
        return proc.returncode, json.loads(_decode(out))

    def cli(self, *argv, mode: str = "", svn_log: bool = False) -> tuple:
        return self.cli_result(self.start_cli(*argv, mode=mode, svn_log=svn_log))

    def status_row(self, **kw) -> tuple:
        code, data = self.cli("status", NAME, **kw)
        self.assertEqual((code, data["ok"]), (0, True), data["text"])
        return data["data"]["projects"][0], data

    def svn_calls(self) -> list:
        if not self.svn_log.exists():
            return []
        return [json.loads(line) for line in self.svn_log.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------
# (а) Окно и CLI на одной папке
# --------------------------------------------------------------------------

@unittest.skipUnless(WINDOWS, "замок папки и svn.bat рассчитаны на Windows")
class BusyFolderTests(RaceBase):
    """Папку занял другой процесс: CLI отвечает «занята» предупреждением (код 0), а не ошибкой; после kill замка нет."""

    def test_cli_status_answers_busy_with_warning_not_error(self):
        self.holder()
        row, data = self.status_row()
        self.assertTrue(row["busy"])
        self.assertEqual(row["next_action"], "wait")
        self.assertIn("program", row["busy_by"])
        self.assertIn(texts.WARN_BUSY_SKIPPED.format(name=NAME), data["warnings"])

    def test_cli_does_not_ask_the_server_about_a_busy_folder(self):
        self.holder()
        row, _data = self.status_row(svn_log=True)
        self.assertFalse(row["server_checked"])
        self.assertTrue(self.svn_calls())                            # локальное состояние читалось
        for call in self.svn_calls():
            self.assertFalse({"-u", "--show-updates"} & set(call["args"]), call["args"])

    def test_cli_still_shows_local_changes_of_a_busy_folder(self):
        self.holder()
        row, _data = self.status_row()
        self.assertGreater(row["local_changes"], 0)

    def test_cli_other_read_commands_warn_that_data_may_be_incomplete(self):
        self.holder()
        for command in ("changes", "history", "locks"):
            with self.subTest(command=command):
                code, data = self.cli(command, NAME)
                self.assertEqual((code, data["ok"]), (0, True), data["text"])
                self.assertIn(texts.WARN_BUSY_READ.format(name=NAME), data["warnings"])

    def test_cli_exit_code_stays_zero_for_a_busy_folder(self):
        self.holder("hold")                       # второй способ занять: folderlock.hold вместо mark_busy
        code, data = self.cli("status", NAME)
        self.assertEqual((code, data["ok"], data["data"]["projects"][0]["busy"]), (0, True, True))

    def test_cli_says_free_when_nobody_holds_the_folder(self):
        row, data = self.status_row()
        self.assertFalse(row["busy"])
        self.assertIsNone(row["busy_by"])
        self.assertNotIn(texts.WARN_BUSY_SKIPPED.format(name=NAME), data["warnings"])

    def test_killed_holder_leaves_the_folder_free_for_cli(self):
        proc = self.holder()
        self.assertTrue(self.status_row()[0]["busy"])
        proc.kill()
        proc.wait()
        row, data = self.status_row()
        self.assertFalse(row["busy"])
        self.assertNotIn(texts.WARN_BUSY_SKIPPED.format(name=NAME), data["warnings"])

    def test_killed_holder_leaves_the_folder_free_for_the_window(self):
        proc = self.holder()
        self.assertTrue(publish.is_busy(str(self.proj)))
        proc.kill()
        proc.wait()
        self.assertFalse(publish.is_busy(str(self.proj)))
        with publish.mark_busy(str(self.proj)):                        # окно берёт папку без отказа
            self.assertTrue(publish.is_busy(str(self.proj)))

    def test_released_holder_leaves_the_folder_free(self):
        proc = self.holder()
        self.go(proc)                                                  # строка в stdin: держатель выходит из блока
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertFalse(self.status_row()[0]["busy"])

    def test_window_busy_mark_in_this_process_is_seen_by_cli(self):
        with publish.mark_busy(str(self.proj)):
            row, data = self.status_row()
            self.assertTrue(row["busy"])
            self.assertIn(texts.WARN_BUSY_SKIPPED.format(name=NAME), data["warnings"])
        self.assertFalse(self.status_row()[0]["busy"])                 # окно закончило: CLI папку свободной видит

    def test_window_refuses_in_russian_while_another_process_holds_the_folder(self):
        self.holder("hold")
        with self.assertRaises(publish.PublishError) as caught:
            with publish.mark_busy(str(self.proj)):
                pass
        self.assertEqual(caught.exception.code, publish.IN_PROGRESS)
        self.assertEqual(caught.exception.message, folderlock.BUSY_TEXT)
        self.assertRegex(caught.exception.message, "[А-Яа-я]")

    def test_window_can_always_take_the_folder_while_cli_reads(self):
        """CLI читает состояние и проверяет замок; окно в это время берёт и отпускает папку: отказов быть не должно."""
        clients = [self.start_cli("status", NAME) for _ in range(3)]
        taken = 0
        while any(proc.poll() is None for proc in clients) or taken == 0:
            with publish.mark_busy(str(self.proj)):                    # PublishError здесь провалит тест
                taken += 1
        for proc in clients:
            code, data = self.cli_result(proc)
            self.assertEqual((code, data["ok"]), (0, True), data["text"])

    def test_probes_never_make_a_lock_holder_fail(self):
        """Окно и CLI проверяют замок (held_by_other) в цикле; процесс, который берёт и отпускает его, не должен ни разу
        получить отказ из-за чужой проверки."""
        stop_probe = threading.Event()
        probes, failures = [], []

        def probe():
            while not stop_probe.is_set():
                try:
                    probes.append(folderlock.held_by_other(str(self.proj)))
                except Exception as exc:                                # noqa: BLE001  (любое исключение - дефект)
                    failures.append(repr(exc))

        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        try:
            proc = self.spawn(FLAP, self.proj, 150, ready=False)
            result = self.finish(proc)
        finally:
            stop_probe.set()
            thread.join(30)
        self.assertEqual(failures, [])
        self.assertTrue(probes)
        self.assertEqual(result, {"cycles": 150, "failed": 0})
        self.assertFalse(folderlock.held_by_other(str(self.proj)))


# --------------------------------------------------------------------------
# (б) Одновременная запись settings.json
# --------------------------------------------------------------------------

@unittest.skipUnless(WINDOWS, "замок настроек на msvcrt и svn.bat рассчитаны на Windows")
class SettingsRaceTests(RaceBase):
    """Два процесса пишут local_histories через update, CLI в это время читает те же настройки."""

    WRITES = 30
    READER_PAUSE = 0.005    # пауза между чтениями; читатель без паузы держит файл почти всегда и мешает замене файла у писателя
    PAUSE = 0.03        # пауза между записями писателя: его работа идёт дольше запуска CLI (на проверки не влияет)

    def run_writers(self, readers=None) -> tuple:
        writers = [self.spawn(SETTINGS_WRITER, self.settings_path, tag, self.WRITES, self.PAUSE) for tag in ("a", "b")]
        self.go(*writers)
        started = readers() if readers else None
        for proc in writers:
            self.finish(proc)
        return started

    def saved(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def test_both_series_survive_while_cli_reads(self):
        procs = self.run_writers(lambda: [self.start_cli("status", NAME), self.start_cli("projects"),
                                          self.start_cli("changes", NAME)])
        for proc in procs:
            self.cli_result(proc)
        histories = self.saved()["local_histories"]
        expected = {f"c:\\{tag}\\{i}": f"d:\\h\\{tag}{i}" for tag in "ab" for i in range(self.WRITES)}
        self.assertEqual(histories, expected)

    def test_settings_file_is_valid_json_and_keeps_other_fields_after_race(self):
        procs = self.run_writers(lambda: [self.start_cli("status", NAME), self.start_cli("projects")])
        for proc in procs:
            self.cli_result(proc)
        data = self.saved()
        self.assertEqual(data["project_dirs"], [str(self.base)])
        self.assertEqual(data["svn_path"], str(self.bat))

    def test_no_temporary_file_is_left_behind(self):
        self.run_writers()
        self.assertFalse(self.settings_path.with_suffix(".tmp").exists())

    def test_cli_answers_are_valid_and_find_the_project_during_the_race(self):
        """Пока писатели меняют файл, каждый запуск CLI читает настройки: проект должен находиться (код 0)."""
        procs = self.run_writers(lambda: [self.start_cli("status", NAME) for _ in range(4)])
        for proc in procs:
            code, data = self.cli_result(proc)
            self.assertEqual((code, data["ok"]), (0, True), data["text"])
            self.assertEqual(data["data"]["projects"][0]["name"], NAME)

    def test_cli_does_not_rewrite_settings_it_only_reads(self):
        """Если бы CLI записал свою (устаревшую) копию, часть записей писателей пропала бы: сверяем и итог, и mtime без писателей."""
        before = (self.settings_path.read_bytes(), self.settings_path.stat().st_mtime_ns)
        for argv in (["status", NAME], ["projects"], ["changes", NAME]):
            self.cli(*argv)
        self.assertEqual((self.settings_path.read_bytes(), self.settings_path.stat().st_mtime_ns), before)

    def test_reader_never_sees_a_torn_or_shrinking_file(self):
        """Файл заменяется целиком: читатель в цикле видит только полный JSON, а число записей не уменьшается.
        Отказ открыть файл в момент замены (PermissionError) здесь допускается и считается отдельно: это предмет
        тестов ниже; проверяется содержимое, которое всё-таки прочиталось."""
        sizes, torn, denied = [], [], []
        stop_read = threading.Event()

        def reader():
            while not stop_read.is_set():
                try:
                    data = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
                except PermissionError:
                    denied.append(1)
                except ValueError as exc:
                    torn.append(repr(exc))
                else:
                    sizes.append(len(data["local_histories"]))
                time.sleep(self.READER_PAUSE)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            self.run_writers()
        finally:
            stop_read.set()
            thread.join(30)
        self.assertEqual(torn, [])
        self.assertTrue(sizes)
        self.assertEqual(sizes, sorted(sizes))

    # Был дефект (svnhelper/config.py, SettingsStore._load): чтение settings.json при старте не повторялось; файл в этот
    # миг занят (замена у писателя, антивирус), OSError глотался, и возвращались пустые Settings(): проекты «пропадали»,
    # CLI отвечал «проект не найден». Вживую это бывало примерно в одном чтении из ~150 во время сохранения настроек
    # окном. Исправлено: чтение повторяется, а если файл так и не прочитан, это явная ошибка (SettingsUnreadable).
    # Здесь файл занят детерминированно на всё время вызова.
    def test_store_does_not_silently_return_empty_settings_when_file_is_held(self):
        with exclusive_open(self.settings_path):
            try:
                settings = SettingsStore(self.settings_path).settings
            except OSError:
                return                                              # явная ошибка вместо пустых настроек допустима
        self.assertEqual(settings.project_dirs, [str(self.base)])

    def test_cli_does_not_answer_project_not_found_when_settings_file_is_held(self):
        with exclusive_open(self.settings_path):
            code, data = self.cli("status", NAME)
        self.assertNotEqual((data.get("error") or {}).get("kind"), "project_not_found", data["text"])


# --------------------------------------------------------------------------
# (в) Журнал действий помощника и счётчик записей
# --------------------------------------------------------------------------

@unittest.skipUnless(WINDOWS, "замки данных на msvcrt и svn.bat рассчитаны на Windows")
class AuditRaceTests(unittest.TestCase):
    """Два процесса пишут журнал (ops.audit.record), CLI в это же время пишет свои строки. Гонка проходит один раз на
    класс (setUpClass), проверки читают её итог."""

    WRITES = 30
    CLI_CALLS = (("status", NAME), ("projects",))

    @classmethod
    def setUpClass(cls):
        cls.race = RaceBase()
        cls.race.setUp()
        cls.addClassCleanup(cls.race.doCleanups)
        writers = [cls.race.spawn(AUDIT_WRITER, tag, cls.WRITES) for tag in ("a", "b")]
        cls.race.go(*writers)
        clients = [cls.race.start_cli(*argv) for argv in cls.CLI_CALLS]
        cls.written = [cls.race.finish(proc) for proc in writers]
        for proc in clients:
            cls.race.cli_result(proc)

    def raw_lines(self) -> list:
        lines = []
        for path in sorted((self.race.data / "agent").glob("audit_*.jsonl")):
            lines += [line for line in path.read_text(encoding="utf-8").split("\n") if line]
        return lines

    def rows(self) -> list:
        return [json.loads(line) for line in self.raw_lines()]

    def test_every_audit_line_is_valid_json(self):
        for line in self.raw_lines():
            self.assertIsInstance(json.loads(line), dict, line)

    def test_every_writer_reports_all_its_lines_written(self):
        self.assertEqual(self.written, [{"written": self.WRITES}] * 2)

    def test_both_writer_series_are_complete_without_duplicates(self):
        for tag in ("a", "b"):
            projects = [row["project"] for row in self.rows() if row["operation"] == "writer_" + tag]
            self.assertEqual(sorted(projects), sorted(f"{tag}{i}" for i in range(self.WRITES)))

    def test_cli_rows_are_in_the_same_file_one_per_call(self):
        cli_ops = sorted(row["operation"] for row in self.rows() if row["client"] == "cli")
        self.assertEqual(cli_ops, sorted(argv[0] for argv in self.CLI_CALLS))

    def test_total_line_count_is_exact(self):
        self.assertEqual(len(self.raw_lines()), 2 * self.WRITES + len(self.CLI_CALLS))

    def test_audit_lock_is_free_afterwards(self):
        with folderlock.named_lock("audit", 2.0, strict=True):
            pass


@unittest.skipUnless(WINDOWS, "замки данных на msvcrt и svn.bat рассчитаны на Windows")
class CounterRaceTests(RaceBase):
    """Лимит записей в час (`Policy.check(..., consume=True)`) общий для процессов: приращения не теряются."""

    def run_counters(self, limit: int, count: int) -> list:
        procs = [self.spawn(COUNTER_WRITER, limit, count) for _ in range(2)]
        self.go(*procs)
        cli = self.start_cli("status", NAME)
        results = [self.finish(proc) for proc in procs]
        self.cli_result(cli)
        return results

    def stamps(self) -> list:
        return json.loads((self.data / "agent" / "counters.json").read_text(encoding="utf-8"))["writes"]

    def test_no_increment_is_lost_under_a_high_limit(self):
        results = self.run_counters(limit=1000, count=30)
        self.assertEqual([r["passed"] for r in results], [30, 30])
        self.assertEqual(len(self.stamps()), 60)

    def test_limit_holds_exactly_across_two_processes(self):
        results = self.run_counters(limit=40, count=30)
        self.assertEqual(sum(r["passed"] for r in results), 40)         # больше 40 - увеличения потерялись
        self.assertEqual(sum(r["denied"] for r in results), 20)
        self.assertEqual(len(self.stamps()), 40)                        # файл счётчика цел и читается как JSON

    def test_cli_read_does_not_consume_the_limit(self):
        self.run_counters(limit=1000, count=5)
        before = len(self.stamps())
        self.cli("status", NAME)
        self.cli("history", NAME)
        self.assertEqual(len(self.stamps()), before)


# --------------------------------------------------------------------------
# (г) Журнал svn_helper.log
# --------------------------------------------------------------------------

@unittest.skipUnless(WINDOWS, "замки данных на msvcrt и svn.bat рассчитаны на Windows")
class WindowLogTests(RaceBase):
    """svn_helper.log принадлежит окну: CLI не создаёт его и не портит, что бы вокруг ни происходило."""

    MAIN_LOG = "svn_helper.log"

    def storm(self) -> None:
        """Несколько CLI одновременно: занятая папка, нет связи, нет прав, неверные параметры, неизвестный проект."""
        self.holder()
        runs = [self.start_cli("status", NAME), self.start_cli("changes", NAME),
                self.start_cli("incoming", NAME, mode="noconn_remote"), self.start_cli("incoming", NAME, mode="authz_remote"),
                self.start_cli("history"), self.start_cli("changes", "нет_такого_проекта"), self.start_cli("projects")]
        for proc in runs:
            proc.communicate(timeout=120)

    def test_cli_does_not_create_the_window_log(self):
        self.storm()
        self.assertFalse((self.data / self.MAIN_LOG).exists())

    def test_cli_does_not_change_an_existing_window_log(self):
        main_log = self.data / self.MAIN_LOG
        main_log.write_text("окно: строка журнала\n", encoding="utf-8")
        before = (main_log.read_bytes(), main_log.stat().st_mtime_ns)
        self.storm()
        self.assertEqual((main_log.read_bytes(), main_log.stat().st_mtime_ns), before)

    def test_cli_writes_its_own_agent_log(self):
        self.storm()
        self.assertTrue(list((self.data / "logs").glob("svnlite_agent_*.log")))

    def test_agent_log_records_are_not_interleaved_after_concurrent_runs(self):
        """Записи журнала агента (несколько процессов в один файл) не перемешиваются: строка-продолжение допустима только
        после начала записи, а метка времени встречается в строке не более одного раза."""
        self.storm()
        stamp = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} [A-Z]+ \[cli\] \S+: ")
        checked = 0
        for path in (self.data / "logs").glob("svnlite_agent_*.log"):
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines):
                self.assertLessEqual(len(stamp.findall(line)), 1, line)
                if number == 0:
                    self.assertRegex(line, "^" + stamp.pattern)
                if stamp.match(line):
                    checked += 1
        self.assertGreater(checked, 0)

    def test_window_log_file_is_free_to_write_for_the_window(self):
        """CLI не держит открытым ни свой, ни чужой журнал: файл окна после запусков открывается на запись."""
        main_log = self.data / self.MAIN_LOG
        main_log.write_text("окно\n", encoding="utf-8")
        self.storm()
        with open(main_log, "a", encoding="utf-8") as handle:
            handle.write("окно продолжает писать\n")
        self.assertTrue(main_log.read_text(encoding="utf-8").endswith("окно продолжает писать\n"))


# --------------------------------------------------------------------------
# (д) Настоящий svnserve
# --------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _svn(*args, cwd=None) -> None:
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(f"{args}: {proc.stderr.decode(errors='replace')}")


def _wait_port(port: int, server: subprocess.Popen, timeout: float = 20.0) -> None:
    """Ждёт, пока svnserve начнёт принимать соединения (опрос порта, а не пауза)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError("svnserve завершился сразу после запуска")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("svnserve не начал принимать соединения")


@unittest.skipUnless(WINDOWS and shutil.which("svn") and shutil.which("svnadmin") and shutil.which("svnserve"),
                     "нужны Windows и svn, svnadmin, svnserve")
class RealServerBusyReadTests(unittest.TestCase):
    """Сквозное чтение status, changes, history через CLI на настоящем svnserve, пока папку держит другой процесс."""

    PROJECT_URL_PATH = "test/projects/" + quote(NAME) + "/trunk"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.root = root
        repos = root / "repos"
        repos.mkdir()
        repo = repos / "test"
        _svn("svnadmin", "create", str(repo))
        (repo / "conf" / "svnserve.conf").write_text("[general]\nanon-access = write\nauth-access = write\n")
        seed = root / "seed"
        _svn("svn", "checkout", repo.as_uri(), str(seed))
        trunk = seed / "projects" / NAME / "trunk"
        (trunk / "docs").mkdir(parents=True)
        (trunk / "a.txt").write_text("один", encoding="utf-8")
        (trunk / "docs" / "б.txt").write_text("два", encoding="utf-8")
        _svn("svn", "add", "--force", ".", cwd=seed)
        _svn("svn", "commit", "-m", "первое сохранение", "--username", "ivanov", cwd=seed)
        cls.port = _free_port()
        cls.server = subprocess.Popen(["svnserve", "-d", "--foreground", "--listen-port", str(cls.port), "-r", str(repos)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_port(cls.port, cls.server)
        except Exception:
            cls.tearDownClass()
            raise
        cls.url = f"svn://127.0.0.1:{cls.port}/{cls.PROJECT_URL_PATH}"
        cls.base = root / "Проекты"
        cls.base.mkdir()
        cls.mine = cls.base / NAME
        cls.other = root / "коллега"
        _svn("svn", "checkout", cls.url, str(cls.mine))
        _svn("svn", "checkout", cls.url, str(cls.other))
        cls.data = root / "data"
        cls.data.mkdir()
        with mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": str(cls.data)}):
            store = SettingsStore(cls.data / "settings.json")
            store.settings.project_dirs = [str(cls.base)]
            store.settings.svn_path = shutil.which("svn")
            store.save()

    @classmethod
    def tearDownClass(cls):
        server = getattr(cls, "server", None)
        if server is not None and server.poll() is None:
            server.kill()
        if server is not None:
            server.wait()
        cls.tmp.cleanup()

    def cli(self, *argv) -> tuple:
        env = dict(os.environ, SVNLITE_DATA_DIR=str(self.data), PYTHONIOENCODING="utf-8")
        env.pop("FAKE_SVN_MODE", None)
        done = subprocess.run([sys.executable, "-m", "svnhelper.cli", *argv], cwd=ROOT, env=env, timeout=120,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(_decode(done.stderr), "")
        return done.returncode, json.loads(_decode(done.stdout))

    def test_status_changes_history_while_the_folder_is_busy_and_after(self):
        (self.mine / "a.txt").write_text("мои правки", encoding="utf-8")            # правка на компьютере
        (self.other / "docs" / "б.txt").write_text("правка коллеги", encoding="utf-8")
        _svn("svn", "commit", "-m", "Правка коллеги", "--username", "petrov", str(self.other))   # новое на сервере
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT), "mark_busy", str(self.mine)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=dict(os.environ, SVNLITE_DATA_DIR=str(self.data)))
        self.addCleanup(stop, holder)
        self.assertEqual(_decode(holder.stdout.readline()).strip(), "ready")

        # пока папка занята: правки на компьютере видны, сервер не спрашивается, история читается, все ответы с пометкой
        code, status = self.cli("status", NAME)
        row = status["data"]["projects"][0]
        self.assertEqual((code, status["ok"], row["busy"], row["server_checked"], row["local_changes"], row["incoming"]),
                         (0, True, True, False, 1, 0), status["text"])
        self.assertEqual(row["next_action"], "wait")
        self.assertIn(texts.WARN_BUSY_SKIPPED.format(name=NAME), status["warnings"])
        code, changes = self.cli("changes", NAME)
        self.assertEqual((code, changes["ok"]), (0, True), changes["text"])
        self.assertEqual([(f["path"], f["kind"]) for f in changes["data"]["files"]], [("a.txt", "modified")])
        self.assertIn(texts.WARN_BUSY_READ.format(name=NAME), changes["warnings"])
        code, history = self.cli("history", NAME)
        self.assertEqual((code, history["ok"]), (0, True), history["text"])
        self.assertEqual([(e["number"], e["comment"]) for e in history["data"]["entries"]], [(1, "первое сохранение")])
        self.assertIn(texts.WARN_BUSY_READ.format(name=NAME), history["warnings"])

        # держателя убили: замка нет, сервер спрашивается и «новое» от коллеги видно
        holder.kill()
        holder.wait()
        code, status = self.cli("status", NAME)
        row = status["data"]["projects"][0]
        self.assertEqual((code, status["ok"], row["busy"], row["server_checked"], row["state"]),
                         (0, True, False, True, "both"), status["text"])
        self.assertGreaterEqual(row["incoming"], 1)
        self.assertNotIn(texts.WARN_BUSY_SKIPPED.format(name=NAME), status["warnings"])
        with mock.patch.dict(os.environ, {"SVNLITE_DATA_DIR": str(self.data)}):
            self.assertFalse(folderlock.held_by_other(str(self.mine)))


if __name__ == "__main__":
    unittest.main()
