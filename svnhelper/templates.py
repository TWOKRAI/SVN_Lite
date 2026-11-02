"""Шаблоны проектов: поиск на сервере, манифест template.json, локальный кэш, имена и копирование. Без tkinter.

Формат и правила - в docs/PLAN_templates.md (разделы «Форматы» и Task T.1). Содержимое файлов шаблона не меняется:
подставляются только имена папок и файлов.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from . import config, fsutil
from .audit import WINDOWS_PATH_LIMIT as MAX_PATH     # путь такой длины и длиннее Windows открывает не везде
from .server import _BAD_NAME_CHARS as _BAD_CHARS, build_url, normalize_url, validate_line_name
from .svn_core import CancelToken, CONNECT_TIMEOUT, ErrorKind, SvnClient, SvnError

log = logging.getLogger("svnhelper.templates")

MANIFEST_NAME = "template.json"
META_NAME = "meta.json"
FILES_DIR = "files"
FORMAT = 1
MAX_DIR_PATH = 248          # для папок Windows оставляет место под 8.3-имя: путь папки от этой длины - отказ
MAX_NAME = 255
STALE_SEC = 24 * 3600       # временные папки кэша и старые версии шаблонов старше суток можно убирать
LIST_THREADS = 6
BUILTIN_IDS = ("folder", "author", "today")   # нельзя использовать как id поля
DATE_DEFAULT = "{{today:yyMMdd}}"
_NOT_FOUND_CODES = {"E170000", "W170000", "E160013", "W160013"}   # такого пути на сервере нет
_NO_LINK_KINDS = (ErrorKind.NO_CONNECTION, ErrorKind.TIMEOUT)
_TOO_LONG_CODES = {"E720206"}                                     # ERROR_FILENAME_EXCED_RANGE (WinError 206)
_SAMPLE_DATE = date(2026, 1, 2)

NO_CACHE_MESSAGE = ("Нет связи с сервером, а этот шаблон на компьютер ещё не скачивался. "
                    "Подключитесь к сети или VPN и откройте окно снова.")


class TemplateError(Exception):
    """Ошибка шаблона с готовым русским текстом для человека."""


TOO_LONG_MESSAGE = ("Путь к файлам шаблона получился слишком длинным для Windows. "
                    "Сообщите администратору: нужно сократить имена папок и файлов в шаблоне.")


class ManifestError(TemplateError):
    """Ошибка в template.json: текст адресован администратору."""


# --------------------------------------------------------------------------
# Проверка имён (не очистка: ничего не заменяется молча)
# --------------------------------------------------------------------------

_RESERVED_WINDOWS = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def validate_file_name(name: str) -> str:
    """Проверяет имя файла или папки для Windows. Возвращает его без изменений, иначе ValueError по-русски."""
    if not name or not name.strip():
        raise ValueError("имя получилось пустым")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("в имени есть управляющие символы (например, табуляция)")
    bad = _BAD_CHARS.search(name)
    if bad:
        raise ValueError(f"в имени нельзя использовать символ «{bad.group()}» (запрещены / \\ : * ? \" < > |)")
    if name != name.lstrip():
        raise ValueError("имя не должно начинаться с пробела")
    if name.endswith(" ") or name.endswith("."):
        raise ValueError("имя не должно заканчиваться пробелом или точкой")
    if name.split(".")[0].rstrip().upper() in _RESERVED_WINDOWS:
        raise ValueError(f"имя «{name}» зарезервировано в Windows")
    if len(name) > MAX_NAME:
        raise ValueError(f"имя длиннее {MAX_NAME} символов")
    return name


# --------------------------------------------------------------------------
# Плейсхолдеры
# --------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_DATE_TOKEN = re.compile(r"yyyy|yy|MM|dd")


def _check_mask(mask: str) -> None:
    """Допустимы только yyyy, yy, MM, dd и знаки между ними. Бросает ValueError."""
    if not mask:
        raise ValueError("не указана маска даты")
    if "mm" in mask:
        raise ValueError("«mm» запрещено (путают с минутами): месяц пишется «MM»")
    rest = _DATE_TOKEN.sub("", mask)
    if re.search(r"[A-Za-z]", rest):
        raise ValueError("в маске даты допустимы только yyyy, yy, MM, dd")
    if "/" in mask or "\\" in mask:
        raise ValueError("в маске даты не должно быть / и \\")


def format_date(mask: str, value: date) -> str:
    """Дата по маске yyyy, yy, MM, dd (не strftime)."""
    _check_mask(mask)
    parts = {"yyyy": f"{value.year:04d}", "yy": f"{value.year % 100:02d}",
             "MM": f"{value.month:02d}", "dd": f"{value.day:02d}"}
    return _DATE_TOKEN.sub(lambda m: parts[m.group()], mask)


def _check_placeholders(text: str, allowed: Iterable[str], where: str) -> None:
    """Все {{...}} в text известны: id из allowed или today:маска. Иначе ManifestError."""
    allowed = set(allowed)
    for match in _PLACEHOLDER.finditer(text):
        token = match.group(1).strip()
        if token == "today":
            raise ManifestError(f"{where}: для {{{{today}}}} нужна маска даты, например {{{{today:yyMMdd}}}}.")
        if token.startswith("today:"):
            try:
                _check_mask(token[len("today:"):])
            except ValueError as exc:
                raise ManifestError(f"{where}: {{{{{token}}}}}: {exc}.") from exc
        elif token not in allowed:
            raise ManifestError(f"{where}: неизвестная подстановка {{{{{token}}}}}.")
    rest = _PLACEHOLDER.sub("", text)
    if "{{" in rest or "}}" in rest:
        raise ManifestError(f"{where}: незакрытая скобка в «{text}».")


def _expand(text: str, context: dict[str, str], today: date) -> str:
    def replace(match: re.Match) -> str:
        token = match.group(1).strip()
        if token.startswith("today:"):
            return format_date(token[len("today:"):], today)
        return context[token]
    return _PLACEHOLDER.sub(replace, text)


# --------------------------------------------------------------------------
# Манифест
# --------------------------------------------------------------------------

@dataclass
class FieldDef:
    id: str
    label: str = ""
    default: str = ""
    required: bool = False
    hint: str = ""


@dataclass
class Manifest:
    title: str
    description: str = ""
    folder_name: str = config.DEFAULT_FOLDER_FORMAT
    parent: str = ""                       # место на сервере без адреса: konstruktor/projects
    fields: list[FieldDef] = field(default_factory=list)   # с date и name; порядок как в диалоге
    renames: dict[str, str] = field(default_factory=dict)  # путь в шаблоне (регистр как в шаблоне) -> новое имя
    format: int = FORMAT


def _builtin_fields() -> list[FieldDef]:
    return [FieldDef("date", "Дата", DATE_DEFAULT), FieldDef("name", "Название", "", required=True)]


def _text(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ManifestError(f"В template.json поле «{key}» должно быть строкой.")
    return value


def _parse_fields(raw: Any) -> list[FieldDef]:
    fields = _builtin_fields()
    if raw is None:
        return fields
    if not isinstance(raw, list):
        raise ManifestError("В template.json поле «fields» должно быть списком.")
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ManifestError("В template.json у каждого поля из «fields» должен быть «id» (строка).")
        field_id = item["id"].strip()
        if field_id in BUILTIN_IDS:
            raise ManifestError(f"В template.json id поля «{field_id}» занят встроенной подстановкой "
                                f"({', '.join(BUILTIN_IDS)}). Выберите другой id.")
        if not re.fullmatch(r"\w+", field_id):
            raise ManifestError(f"В template.json id поля «{field_id}» может состоять только из букв, цифр и «_».")
        if field_id in seen:
            raise ManifestError(f"В template.json id поля «{field_id}» повторяется.")
        seen.add(field_id)
        for key in ("label", "default", "hint"):
            if key in item and not isinstance(item[key], str):
                raise ManifestError(f"В template.json у поля «{field_id}» «{key}» должно быть строкой.")
        if "required" in item and not isinstance(item["required"], bool):
            raise ManifestError(f"В template.json у поля «{field_id}» «required» должно быть true или false.")
        parsed = FieldDef(field_id, item.get("label") or field_id, item.get("default", ""),
                          item.get("required", False), item.get("hint", ""))
        for index, existing in enumerate(fields):
            if existing.id == field_id:      # шаблон может переопределить date и name
                fields[index] = parsed
                break
        else:
            fields.append(parsed)
    return fields


def _check_sample(template: str, field_ids: Iterable[str], where: str, as_folder: bool = False) -> None:
    """Подставляет «x» вместо полей и проверяет постоянную часть: имя должно быть допустимым для Windows."""
    context = {fid: "x" for fid in field_ids}
    context.update(folder="x", author="x")
    sample = _expand(template, context, _SAMPLE_DATE)
    try:
        validate_file_name(sample)
        if as_folder:
            validate_line_name(sample)
    except ValueError as exc:
        raise ManifestError(f"{where}: при подстановке получится недопустимое имя «{sample}»: "
                            f"{str(exc).rstrip('.')}.") from exc


def _norm_rel(path: str) -> str:
    return "/".join(part for part in path.replace("\\", "/").split("/") if part)


def parse_manifest(data: bytes | str, template_paths: Optional[Sequence[str]] = None,
                   repos: Optional[Sequence[str]] = None, default_folder_format: str = config.DEFAULT_FOLDER_FORMAT,
                   fallback_title: str = "") -> Manifest:
    """Разбор и строгая проверка template.json. Ошибки (ManifestError) написаны для администратора.

    template_paths - пути внутри шаблона (list_template_paths): по ним проверяются ключи renames.
    repos - корни хранилищ из svn_helper.json: parent должен начинаться с одного из них.
    """
    try:
        raw = json.loads(data.decode("utf-8-sig") if isinstance(data, bytes) else data)
    except ValueError as exc:
        raise ManifestError(f"Файл template.json повреждён ({exc}). Исправьте его и сохраните в кодировке UTF-8.") from exc
    if not isinstance(raw, dict):
        raise ManifestError("Файл template.json должен содержать объект { ... }.")
    version = raw.get("format")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ManifestError("В template.json нужно поле «format»: 1.")
    if version > FORMAT:
        raise ManifestError(f"Шаблон сделан для более новой версии программы (format {version}). "
                            "Обновите программу.")
    if version != FORMAT:
        raise ManifestError(f"В template.json неподдерживаемое значение format: {version}.")

    fields = _parse_fields(raw.get("fields"))
    field_ids = {f.id for f in fields}
    for f in fields:
        _check_placeholders(f.default, ("author",), f"Поле «{f.id}», значение по умолчанию")

    from_template = "folder_name" in raw
    folder_name = _text(raw, "folder_name") if from_template else default_folder_format
    where = "folder_name" if from_template else "Формат имени папки (project_folder_format в svn_helper.json)"
    if not folder_name.strip():
        raise ManifestError(f"{where}: пусто.")
    if re.search(r"\{\{\s*folder\s*\}\}", folder_name):
        raise ManifestError(f"{where}: {{{{folder}}}} нельзя использовать внутри имени самой папки.")
    _check_placeholders(folder_name, field_ids | {"author"}, where)
    _check_sample(folder_name, field_ids, where, as_folder=True)

    parent = _norm_rel(_text(raw, "parent"))
    if parent and any(part in (".", "..") for part in parent.split("/")):
        raise ManifestError(f"В template.json parent «{parent}»: «.» и «..» в пути не допускаются.")
    if parent and repos:
        roots = [r.strip("/") for r in repos]
        if not any(parent == r or parent.startswith(r + "/") for r in roots):
            raise ManifestError(f"В template.json parent «{parent}» должен начинаться с одного из корней "
                                f"сервера: {', '.join(roots)}.")
    elif parent:
        log.warning("parent «%s» не проверен: список корней сервера (repos) пуст", parent)

    renames = _parse_renames(raw.get("renames"), field_ids | {"folder", "author"}, template_paths)
    return Manifest(title=_text(raw, "title").strip() or fallback_title, description=_text(raw, "description").strip(),
                    folder_name=folder_name, parent=parent, fields=fields, renames=renames, format=version)


def _parse_renames(raw: Any, allowed_ids: set[str], template_paths: Optional[Sequence[str]]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError("В template.json поле «renames» должно быть объектом { \"путь\": \"новое имя\" }.")
    canonical = {p.casefold(): p for p in template_paths} if template_paths is not None else None
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(f"В template.json renames «{key}»: новое имя должно быть непустой строкой.")
        path = _norm_rel(key)
        if canonical is not None:
            if path.casefold() not in canonical:
                raise ManifestError(f"В template.json renames: пути «{key}» нет в шаблоне.")
            path = canonical[path.casefold()]
        if not path or ".." in path.split("/"):
            raise ManifestError(f"В template.json renames: неверный путь «{key}».")
        if "/" in value or "\\" in value:
            raise ManifestError(f"В template.json renames «{key}»: новое имя «{value}» должно быть именем, "
                                "без / и \\.")
        _check_placeholders(value, allowed_ids, f"renames «{key}»")
        _check_sample(value, allowed_ids, f"renames «{key}»")
        if any(p.casefold() == path.casefold() for p in result):
            raise ManifestError(f"В template.json renames: путь «{path}» указан дважды.")
        result[path] = value
    seen: dict[tuple[str, str], str] = {}
    for path, value in result.items():
        slot = (path.rpartition("/")[0].casefold(), value.casefold())
        if slot in seen:
            raise ManifestError(f"В template.json renames: «{seen[slot]}» и «{path}» получают одно и то же имя "
                                f"«{value}» (регистр не различается).")
        seen[slot] = path
    if template_paths is not None:     # постоянное имя не должно совпасть с соседом, который не переименовывается
        renamed = {p.casefold() for p in result}
        for path, value in result.items():
            if "{{" in value:
                continue
            parent = path.rpartition("/")[0]
            for other in template_paths:
                if (other.rpartition("/")[0] == parent and other.casefold() not in renamed
                        and other.rpartition("/")[2].casefold() == value.casefold() and other != path):
                    raise ManifestError(f"В template.json renames: «{path}» получает имя «{value}», "
                                        "но такое уже есть в той же папке.")
    return result


class TemplatePaths(list):
    """Список путей шаблона; dirs - какие из них папки (для разных пределов длины пути)."""

    def __init__(self, paths: Iterable[str] = (), dirs: Iterable[str] = ()):
        super().__init__(paths)
        self.dirs = set(dirs)


def list_template_paths(template_dir: Path) -> TemplatePaths:
    """Все файлы и папки шаблона относительно корня, через «/». Сам template.json в корне не входит."""
    paths: list[str] = []
    folders: list[str] = []
    root = str(template_dir)
    for base, dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root).replace(os.sep, "/")
        prefix = "" if rel_base == "." else rel_base + "/"
        for name in dirs + files:
            if not prefix and name.casefold() == MANIFEST_NAME:
                continue
            paths.append(prefix + name)
        folders.extend(prefix + name for name in dirs)
    return TemplatePaths(sorted(paths), folders)


def load_manifest(template_dir: Path, repos: Optional[Sequence[str]] = None,
                  default_folder_format: str = config.DEFAULT_FOLDER_FORMAT, fallback_title: str = "") -> Manifest:
    """template.json из папки шаблона, с проверкой renames по её содержимому."""
    manifest_path = Path(template_dir) / MANIFEST_NAME
    try:
        data = manifest_path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"В шаблоне нет файла template.json ({exc.strerror or exc}).") from exc
    return parse_manifest(data, list_template_paths(Path(template_dir)), repos, default_folder_format, fallback_title)


# --------------------------------------------------------------------------
# План имён
# --------------------------------------------------------------------------

@dataclass
class Plan:
    folder: str = ""                                           # итоговое имя главной папки
    renames: list[tuple[str, str]] = field(default_factory=list)   # (путь в шаблоне, новое имя), самые глубокие первыми
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    longest_path: int = 0                                      # длина самого длинного итогового пути, 0 - не считалась

    @property
    def ok(self) -> bool:
        return not self.errors


def default_values(manifest: Manifest, author: str = "", today: Optional[date] = None) -> dict[str, str]:
    """Значения полей по умолчанию (для диалога): подстановки в default уже раскрыты."""
    today = today or date.today()
    context = {"author": author}
    return {f.id: _expand(f.default, context, today).strip() for f in manifest.fields}


def _final_path(path: str, rename_map: dict[str, str]) -> str:
    """Путь шаблона после всех переименований (в том числе папок выше по пути)."""
    original = path.split("/")
    parts = list(original)
    for index in range(len(original)):
        new = rename_map.get("/".join(original[:index + 1]).casefold())
        if new is not None:
            parts[index] = new
    return "/".join(parts)


def build_plan(manifest: Manifest, values: dict[str, str], author: str = "", today: Optional[date] = None,
               template_paths: Sequence[str] = (), base_dir: str = "", warn_length: int = 200,
               staging_extra: int = 0) -> Plan:
    """Подставляет значения и проверяет итоговые имена. Ошибки не бросаются, а собираются в plan.errors.

    template_paths - содержимое шаблона (list_template_paths): нужно для поиска совпадений имён и длины пути.
    base_dir - папка, в которой появится проект (для длины пути). warn_length - filter.path_warn_length.
    staging_extra - на сколько символов длиннее имя временной папки сборки (".<имя>.creating": 10): путь должен
    помещаться и во временной папке. Если template_paths пуст, длина путей не проверяется (предупреждение).
    """
    today = today or date.today()
    plan = Plan()
    context: dict[str, str] = {"author": author}
    for f in manifest.fields:
        raw = values.get(f.id)
        if raw is None:
            raw = _expand(f.default, context, today)
        value = raw.strip()
        label = f.label or f.id
        if f.required and not value:
            plan.errors.append(f"Заполните поле «{label}».")
        if "/" in value or "\\" in value:
            plan.errors.append(f"В поле «{label}» нельзя использовать / и \\.")
            value = value.replace("/", "_").replace("\\", "_")
        context[f.id] = value

    plan.folder = _expand(manifest.folder_name, context, today).strip()
    folder_ok = _check_folder(plan)
    context["folder"] = plan.folder

    new_names: dict[str, str] = {}
    for path, template in manifest.renames.items():
        new = _expand(template, context, today)
        if "/" in new or "\\" in new:
            plan.errors.append(f"«{path}»: новое имя «{new}» содержит / или \\.")
            continue
        try:
            validate_file_name(new)
        except ValueError as exc:
            plan.errors.append(f"«{path}»: {exc}.")
            continue
        new_names[path] = new
    _check_collisions(plan, new_names, template_paths)
    plan.renames = sorted(new_names.items(), key=lambda item: -item[0].count("/"))

    if not template_paths:
        plan.warnings.append("Длину путей в проекте проверить не удалось: список файлов шаблона пуст.")
    elif folder_ok:
        _check_length(plan, new_names, template_paths, base_dir, warn_length, staging_extra)
    return plan


def _check_folder(plan: Plan) -> bool:
    try:
        validate_file_name(plan.folder)
        validate_line_name(plan.folder)    # длина имени на сервере и точка в начале
    except ValueError as exc:
        plan.errors.append(f"Название папки проекта «{plan.folder}»: {str(exc).rstrip('.')}.")
        return False
    return True


def _check_collisions(plan: Plan, new_names: dict[str, str], template_paths: Sequence[str]) -> None:
    taken: dict[tuple[str, str], str] = {}
    for path, new in new_names.items():
        slot = (path.rpartition("/")[0].casefold(), new.casefold())
        if slot in taken:
            plan.errors.append(f"«{taken[slot]}» и «{path}» получают одно и то же имя «{new}».")
        taken[slot] = path
    renamed = {p.casefold() for p in new_names}
    for path in template_paths:
        if path.casefold() in renamed:
            continue
        slot = (path.rpartition("/")[0].casefold(), path.rpartition("/")[2].casefold())
        if slot in taken:
            plan.errors.append(f"«{taken[slot]}» получит имя «{path.rpartition('/')[2]}», но такое уже есть "
                               "в той же папке.")


def _check_length(plan: Plan, new_names: dict[str, str], template_paths: Sequence[str], base_dir: str,
                  warn_length: int, staging_extra: int = 0) -> None:
    """Файлы: отказ от 260 символов, папки: от 248. Считается и итоговый путь, и путь во временной папке."""
    rename_map = {p.casefold(): n for p, n in new_names.items()}
    root_len = (len(base_dir.rstrip("\\/")) + 1 if base_dir else 0) + len(plan.folder) + staging_extra
    dirs = getattr(template_paths, "dirs", None)
    if dirs is None:
        dirs = {p.rpartition("/")[0] for p in template_paths if "/" in p}
    longest_file = longest_dir = root_len     # сама главная папка - тоже папка
    longest = root_len
    for path in template_paths:
        length = root_len + 1 + len(_final_path(path, rename_map))
        longest = max(longest, length)
        if path in dirs:
            longest_dir = max(longest_dir, length)
        else:
            longest_file = max(longest_file, length)
    plan.longest_path = longest
    if longest_dir >= MAX_DIR_PATH:
        plan.errors.append(f"Самая длинная папка в проекте получится {longest_dir} символов: Windows создаёт папки "
                           f"с путём не длиннее {MAX_DIR_PATH - 1}. Сократите название или выберите папку ближе к корню диска.")
    if longest_file >= MAX_PATH:
        plan.errors.append(f"Самый длинный путь в проекте получится {longest_file} символов: Windows открывает "
                           f"пути не длиннее {MAX_PATH - 1}. Сократите название или выберите папку ближе к корню диска.")
    if plan.ok and longest > warn_length:
        plan.warnings.append(f"Самый длинный путь в проекте получится {longest} символов "
                             f"(рекомендуется не больше {warn_length}): некоторые программы могут не открыть такие файлы.")


# --------------------------------------------------------------------------
# Создание папки по плану
# --------------------------------------------------------------------------

def _too_long(exc: BaseException) -> bool:
    return getattr(exc, "winerror", None) == 206 or "WinError 206" in str(exc)


def _remove_tree(path: Path, base: Path) -> bool:
    """Удаляет папку через fsutil (файлы «только для чтения» тоже). Неудача идёт в журнал. True: папки больше нет."""
    try:
        fsutil.remove_partial_dir(str(path), str(base))
    except (OSError, ValueError) as exc:
        log.warning("Не удалось удалить %s: %s", path, exc)
    return not os.path.lexists(path)


def apply_plan(plan: Plan, template_dir: Path, target_dir: Path, cancel: Optional[CancelToken] = None) -> None:
    """Копирует шаблон в target_dir (пустые папки сохраняются, template.json не копируется) и переименовывает.

    target_dir должна не существовать или быть пустой. При сбое и при остановке (cancel -> SvnError CANCELLED)
    убирается созданное этой функцией: папка, которой не было, удаляется, пустая прежняя остаётся пустой.
    """
    if plan.errors:
        raise TemplateError("Нельзя создать проект: " + " ".join(plan.errors))
    template_dir, target_dir = Path(template_dir), Path(target_dir)
    if target_dir.exists() and (not target_dir.is_dir() or any(target_dir.iterdir())):
        raise FileExistsError(str(target_dir))
    existed = target_dir.exists()
    root = os.path.normcase(str(template_dir))

    def check_cancel() -> None:
        if cancel is not None and cancel.cancelled:
            raise SvnError(ErrorKind.CANCELLED)

    def skip_manifest(directory: str, names: list[str]) -> list[str]:
        if os.path.normcase(directory) == root:
            return [n for n in names if n.casefold() == MANIFEST_NAME]
        return []

    def copy_file(src: str, dst: str) -> str:
        check_cancel()
        return shutil.copy2(src, dst)

    try:
        shutil.copytree(template_dir, target_dir, ignore=skip_manifest, copy_function=copy_file,
                        dirs_exist_ok=True)
        _rename_all(plan, target_dir, check_cancel)
    except BaseException as exc:
        _rollback(target_dir, existed)
        if _too_long(exc):
            raise TemplateError(TOO_LONG_MESSAGE) from exc
        raise


def _rollback(target_dir: Path, existed: bool) -> None:
    if not existed:
        _remove_tree(target_dir, target_dir.parent)
        return
    for child in target_dir.iterdir():            # папка была пустой и пользователь её выбрал: оставляем её пустой
        if child.is_dir() and not child.is_symlink():
            _remove_tree(child, target_dir)
            continue
        try:
            try:
                child.unlink()
            except PermissionError:
                os.chmod(child, 0o600)
                child.unlink()
        except OSError as exc:
            log.warning("Не удалось удалить %s: %s", child, exc)


def _rename_all(plan: Plan, target_dir: Path, check_cancel: Callable[[], None]) -> None:
    """Глубокие пути первыми. Внутри одного уровня - через временные имена, чтобы a->b и b->c не мешали друг другу."""
    levels: dict[int, list[tuple[str, str]]] = {}
    for path, new in plan.renames:
        levels.setdefault(path.count("/"), []).append((path, new))
    for depth in sorted(levels, reverse=True):
        check_cancel()
        staged = []
        for path, new in levels[depth]:
            source = target_dir.joinpath(*path.split("/"))
            temp = source.with_name(f".r{uuid.uuid4().hex[:6]}")
            os.rename(source, temp)
            staged.append((temp, source.with_name(new)))
        for temp, final in staged:
            os.rename(temp, final)


# --------------------------------------------------------------------------
# Поиск шаблонов на сервере и кэш на компьютере
# --------------------------------------------------------------------------
#
# Кэш: <кэш>/<hash8>/meta.json (указатель на текущую версию) и <кэш>/<hash8>/r<версия>/files (копия шаблона).
# Версия в кэше не меняется после записи: путь FetchedTemplate.path остаётся рабочим, пока им пользуются.
# Новая версия кладётся рядом, указатель переключается одной заменой файла, старые версии убираются позже.

@dataclass
class TemplateInfo:
    id: str            # имя папки шаблона на сервере
    title: str
    description: str
    revision: Optional[int]    # версия шаблона: когда папка менялась последний раз
    url: str


@dataclass
class TemplateList:
    templates: list[TemplateInfo]
    offline: bool = False      # сервер недоступен: список взят из сохранённых на компьютере шаблонов


@dataclass
class FetchedTemplate:
    path: Path                 # папка с копией шаблона
    revision: Optional[int]
    fetched: Optional[datetime]   # когда копия скачана
    offline: bool = False      # обновить не удалось (нет связи): взята сохранённая копия


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(cache_dir: Path, template_id: str) -> threading.Lock:
    """Один замок на шаблон: два окна не скачивают и не заменяют один и тот же шаблон одновременно."""
    key = f"{os.path.normcase(str(cache_dir))}|{template_id}"
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def default_cache_dir() -> Path:
    """Кэш шаблонов. data_dir() в Roaming (профиль ходит по сети) - кэш кладётся в %LOCALAPPDATA%."""
    data = config.data_dir()
    local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if not os.environ.get("SVNLITE_DATA_DIR") and local and roaming:
        if os.path.normcase(str(data)).startswith(os.path.normcase(roaming)):
            return Path(local) / config.APP_DIR_NAME / "templates_cache"
    return data / "templates_cache"


def _entry_dir(cache_dir: Path, template_id: str) -> Path:
    """Короткое имя записи (путь кэша не должен раздувать длину путей); настоящий id лежит в meta.json."""
    return Path(cache_dir) / hashlib.sha1(template_id.encode("utf-8")).hexdigest()[:8]


def _read_meta(entry: Path, template_id: Optional[str] = None) -> Optional[dict]:
    """meta.json записи кэша; None, если записи нет, она неполная или принадлежит другому шаблону."""
    try:
        meta = json.loads((entry / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
        return None
    version = meta.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\w+", version) or not (entry / version / FILES_DIR).is_dir():
        return None
    if template_id is not None and meta["id"] != template_id:
        return None
    return meta


def _parse_time(text: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _info_from_meta(meta: dict) -> TemplateInfo:
    template_id = meta["id"]
    return TemplateInfo(template_id, str(meta.get("title") or template_id), str(meta.get("description") or ""),
                        meta.get("revision") if isinstance(meta.get("revision"), int) else None,
                        str(meta.get("url") or ""))


def cached_templates(cache_dir: Path) -> list[TemplateInfo]:
    """Шаблоны, уже скачанные на компьютер (для работы без связи)."""
    result = []
    cache_dir = Path(cache_dir)
    for entry in sorted(cache_dir.glob("*")) if cache_dir.is_dir() else []:
        if entry.name.startswith("."):          # временные папки скачивания
            continue
        meta = _read_meta(entry)
        if meta:
            result.append(_info_from_meta(meta))
    result.sort(key=lambda t: t.title.casefold())
    return result


def _lenient_titles(data: bytes, template_id: str) -> tuple[str, str]:
    """Название и описание для списка; полная проверка манифеста - при выборе шаблона (load_manifest)."""
    raw = json.loads(data.decode("utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("ожидался объект JSON")
    title, description = raw.get("title"), raw.get("description")
    return (title.strip() if isinstance(title, str) and title.strip() else template_id,
            description.strip() if isinstance(description, str) else "")


def list_templates(client: SvnClient, root_url: str, cache_dir: Optional[Path] = None,
                   cancel: Optional[CancelToken] = None, pool: Optional[Executor] = None) -> list[TemplateInfo]:
    """Шаблоны из папки root_url: каждая подпапка с template.json. Нет связи -> SvnError (NO_CONNECTION/TIMEOUT).

    Версия шаблона берётся из листинга. template.json читается только у изменившихся папок (у остальных
    название и описание берутся из кэша), параллельно. Сбой чтения одного шаблона (например, нет прав)
    уходит в журнал, шаблон в список не попадает, остальные работают.
    Свой pool можно передать, но не тот, в котором выполняется сам вызов (иначе он будет ждать сам себя).
    """
    if not root_url.strip():
        return []
    cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
    root = normalize_url(root_url)
    entries = [e for e in client.list_entries(root, cancel=cancel, timeout=CONNECT_TIMEOUT) if e.kind == "dir"]

    def describe(name: str, revision: Optional[int]) -> Optional[TemplateInfo]:
        url = build_url(root, [name])
        meta = _read_meta(_entry_dir(cache_dir, name), name)
        if meta and revision is not None and meta.get("revision") == revision:
            cached = _info_from_meta(meta)
            return TemplateInfo(name, cached.title, cached.description, revision, url)
        try:
            data = client.cat(build_url(url, [MANIFEST_NAME]), cancel=cancel)
        except SvnError as exc:
            if exc.kind in _NO_LINK_KINDS or exc.kind == ErrorKind.CANCELLED:
                raise
            if not (exc.codes & _NOT_FOUND_CODES or exc.kind == ErrorKind.NOT_FOUND):
                log.warning("Шаблон «%s»: template.json не прочитан (%s), в список не включён", name, exc.kind.value)
            return None            # обычная папка без template.json - не шаблон
        try:
            title, description = _lenient_titles(data, name)
        except ValueError as exc:
            log.warning("Шаблон «%s»: template.json не читается (%s), в список не включён", name, exc)
            return None
        return TemplateInfo(name, title, description, revision, url)

    own_pool = pool is None
    pool = pool or ThreadPoolExecutor(max(1, min(LIST_THREADS, len(entries))))
    try:
        futures = [pool.submit(describe, e.name, e.revision) for e in entries]
        found = [f.result() for f in futures]
    finally:
        if own_pool:
            pool.shutdown(wait=False, cancel_futures=True)
    result = [info for info in found if info]
    result.sort(key=lambda t: t.title.casefold())
    return result


def load_templates(client: SvnClient, root_url: str, cache_dir: Optional[Path] = None,
                   cancel: Optional[CancelToken] = None, pool: Optional[Executor] = None) -> TemplateList:
    """Список для диалога: с сервера, а без связи - из сохранённых на компьютере (offline=True)."""
    cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
    if not root_url.strip():
        return TemplateList([])
    try:
        return TemplateList(list_templates(client, root_url, cache_dir, cancel, pool))
    except SvnError as exc:
        if exc.kind not in _NO_LINK_KINDS:
            raise
        log.warning("Нет связи с сервером шаблонов, берём сохранённые: %s", exc.kind.value)
        return TemplateList(cached_templates(cache_dir), offline=True)


def _fetched(entry: Path, meta: dict, offline: bool) -> FetchedTemplate:
    return FetchedTemplate(entry / meta["version"] / FILES_DIR,
                           meta.get("revision") if isinstance(meta.get("revision"), int) else None,
                           _parse_time(meta.get("fetched")), offline)


def cached_template(cache_dir: Path, template_id: str) -> Optional[FetchedTemplate]:
    """Сохранённая копия шаблона без обращения к серверу (показывается сразу, пока идёт обновление)."""
    entry = _entry_dir(Path(cache_dir), template_id)
    meta = _read_meta(entry, template_id)
    return _fetched(entry, meta, False) if meta else None


def fetch_template(client: SvnClient, info: TemplateInfo, cache_dir: Optional[Path] = None,
                   cancel: Optional[CancelToken] = None) -> FetchedTemplate:
    """Копия шаблона на компьютере. Совпадает версия - берётся кэш без скачивания; иначе экспорт с сервера
    и переключение кэша на новую версию (прежняя копия не трогается). Нет связи: сохранённая копия
    (offline=True) либо TemplateError, если её нет. Вызовы для одного шаблона идут по очереди.
    """
    cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
    with _lock_for(cache_dir, info.id):
        return _fetch_locked(client, info, cache_dir, cancel)


def _fetch_locked(client: SvnClient, info: TemplateInfo, cache_dir: Path,
                  cancel: Optional[CancelToken]) -> FetchedTemplate:
    entry = _entry_dir(cache_dir, info.id)
    meta = _read_meta(entry, info.id)
    if meta and info.revision is not None and meta.get("revision") == info.revision:
        return _fetched(entry, meta, False)

    cache_dir.mkdir(parents=True, exist_ok=True)
    staging = cache_dir / f".n{uuid.uuid4().hex[:6]}"
    try:
        staging.mkdir()
        client.export(info.url, str(staging / FILES_DIR), cancel=cancel, revision=info.revision)
        version = f"r{info.revision}" if info.revision is not None else f"u{uuid.uuid4().hex[:6]}"
        entry.mkdir(exist_ok=True)
        if (entry / version).exists():
            _remove_tree(staging, cache_dir)       # та же версия уже лежит рядом (прошлый сбой): она идентична
        else:
            os.rename(staging, entry / version)
        new_meta = {"id": info.id, "title": info.title, "description": info.description, "url": info.url,
                    "revision": info.revision, "version": version,
                    "fetched": datetime.now().isoformat(timespec="seconds")}
        tmp = entry / ".meta.tmp"
        tmp.write_text(json.dumps(new_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, entry / META_NAME)
    except SvnError as exc:
        _remove_tree(staging, cache_dir)
        if exc.kind in _NO_LINK_KINDS:
            if meta:
                return _fetched(entry, meta, True)
            raise TemplateError(NO_CACHE_MESSAGE) from exc
        if exc.codes & _NOT_FOUND_CODES or exc.kind == ErrorKind.NOT_FOUND:
            raise TemplateError(f"Шаблон «{info.title}» на сервере не найден: возможно, его удалили. "
                                "Обновите список шаблонов.") from exc
        if exc.codes & _TOO_LONG_CODES or _too_long(exc):
            raise TemplateError(TOO_LONG_MESSAGE) from exc
        raise
    except BaseException:
        _remove_tree(staging, cache_dir)
        raise
    _prune_versions(entry, keep=new_meta["version"])
    return _fetched(entry, new_meta, False)


def _prune_versions(entry: Path, keep: str) -> None:
    """Старые версии шаблона убираются лениво: не раньше чем через сутки (ими могли пользоваться)."""
    now = time.time()
    for child in entry.iterdir():
        if child.name == keep or child.name.startswith(".") or not child.is_dir():
            continue
        try:
            if now - child.stat().st_mtime > STALE_SEC:
                _remove_tree(child, entry)
        except OSError as exc:
            log.warning("Не удалось проверить %s: %s", child, exc)


def cleanup_cache(cache_dir: Path) -> None:
    """Убирает остатки: временные папки скачивания и старые версии шаблонов старше суток."""
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return
    now = time.time()
    for child in cache_dir.iterdir():
        try:
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                if now - child.stat().st_mtime > STALE_SEC:
                    _remove_tree(child, cache_dir)
                continue
            meta = _read_meta(child)
            if meta:
                _prune_versions(child, keep=meta["version"])
        except OSError as exc:
            log.warning("Не удалось проверить %s: %s", child, exc)


def warm_cache(client: SvnClient, root_url: str, cache_dir: Optional[Path] = None,
               cancel: Optional[CancelToken] = None) -> int:
    """Прогрев кэша при запуске (если есть связь): скачивает новые и изменившиеся шаблоны. Возвращает их число.

    Сбой одного шаблона в журнал, остальные продолжаются. Нет связи или отмена - тихо 0.
    """
    cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
    cleanup_cache(cache_dir)
    try:
        infos = list_templates(client, root_url, cache_dir, cancel)
    except SvnError as exc:
        log.info("Кэш шаблонов не обновлён: %s", exc.kind.value)
        return 0
    updated = 0
    for info in infos:
        try:
            before = cached_template(cache_dir, info.id)
            result = fetch_template(client, info, cache_dir, cancel)
        except (SvnError, TemplateError, OSError) as exc:
            log.warning("Шаблон «%s» не удалось сохранить на компьютер: %s", info.id, exc)
            if isinstance(exc, SvnError) and exc.kind == ErrorKind.CANCELLED:
                break
            continue
        if not result.offline and (before is None or before.revision != result.revision):
            updated += 1
    return updated
