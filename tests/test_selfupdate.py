"""Самообновление: поиск файла, копирование рядом с exe, bat-скрипт подмены (по-настоящему, на пустышках)."""
import os
import subprocess
import sys
import time

import pytest

from svnhelper import selfupdate
from svnhelper.selfupdate import UpdateError

MZ_BODY = b"MZ" + b"\0" * 1_100_000


def test_resolve_folder_and_file(tmp_path):
    exe = tmp_path / selfupdate.EXE_NAME
    exe.write_bytes(MZ_BODY)
    assert selfupdate.resolve_source(str(tmp_path)) == str(exe)
    assert selfupdate.resolve_source(str(exe)) == str(exe)


@pytest.mark.parametrize("bad", ["", "svn://host/x.exe", "https://host/x.exe"])
def test_resolve_rejects_bad_values(bad):
    with pytest.raises(UpdateError):
        selfupdate.resolve_source(bad)


def test_resolve_rejects_non_exe_and_missing(tmp_path):
    other = tmp_path / "a.bat"
    other.write_text("x")
    with pytest.raises(UpdateError):
        selfupdate.resolve_source(str(other))
    with pytest.raises(UpdateError):
        selfupdate.resolve_source(str(tmp_path))          # папка без SVN-lite.exe


def test_prepare_requires_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate, "is_supported", lambda: False)
    with pytest.raises(UpdateError):
        selfupdate.prepare(str(tmp_path))


def test_prepare_copies_next_to_running_exe(tmp_path, monkeypatch):
    server = tmp_path / "server"
    server.mkdir()
    (server / selfupdate.EXE_NAME).write_bytes(MZ_BODY)
    app = tmp_path / "app"
    app.mkdir()
    running = app / "SVN-lite.exe"
    running.write_bytes(b"old")
    monkeypatch.setattr(selfupdate, "is_supported", lambda: True)
    monkeypatch.setattr(sys, "executable", str(running))
    prepared = selfupdate.prepare(str(server))
    assert prepared.target == str(running)
    assert os.path.getsize(prepared.new_path) == len(MZ_BODY)


def test_prepare_rejects_corrupt_file(tmp_path, monkeypatch):
    server = tmp_path / "server"
    server.mkdir()
    (server / selfupdate.EXE_NAME).write_bytes(b"not an exe" * 200_000)
    running = tmp_path / "SVN-lite.exe"
    running.write_bytes(b"old")
    monkeypatch.setattr(selfupdate, "is_supported", lambda: True)
    monkeypatch.setattr(sys, "executable", str(running))
    with pytest.raises(UpdateError):
        selfupdate.prepare(str(server))
    assert not (tmp_path / "SVN-lite.exe.new").exists()   # мусор после отказа не остаётся


@pytest.mark.skipif(sys.platform != "win32", reason="bat-скрипт только для Windows")
def test_swap_script_replaces_file(tmp_path):
    """Скрипт подменяет файл (в том числе в папке с пробелами и кириллицей) и удаляет сам себя."""
    folder = tmp_path / "Папка с пробелом и кириллицей"
    folder.mkdir()
    target = folder / "app.exe"
    target.write_text("old")
    new = folder / "app.exe.new"
    new.write_text("new")
    script = tmp_path / "update.bat"
    script.write_text(selfupdate.swap_script(str(new), str(target)), encoding="utf-8", newline="")
    subprocess.run(["cmd.exe", "/c", str(script)], check=True, timeout=30, cwd=tmp_path)
    assert target.read_text() == "new"
    assert not new.exists()
    assert not script.exists()   # скрипт удаляет сам себя


@pytest.mark.skipif(sys.platform != "win32", reason="bat-скрипт только для Windows")
def test_swap_script_waits_while_target_is_busy(tmp_path):
    target = tmp_path / "app.exe"
    target.write_text("old")
    new = tmp_path / "app.exe.new"
    new.write_text("new")
    script = tmp_path / "update.bat"
    script.write_text(selfupdate.swap_script(str(new), str(target)), encoding="utf-8", newline="")
    busy = open(target, "r+b")           # открытый файл на запись/чтение, как запущенный exe
    try:
        proc = subprocess.Popen(["cmd.exe", "/c", str(script)], cwd=tmp_path)
        time.sleep(2.5)
        assert target.read_text() == "old" and new.exists()   # пока занят, подмены нет
    finally:
        busy.close()
    assert proc.wait(timeout=30) == 0
    assert target.read_text() == "new"


# ---- папка с документацией (info_path) ----

def test_scan_info_folder_lists_only_documents(tmp_path):
    from svnhelper import docs
    for name in ("ШПАРГАЛКА.html", "Инструкция.PDF", "Регламент.docx", "setup.exe", "run.bat", "~$Регламент.docx",
                 ".hidden.pdf", "без_расширения"):
        (tmp_path / name).write_text("x")
    (tmp_path / "подпапка.pdf").mkdir()                       # папка с «документным» именем не считается
    found = docs.scan_info_folder(str(tmp_path))
    assert [e.title for e in found] == ["Инструкция", "Регламент", "ШПАРГАЛКА"]
    assert docs.find_help(found).endswith("ШПАРГАЛКА.html")


def test_scan_info_folder_missing_or_empty():
    from svnhelper import docs
    assert docs.scan_info_folder("") == []
    assert docs.scan_info_folder(r"\нет-такого-сервера\папка") == []
    assert docs.find_help([]) == ""
