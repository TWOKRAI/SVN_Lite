"""Выпуск новой версии одной командой: python packaging\\release.py

1. поднимает номер версии (последняя цифра) в svnhelper/__init__.py (--keep-version: не поднимать);
2. собирает SVN-lite.exe (окно) и svnlite.exe (консоль для ИИ-помощника) через packaging\\build.bat;
3. сохраняет прежние exe с сервера в out\\old\\;
4. кладёт оба новых exe в папку обновлений и ставит latest_version в svn_helper.json на сервере.
   Окно при самообновлении забирает оба файла (svnlite.exe подменяется без ожидания, см. svnhelper/selfupdate.py).

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
EXE_NAMES = ("SVN-lite.exe", "svnlite.exe")     # оба выпускаются и обновляются вместе (Task AG.7)
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


def publish_exes(dist: Path, dest: Path, old_dir: Path, old_version: str) -> list:
    """Копирует собранные exe из dist в папку обновлений dest; прежние файлы с сервера уходят в old_dir.

    Сначала проверяются оба собранных файла: нет одного, на сервер не копируется ничего (версии не должны разойтись).
    Возвращает пути файлов в папке обновлений."""
    missing = [name for name in EXE_NAMES if not (dist / name).is_file()]
    if missing:
        sys.exit(f"После сборки нет файлов: {', '.join(missing)} в {dist}. На сервер ничего не копировал.")
    copied = []
    for name in EXE_NAMES:
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
    args = parser.parse_args()

    if not args.dest.is_dir():
        sys.exit(f"Папка обновлений недоступна: {args.dest}")
    old_version = current_version()
    version = old_version if args.keep_version else bump_version()
    print(f"Версия: {old_version} -> {version}")

    if subprocess.run(["cmd", "/c", str(ROOT / "packaging" / "build.bat")], cwd=ROOT).returncode != 0:
        sys.exit("Сборка не удалась, на сервер ничего не копировал.")

    copied = publish_exes(ROOT / "out" / "dist", args.dest, ROOT / "out" / "old", old_version)
    config = args.dest / "svn_helper.json"
    if config.exists():
        set_latest_version(config, version)
    print(f"Готово: {', '.join(str(path) for path in copied)} (latest_version = {version})")


if __name__ == "__main__":
    main()
