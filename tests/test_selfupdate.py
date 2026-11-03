"""Самообновление: поиск файла, копирование рядом с exe, bat-скрипт подмены (по-настоящему, на пустышках)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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


# ---- svnlite.exe рядом с окном (Task AG.7) ----

CLI = selfupdate.CLI_EXE_NAME


def make_install(tmp_path, monkeypatch, with_cli_on_server=True, cli_body=MZ_BODY):
    """Папка обновлений на «сервере» и папка установки с запущенным «окном» (пустышки)."""
    server = tmp_path / "server"
    server.mkdir()
    (server / selfupdate.EXE_NAME).write_bytes(MZ_BODY)
    if with_cli_on_server:
        (server / CLI).write_bytes(cli_body)
    app = tmp_path / "app"
    app.mkdir()
    running = app / selfupdate.EXE_NAME
    running.write_bytes(b"old")
    (app / CLI).write_bytes(b"old cli")
    monkeypatch.setattr(selfupdate, "is_supported", lambda: True)
    monkeypatch.setattr(sys, "executable", str(running))
    return server, app


def test_prepare_copies_cli_next_to_running_exe(tmp_path, monkeypatch):
    server, app = make_install(tmp_path, monkeypatch)
    prepared = selfupdate.prepare(str(server))
    assert prepared.extras == [(str(app / (CLI + ".new")), str(app / CLI))]
    assert (app / (CLI + ".new")).stat().st_size == len(MZ_BODY)
    assert (app / CLI).read_bytes() == b"old cli"                # сам файл не тронут до подмены


def test_prepare_without_cli_on_server_updates_window_only(tmp_path, monkeypatch):
    server, app = make_install(tmp_path, monkeypatch, with_cli_on_server=False)
    prepared = selfupdate.prepare(str(server))
    assert prepared.extras == []
    assert not (app / (CLI + ".new")).exists()


def test_prepare_with_corrupt_cli_still_prepares_window(tmp_path, monkeypatch):
    server, app = make_install(tmp_path, monkeypatch, cli_body=b"not an exe" * 200_000)
    prepared = selfupdate.prepare(str(server))
    assert os.path.exists(prepared.new_path) and prepared.extras == []
    assert not (app / (CLI + ".new")).exists()                   # мусор не остаётся


def test_swap_script_without_extras_is_unchanged_in_effect():
    script = selfupdate.swap_script("a.new", "a.exe")
    assert CLI not in script and script.count("move /y") == 1


def test_swap_script_drops_cli_files_when_window_swap_fails():
    """Окно подменить не удалось: новые файлы помощника удаляются, чтобы версии не разошлись."""
    script = selfupdate.swap_script("a.exe.new", "a.exe", (("c.exe.new", "c.exe"),))
    fail, done = script.index(":fail"), script.index(":done")
    assert 'del /q "c.exe.new"' in script[fail:done]
    assert 'move /y "c.exe.new" "c.exe"' in script[done:]


@pytest.mark.skipif(sys.platform != "win32", reason="bat-скрипт только для Windows")
def test_swap_script_replaces_window_and_cli(tmp_path):
    window, window_new = tmp_path / "app.exe", tmp_path / "app.exe.new"
    cli, cli_new = tmp_path / CLI, tmp_path / (CLI + ".new")
    for path, text in ((window, "old"), (window_new, "new"), (cli, "old cli"), (cli_new, "new cli")):
        path.write_text(text)
    script = tmp_path / "update.bat"
    script.write_text(selfupdate.swap_script(str(window_new), str(window), ((str(cli_new), str(cli)),)),
                      encoding="utf-8", newline="")
    subprocess.run(["cmd.exe", "/c", str(script)], check=True, timeout=30, cwd=tmp_path)
    assert (window.read_text(), cli.read_text()) == ("new", "new cli")
    assert not window_new.exists() and not cli_new.exists() and not script.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="bat-скрипт только для Windows")
def test_running_foreign_cli_does_not_block_window_update(tmp_path, monkeypatch):
    """svnlite.exe держит запущенный помощник (имитация: открытый файл). Окно подменяется без ожидания, занятый
    svnlite.exe остаётся прежним, новый ждёт «.new», а при следующем запуске окна подменяется."""
    server, app = make_install(tmp_path, monkeypatch)
    prepared = selfupdate.prepare(str(server))
    script = tmp_path / "update.bat"
    script.write_text(selfupdate.swap_script(prepared.new_path, prepared.target, tuple(prepared.extras)),
                      encoding="utf-8", newline="")
    cli, cli_new = app / CLI, app / (CLI + ".new")
    busy = open(cli, "r+b")                                      # как запущенный svnlite.exe
    try:
        started = time.monotonic()
        subprocess.run(["cmd.exe", "/c", str(script)], check=True, timeout=30, cwd=tmp_path)
        assert time.monotonic() - started < 15                    # не ждали минуту, пока файл освободится
        assert (app / selfupdate.EXE_NAME).stat().st_size == len(MZ_BODY)      # окно подменено
        assert cli.read_bytes() == b"old cli" and cli_new.exists()             # помощник занят: подмена отложена
        assert selfupdate.apply_pending() == []                   # и при запуске окна он ещё занят
        assert cli_new.exists()
    finally:
        busy.close()
    assert selfupdate.apply_pending() == [str(cli)]               # освободился: подменён при запуске окна
    assert cli.stat().st_size == len(MZ_BODY) and not cli_new.exists()


def test_apply_pending_noop_when_nothing_pending_or_not_frozen(tmp_path, monkeypatch):
    server, app = make_install(tmp_path, monkeypatch)
    assert selfupdate.apply_pending() == []                       # «.new» нет
    (app / (CLI + ".new")).write_bytes(MZ_BODY)
    monkeypatch.setattr(selfupdate, "is_supported", lambda: False)
    assert selfupdate.apply_pending() == []                       # запуск из исходников: ничего не трогаем
    assert (app / (CLI + ".new")).exists()


def test_apply_pending_drops_corrupt_file(tmp_path, monkeypatch):
    server, app = make_install(tmp_path, monkeypatch)
    (app / (CLI + ".new")).write_bytes(b"MZ-truncated")
    assert selfupdate.apply_pending() == []
    assert not (app / (CLI + ".new")).exists() and (app / CLI).read_bytes() == b"old cli"


# ---- версии окна и svnlite.exe (doctor) ----

def test_window_version_roundtrip_and_frozen_only(tmp_path, monkeypatch):
    from svnhelper import __version__
    monkeypatch.setenv("SVNLITE_DATA_DIR", str(tmp_path))
    assert selfupdate.window_version() == ""                      # окно ещё не запускалось
    monkeypatch.setattr(selfupdate, "is_supported", lambda: False)
    selfupdate.record_window_version()
    assert selfupdate.window_version() == ""                      # запуск из исходников запись не делает
    monkeypatch.setattr(selfupdate, "is_supported", lambda: True)
    selfupdate.record_window_version()
    assert selfupdate.window_version() == __version__
    (tmp_path / selfupdate.WINDOW_VERSION_FILE).write_text("не json", encoding="utf-8")
    assert selfupdate.window_version() == ""


def doctor_result(tmp_path, monkeypatch, frozen, window):
    from svnhelper.config import Config, SettingsStore
    from svnhelper.ops import make_context, run
    monkeypatch.setenv("SVNLITE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(selfupdate, "is_supported", lambda: frozen)
    if window is not None:
        (tmp_path / selfupdate.WINDOW_VERSION_FILE).write_text(json.dumps({"version": window}), encoding="utf-8")
    ctx = make_context(client=None, store=SettingsStore(tmp_path / "settings.json"), config=Config())
    return run(ctx, "doctor", check_connection=False)


def test_doctor_shows_both_versions_and_flags_mismatch(tmp_path, monkeypatch):
    from svnhelper import __version__
    result = doctor_result(tmp_path, monkeypatch, frozen=True, window="0.0.1")
    program = result.data["program"]
    assert (program["version"], program["cli_version"], program["window_version"]) == (__version__, __version__, "0.0.1")
    assert program["versions_match"] is False
    assert "Версии программы разошлись: окно 0.0.1" in result.text
    assert any("разошлись" in w for w in result.warnings)


def test_doctor_same_versions_or_unknown_window_is_quiet(tmp_path, monkeypatch):
    from svnhelper import __version__
    for window in (__version__, None):
        result = doctor_result(tmp_path, monkeypatch, frozen=True, window=window)
        assert result.data["program"]["versions_match"] is True
        assert "разошлись" not in result.text and not any("разошлись" in w for w in result.warnings)


def test_doctor_from_source_shows_package_version_only(tmp_path, monkeypatch):
    from svnhelper import __version__
    result = doctor_result(tmp_path, monkeypatch, frozen=False, window="0.0.1")
    assert result.data["program"] == {"version": __version__}
    assert "разошлись" not in result.text


# ---- release.py ----

def test_release_publishes_both_exes_and_keeps_previous(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging"))
    try:
        import release
    finally:
        sys.path.pop(0)
    dist, dest, old = tmp_path / "dist", tmp_path / "update", tmp_path / "old"
    dist.mkdir()
    dest.mkdir()
    for name in release.EXE_NAMES:
        (dist / name).write_bytes(b"new " + name.encode())
    (dest / "SVN-lite.exe").write_bytes(b"prev window")           # на сервере уже лежит прежний выпуск окна
    copied = release.publish_exes(dist, dest, old, "1.2.8")
    assert [p.name for p in copied] == ["SVN-lite.exe", "svnlite.exe"]
    assert (dest / "SVN-lite.exe").read_bytes() == b"new SVN-lite.exe"
    assert (dest / "svnlite.exe").read_bytes() == b"new svnlite.exe"
    assert (old / "SVN-lite.prev-1.2.8.exe").read_bytes() == b"prev window"
    assert not (old / "svnlite.prev-1.2.8.exe").exists()          # прежнего svnlite.exe на сервере не было


def test_release_copies_nothing_if_one_exe_is_missing(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging"))
    try:
        import release
    finally:
        sys.path.pop(0)
    dist, dest = tmp_path / "dist", tmp_path / "update"
    dist.mkdir()
    dest.mkdir()
    (dist / "SVN-lite.exe").write_bytes(b"x")
    with pytest.raises(SystemExit):
        release.publish_exes(dist, dest, tmp_path / "old", "1.2.8")
    assert list(dest.iterdir()) == []


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
