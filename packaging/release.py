"""Выпуск новой версии одной командой: python packaging\\release.py

1. поднимает номер версии (последняя цифра) в svnhelper/__init__.py (--keep-version: не поднимать);
2. собирает SVN-lite.exe (окно) и svnlite.exe (консоль для ИИ-помощника) через packaging\\build.bat;
3. сохраняет прежние exe с сервера в out\\old\\;
4. кладёт новое окно в папку обновлений и ставит latest_version в svn_helper.json на сервере.
   svnlite.exe в папку обновлений кладётся только с флагом --with-cli: консольную программу нельзя раздавать на все
   ПК, пока её не согласовали с ИТ и не проверили антивирусом (TODO.md, docs/agent/INSTALL_AGENT.md §7 п.10).
   Окно забирает svnlite.exe при самообновлении, только если он уже лежит рядом с ним (svnhelper/selfupdate.py).
   На сервер сначала копируется svnlite.exe, потом окно: после окна ставится latest_version и начинается обновление.

Папка обновлений берётся из UPDATE_DIR (можно переопределить: --dest <папка>).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "svnhelper" / "__init__.py"
WINDOW_EXE = "SVN-lite.exe"
CLI_EXE = "svnlite.exe"                         # выпускается только с --with-cli (Task AG.7)
UPDATE_DIR = Path(r"\\192.168.11.11\innotech\Automation\common\SVN-Lite")
VERSION_RE = re.compile(r'^(__version__\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', re.M)


def bump_version() -> str:
    text = INIT.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        sys.exit("Не нашёл __version__ в svnhelper/__init__.py")
    major, minor, patch = int(match[2]), int(match[3]), int(match[4]) + 1
    version = f"{major}.{minor}.{patch}"
    INIT.write_text(VERSION_RE.sub(lambda m: f'{m[1]}{version}{m[5]}', text, count=1), encoding="utf-8", newline="")
    return version


def current_version() -> str:
    match = VERSION_RE.search(INIT.read_text(encoding="utf-8"))
    return f"{match[2]}.{match[3]}.{match[4]}" if match else "?"


def set_latest_version(path: Path, version: str) -> None:
    """Меняет только значение latest_version, остальной файл (кодировка, BOM, отступы) остаётся как был."""
    data = path.read_bytes()
    pattern = re.compile(rb'("latest_version"\s*:\s*")[^"]*(")')
    if not pattern.search(data):
        sys.exit(f"В {path} нет поля latest_version")
    path.write_bytes(pattern.sub(lambda m: m[1] + version.encode() + m[2], data, count=1))


def publish_exes(dist: Path, dest: Path, old_dir: Path, old_version: str, with_cli: bool = False) -> list:
    """Копирует собранные exe из dist в папку обновлений dest; прежние файлы с сервера уходят в old_dir.

    Без with_cli выпускается одно окно. Сначала проверяются все выпускаемые файлы: нет одного, на сервер не копируется
    ничего (версии не должны разойтись). Порядок копирования: svnlite.exe, потом окно: окно служит сигналом для
    клиентов (после него ставится latest_version), а к этому моменту svnlite.exe уже должен лежать на месте.
    Возвращает пути файлов в папке обновлений."""
    names = ([CLI_EXE] if with_cli else []) + [WINDOW_EXE]
    missing = [name for name in names if not (dist / name).is_file()]
    if missing:
        sys.exit(f"После сборки нет файлов: {', '.join(missing)} в {dist}. На сервер ничего не копировал.")
    copied = []
    for name in names:
        target = dest / name
        if target.exists():
            old_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, old_dir / f"{target.stem}.prev-{old_version}{target.suffix}")
        shutil.copy2(dist / name, target)
        copied.append(target)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-version", action="store_true", help="не поднимать номер версии")
    parser.add_argument("--dest", type=Path, default=UPDATE_DIR, help="папка обновлений на сервере")
    parser.add_argument("--with-cli", action="store_true",
                        help="положить в папку обновлений ещё и svnlite.exe (по умолчанию выпускается одно окно)")
    args = parser.parse_args()

    if not args.dest.is_dir():
        sys.exit(f"Папка обновлений недоступна: {args.dest}")
    old_version = current_version()
    version = old_version if args.keep_version else bump_version()
    print(f"Версия: {old_version} -> {version}")

    if subprocess.run(["cmd", "/c", str(ROOT / "packaging" / "build.bat")], cwd=ROOT).returncode != 0:
        sys.exit("Сборка не удалась, на сервер ничего не копировал.")

    copied = publish_exes(ROOT / "out" / "dist", args.dest, ROOT / "out" / "old", old_version, args.with_cli)
    if not args.with_cli:
        print(f"Выпущено одно окно: {CLI_EXE} в папку обновлений не копировался (флаг --with-cli, когда ИТ "
              "согласовали раздачу консольной программы).")
    config = args.dest / "svn_helper.json"
    if config.exists():
        set_latest_version(config, version)
    print(f"Готово: {', '.join(str(path) for path in copied)} (latest_version = {version})")


if __name__ == "__main__":
    main()
