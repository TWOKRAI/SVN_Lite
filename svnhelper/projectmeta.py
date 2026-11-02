"""Метаданные проекта: файл project.json в корне главной ветки. Без tkinter.

Формат описан в docs/PLAN_templates.md. Клиент в первой версии читает из файла только название
и шаблон; остальное записывается. Решения («Отправить» показывать или нет) по этому файлу
не принимаются: его можно скопировать вместе с чужой папкой.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("svnhelper.projectmeta")

FILE_NAME = "project.json"
FORMAT = 1


@dataclass
class ProjectMeta:
    name: str
    folder: str
    created: str = ""                       # ISO без часового пояса: 2026-10-28T14:05:00
    author: str = ""
    template: dict[str, Any] = field(default_factory=dict)   # {"id", "title", "revision"}
    fields: dict[str, str] = field(default_factory=dict)
    svn: Optional[dict[str, Any]] = None    # None, пока проекта нет на сервере; потом {"url", "published"}
    links: dict[str, Any] = field(default_factory=dict)      # заготовка, клиент не читает
    format: int = FORMAT
    extra: dict[str, Any] = field(default_factory=dict)      # неизвестные поля: не теряются при записи


_KNOWN = ("format", "name", "folder", "created", "author", "template", "fields", "svn", "links")


def default_author(login: str = "") -> str:
    """Автор проекта: логин svn, иначе имя пользователя Windows."""
    if login and login.strip():
        return login.strip()
    try:
        return getpass.getuser()
    except Exception as exc:  # нет переменных окружения/профиля: автор не обязателен
        log.warning("Не удалось определить имя пользователя: %s", exc)
        return ""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def new_meta(name: str, folder: str, *, login: str = "", template: Optional[dict[str, Any]] = None,
             fields: Optional[dict[str, str]] = None) -> ProjectMeta:
    """Метаданные нового проекта: дата — сейчас, автор — по `default_author`."""
    return ProjectMeta(name=name, folder=folder, created=now_iso(), author=default_author(login),
                       template=dict(template or {}), fields=dict(fields or {}))


def meta_path(folder: str | os.PathLike) -> Path:
    return Path(folder) / FILE_NAME


def _to_dict(meta: ProjectMeta) -> dict[str, Any]:
    data: dict[str, Any] = {
        "format": meta.format,
        "name": meta.name,
        "folder": meta.folder,
        "created": meta.created,
        "author": meta.author,
        "template": meta.template,
        "fields": meta.fields,
        "svn": meta.svn,
        "links": meta.links,
    }
    for key, value in meta.extra.items():
        data.setdefault(key, value)
    return data


def _load_raw(path: Path) -> Optional[dict[str, Any]]:
    """Содержимое файла как словарь; нет файла или он битый — None (битый пишется в журнал)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        log.warning("Файл %s повреждён (не JSON): %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("Файл %s повреждён: ожидался объект JSON", path)
        return None
    return data


def _dict_of(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return dict(value) if isinstance(value, dict) else {}


def read(folder: str | os.PathLike) -> Optional[ProjectMeta]:
    """Читает project.json из папки проекта. Нет файла или он битый — None."""
    path = meta_path(folder)
    data = _load_raw(path)
    if data is None:
        return None
    fmt = data.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        log.warning("Файл %s: нет числового поля format", path)
        return None
    if fmt > FORMAT:
        log.warning("Файл %s записан более новой версией формата (%s > %s): читаю известные поля",
                    path, fmt, FORMAT)
    name, folder_name = data.get("name"), data.get("folder")
    if not isinstance(name, str) or not isinstance(folder_name, str):
        log.warning("Файл %s: нет полей name/folder", path)
        return None
    svn = data.get("svn")
    return ProjectMeta(
        name=name,
        folder=folder_name,
        created=str(data.get("created") or ""),
        author=str(data.get("author") or ""),
        template=_dict_of(data, "template"),
        fields={str(k): str(v) for k, v in _dict_of(data, "fields").items()},
        svn=dict(svn) if isinstance(svn, dict) else None,
        links=_dict_of(data, "links"),
        format=fmt,
        extra={k: v for k, v in data.items() if k not in _KNOWN},
    )


def write(folder: str | os.PathLike, meta: ProjectMeta) -> Path:
    """Записывает project.json (UTF-8). Неизвестные поля из `meta.extra` и из уже лежащего файла
    сохраняются. Запись через временный файл: обрыв не оставляет половину файла."""
    path = meta_path(folder)
    data = _to_dict(meta)
    existing = _load_raw(path)
    if existing:
        for key, value in existing.items():
            if key not in _KNOWN:
                data.setdefault(key, value)
    data["format"] = max(meta.format, FORMAT)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.exception("Не удалось записать %s", path)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path
