"""Конфиг администратора (svn_helper.json), пользовательские настройки и журнал."""
from __future__ import annotations

import base64
import contextlib
import copy
import ctypes
import dataclasses
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

CONFIG_NAME = "svn_helper.json"

# Потолок прав ИИ-помощника, который задаёт администратор (agent_max_mode): от самого строгого к самому свободному
# write_confirm - запись только с подтверждением: сотрудник не может включить режим без токена (write_full)
AGENT_MODES = ("off", "read_only", "write_confirm", "write")
DEFAULT_AGENT_MAX_MODE = "read_only"


def clean_agent_mode(value: object) -> Optional[str]:
    """Режим помощника из настроек (off, read_only, write_confirm, write; регистр и пробелы не важны) или None, если значение неверное."""
    mode = value.strip().lower() if isinstance(value, str) else ""
    return mode if mode in AGENT_MODES else None


def stricter_mode(first: str, second: str) -> str:
    """Меньший из двух режимов (off меньше read_only, он меньше write_confirm, он меньше write). Неизвестный режим считается самым строгим."""
    def rank(mode: str) -> int:
        return AGENT_MODES.index(mode) if mode in AGENT_MODES else 0
    return first if rank(first) <= rank(second) else second
APP_DIR_NAME = "SvnHelper"

DEFAULT_SERVER = "svn://192.168.11.11"
DEFAULT_REPOS = ["konstruktor", "automation", "common", "test"]
DEFAULT_WORK_DIR = "C:\\Проекты_svn"
# Общий svn_helper.json на сервере: путь к файлу (\\\\сервер\\папка\\svn_helper.json) или ссылка
# svn://…/svn_helper.json. Подставляется в настройки по умолчанию; пустая строка = не использовать.
DEFAULT_FOLDER_FORMAT = "{{date}}_{{name}}"   # имя главной папки нового проекта; у шаблона может быть своё
DEFAULT_COMMIT_TEMPLATES = ["Правка чертежа", "Новая редакция КП", "Исправил по замечаниям"]
DEFAULT_SHARED_CONFIG = "\\\\192.168.11.11\\innotech\\Automation\\common\\SVN-Lite\\svn_helper.json"
SHARED_URL_RE = re.compile(r"^(svn|svn\+ssh|http|https|file)://", re.I)


def app_dir() -> Path:
    """Папка, где лежит программа (и svn_helper.json)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """%APPDATA%\\SvnHelper: настройки пользователя и журнал.

    Переменная SVNLITE_DATA_DIR переключает всё это в другую папку (демо-режим: личные настройки не затрагиваются).
    """
    custom = os.environ.get("SVNLITE_DATA_DIR", "").strip()
    if custom:
        return Path(custom)
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home()
    return base / APP_DIR_NAME


# --------------------------------------------------------------------------
# Конфиг администратора
# --------------------------------------------------------------------------

@dataclass
class FilterRules:
    """Секция "filter" в svn_helper.json: что считать мусором и о чём предупреждать."""
    enabled: bool = True
    skip_junk: bool = True       # не оставлять мусор в рабочей папке
    show_report: bool = True     # показывать отчёт перед скачиванием, если есть что показать
    junk_extensions: list[str] = field(default_factory=lambda: [".bak", ".tmp", ".temp", ".old", ".swp"])
    junk_names: list[str] = field(default_factory=lambda: ["thumbs.db", "desktop.ini", ".ds_store"])
    junk_prefixes: list[str] = field(default_factory=lambda: ["~$", ".~lock"])
    junk_dirs: list[str] = field(default_factory=list)
    junk_globs: list[str] = field(default_factory=list)  # по относительному пути, без учёта регистра
    big_file_mb: int = 50
    path_warn_length: int = 200


def _parse_filter(raw: object, warnings: list[str], base: Optional[FilterRules] = None) -> FilterRules:
    rules = dataclasses.replace(base) if base else FilterRules()
    if raw is None:
        return rules
    if not isinstance(raw, dict):
        warnings.append('Секция "filter" в файле настроек должна быть объектом { ... }. Использованы стандартные правила.')
        return rules

    def flag(key: str) -> None:
        if key in raw:
            if isinstance(raw[key], bool):
                setattr(rules, key, raw[key])
            else:
                warnings.append(f'filter.{key}: нужно true или false. Использовано стандартное значение.')

    def names(key: str, fix) -> None:
        if key in raw:
            value = raw[key]
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                setattr(rules, key, [fix(v) for v in value if v.strip()])
            else:
                warnings.append(f'filter.{key}: нужен список строк. Использовано стандартное значение.')

    def number(key: str) -> None:
        if key in raw:
            if isinstance(raw[key], int) and not isinstance(raw[key], bool) and raw[key] > 0:
                setattr(rules, key, raw[key])
            else:
                warnings.append(f'filter.{key}: нужно целое число больше нуля. Использовано стандартное значение.')

    for key in ("enabled", "skip_junk", "show_report"):
        flag(key)
    names("junk_extensions", lambda v: "." + v.strip().casefold().lstrip("."))
    names("junk_names", lambda v: v.strip().casefold())
    names("junk_prefixes", lambda v: v.strip().casefold())
    names("junk_dirs", lambda v: v.strip().strip("/\\").casefold())
    names("junk_globs", lambda v: v.strip().replace("\\", "/").casefold())
    number("big_file_mb")
    number("path_warn_length")
    return rules


@dataclass
class Config:
    server: str = DEFAULT_SERVER
    repos: list[str] = field(default_factory=lambda: list(DEFAULT_REPOS))
    work_dir: str = DEFAULT_WORK_DIR
    svn_path: str = ""
    username: str = ""
    password: str = ""   # только читается из файла; программа сама пароль никуда не записывает
    support: str = ""         # контакт администратора: «Иванов И., доб. 123, ivanov@…»
    latest_version: str = ""  # последняя версия программы, например "1.1.0"
    update_path: str = ""     # где взять новую версию: папка или файл на сервере
    help_path: str = ""       # путь/ссылка на шпаргалку ("" = ШПАРГАЛКА.* из info_path, затем рядом с программой)
    info_path: str = ""       # папка с документацией на сервере: её файлы показываются в меню «Справка → Документы»
    tortoise_installer_path: str = ""  # msi TortoiseSVN на сервере: программа предложит поставить его, если нет svn.exe
    templates_root: str = ""   # ссылка на папку шаблонов проектов на сервере ("" = шаблоны не настроены)
    project_folder_format: str = DEFAULT_FOLDER_FORMAT   # формат имени главной папки: {{date}}_{{name}}
    agent_max_mode: str = DEFAULT_AGENT_MAX_MODE   # потолок прав помощника: off | read_only | write_confirm | write; сильнее выбора сотрудника
    agent_max_mode_set: bool = False   # потолок уже задан одним из файлов (из двух файлов берётся меньший)
    filter: FilterRules = field(default_factory=FilterRules)
    # готовые комментарии для кнопок в панели «Изменения»
    commit_templates: list[str] = field(default_factory=lambda: list(DEFAULT_COMMIT_TEMPLATES))
    # папки на диске, которые соответствуют папкам хранилища: [{"path": "Z:\\Проекты_SVN", "repo": "Проекты_SVN"}]
    mirrors: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_config(path: Optional[Path] = None, warn_missing: bool = True) -> Config:
    """Читает конфиг в utf-8-sig (Блокнот добавляет BOM). При проблемах берёт значения по умолчанию."""
    path = path or app_dir() / CONFIG_NAME
    cfg = Config()
    try:
        data = _read_json(path.read_bytes())
    except FileNotFoundError:
        if warn_missing:
            cfg.warnings.append(
                f"Файл настроек {path} не найден. Используются значения по умолчанию.")
        return cfg
    except (OSError, ValueError) as exc:  # JSONDecodeError и UnicodeDecodeError — тоже ValueError
        cfg.warnings.append(
            f"Файл настроек {path} повреждён ({exc}). Используются значения по умолчанию. "
            "Сообщите администратору.")
        return cfg
    apply_config_data(cfg, data)
    return cfg


def _read_json(raw: bytes) -> dict:
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("ожидался объект JSON { ... }")
    return data


def apply_config_data(cfg: Config, data: dict, shared: bool = False) -> None:
    """Накладывает поля из data на cfg. Отсутствующие поля не трогает, неверные заменяет предупреждением.

    shared=True — данные из общего конфига на сервере (его читают все): пароль из него не берём.
    """
    if "server" in data:
        server = data["server"]
        if isinstance(server, str) and server.strip():
            cfg.server = server.strip().rstrip("/")
        else:
            cfg.warnings.append("В файле настроек неверно указан сервер (server). Использован стандартный.")

    if "repos" in data:
        repos = data["repos"]
        if isinstance(repos, list):
            cleaned = [r.strip().strip("/") for r in repos if isinstance(r, str) and r.strip().strip("/")]
            if cleaned:
                cfg.repos = cleaned
            else:
                cfg.warnings.append("В файле настроек пустой список проектов (repos). Использован стандартный.")
        else:
            cfg.warnings.append("В файле настроек неверно задан список (repos). Использован стандартный.")

    if "work_dir" in data:
        work_dir = data["work_dir"]
        if isinstance(work_dir, str) and work_dir.strip():
            cfg.work_dir = os.path.expandvars(work_dir.strip())
        else:
            cfg.warnings.append("В файле настроек неверно указана папка (work_dir). Использована стандартная.")

    if isinstance(data.get("svn_path"), str):
        cfg.svn_path = os.path.expandvars(data["svn_path"].strip())
    for key in ("username", "password"):
        if key == "password" and shared:
            if key in data:  # пользователю это не нужно, поэтому только в журнал
                logging.getLogger("svnhelper").warning(
                    "В общем файле настроек задан пароль (password): он проигнорирован. "
                    "Уберите его из общего файла.")
            continue
        if key in data:
            value = data[key]
            if isinstance(value, str):
                setattr(cfg, key, value if key == "password" else value.strip())
            else:
                cfg.warnings.append(f'В файле настроек "{key}" должен быть строкой. Поле пропущено.')
    for key in ("support", "latest_version", "update_path", "help_path", "info_path", "tortoise_installer_path"):
        if key in data:
            value = data[key]
            if isinstance(value, str):
                value = value.strip()
                setattr(cfg, key, os.path.expandvars(value) if key.endswith("_path") else value)
            else:
                cfg.warnings.append(f'В файле настроек "{key}" должен быть строкой. Поле пропущено.')
    for key in ("templates_root", "project_folder_format"):
        if key in data:
            value = data[key]
            if isinstance(value, str):
                value = value.strip()
                if key == "templates_root":
                    cfg.templates_root = value.rstrip("/")
                elif value:
                    cfg.project_folder_format = value
            else:
                cfg.warnings.append(f'В файле настроек "{key}" должен быть строкой. Поле пропущено.')
    if "agent_max_mode" in data:
        mode = clean_agent_mode(data["agent_max_mode"])
        if mode is None:
            cfg.warnings.append(f'В файле настроек "agent_max_mode" должно быть off, read_only, write_confirm или write. '
                                f"Использовано {DEFAULT_AGENT_MAX_MODE}.")
            mode = DEFAULT_AGENT_MAX_MODE
        # локальный и общий файлы: действует меньший, чтобы один из них не мог поднять потолок другого
        cfg.agent_max_mode = stricter_mode(cfg.agent_max_mode, mode) if cfg.agent_max_mode_set else mode
        cfg.agent_max_mode_set = True
    cfg.filter = _parse_filter(data.get("filter"), cfg.warnings, cfg.filter)
    if "commit_templates" in data:
        templates = data["commit_templates"]
        if isinstance(templates, list) and all(isinstance(t, str) for t in templates):
            cfg.commit_templates = [t.strip() for t in templates if t.strip()]
        else:
            cfg.warnings.append('В файле настроек "commit_templates" должен быть списком строк. Поле пропущено.')
    if "mirrors" in data:
        mirrors = clean_mirrors(data["mirrors"])
        if mirrors is None:
            cfg.warnings.append('В файле настроек "mirrors" должен быть списком вида '
                                '[{"path": "Z:\\\\Проекты_SVN", "repo": "Проекты_SVN"}]. Поле пропущено.')
        else:
            cfg.mirrors = mirrors


def clean_mirrors(raw: object) -> Optional[list[dict]]:
    """Список пар {"path", "repo"} из настроек. None, если это вообще не список; негодные записи пропускаются."""
    if not isinstance(raw, list):
        return None
    result: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path, repo = item.get("path"), item.get("repo")
        if isinstance(path, str) and isinstance(repo, str) and path.strip() and repo.strip().strip("/"):
            result.append({"path": os.path.expandvars(path.strip()), "repo": repo.strip().strip("/")})
    return result


def parse_mirrors_text(text: str) -> list[dict]:
    """Строка из окна «Параметры»: «Z:\\Проекты_SVN = Проекты_SVN; D:\\Витрина = test/projects»."""
    result = []
    for part in text.split(";"):
        path, sep, repo = part.partition("=")
        if sep and path.strip() and repo.strip().strip("/"):
            result.append({"path": path.strip(), "repo": repo.strip().strip("/")})
    return result


def mirrors_text(mirrors: list[dict]) -> str:
    """Обратное к parse_mirrors_text."""
    return "; ".join(f"{m['path']} = {m['repo']}" for m in mirrors)


# --------------------------------------------------------------------------
# Общий svn_helper.json на сервере
# --------------------------------------------------------------------------

class SharedConfigError(Exception):
    pass


def fetch_shared(source: str, svn_cat: Callable[[str], bytes]) -> dict:
    """Читает общий конфиг: из файла (в том числе \\\\сервер\\папка) или по ссылке svn://… через `svn cat`."""
    source = source.strip().strip('"')
    try:
        if SHARED_URL_RE.match(source):
            raw = svn_cat(source)
        else:
            raw = Path(source).read_bytes()
        return _read_json(raw)
    except ValueError as exc:
        raise SharedConfigError(f"файл настроек повреждён ({exc})") from exc
    except OSError as exc:
        raise SharedConfigError(f"не удалось прочитать файл ({exc.strerror or exc})") from exc


def shared_cache_path() -> Path:
    return data_dir() / "shared_config.json"


def load_shared_cache() -> Optional[dict]:
    """Последняя удачно прочитанная копия общего конфига: нужна, когда сервер недоступен."""
    try:
        return _read_json(shared_cache_path().read_bytes())
    except (OSError, ValueError):
        return None


def save_shared_cache(data: dict) -> None:
    data = {k: v for k, v in data.items() if k != "password"}  # пароль на диск открытым текстом не пишем
    try:
        path = shared_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logging.getLogger("svnhelper").exception("Не удалось сохранить копию общих настроек")


def build_config(settings: "Settings", shared: Optional[dict] = None,
                 local_path: Optional[Path] = None) -> Config:
    """Итоговая конфигурация. Порядок (позже важнее): значения по умолчанию →
    svn_helper.json рядом с программой → общий конфиг на сервере → личные настройки."""
    cfg = load_config(local_path, warn_missing=False)
    if shared:
        apply_config_data(cfg, shared, shared=True)
    apply_overrides(cfg, settings)
    return cfg


def effective_credentials(settings: "Settings", cfg: Config) -> Optional[tuple[str, str]]:
    """Логин и пароль для входа: личные (пароль расшифровывается DPAPI) важнее конфига."""
    username = settings.username or cfg.username
    password = unprotect_text(settings.password_enc) if settings.password_enc else ""
    password = password or cfg.password
    return (username, password) if username and password else None


# --------------------------------------------------------------------------
# Шифрование пароля средствами Windows (DPAPI): расшифровать может только тот же пользователь
# --------------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi():
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(_Blob)]
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(_Blob)]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return crypt32, kernel32


def _run_dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = _dpapi()
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "SvnHelper", None, None, None, 0,
                                      ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                        ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI: операция не удалась")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect_text(text: str) -> str:
    """Шифрует строку для текущего пользователя Windows. Бросает OSError, если DPAPI недоступен."""
    try:
        return base64.b64encode(_run_dpapi(text.encode("utf-8"), True)).decode("ascii")
    except AttributeError as exc:  # не Windows
        raise OSError("шифрование доступно только в Windows") from exc


def unprotect_text(token: str) -> str:
    """Обратная операция; при любой ошибке (другой пользователь/ПК) возвращает пустую строку."""
    try:
        return _run_dpapi(base64.b64decode(token), False).decode("utf-8")
    except (OSError, ValueError, AttributeError):
        return ""


def find_svn(config_svn_path: str = "") -> Optional[str]:
    """svn_path из конфига → PATH → Program Files\\TortoiseSVN\\bin\\svn.exe."""
    if config_svn_path and os.path.isfile(config_svn_path):
        return config_svn_path
    found = shutil.which("svn")
    if found:
        return found
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        root = os.environ.get(var)
        if root:
            candidate = os.path.join(root, "TortoiseSVN", "bin", "svn.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def find_svnadmin(svn_exe: str = "") -> Optional[str]:
    """svnadmin.exe рядом с найденным svn.exe (в TortoiseSVN они лежат в одной папке bin). Нет - None.

    svn_exe: путь к svn.exe, который уже используется программой; пусто - искать его, как find_svn.
    Без svnadmin.exe локальный проект создаётся без истории и без ошибки.
    """
    svn_exe = svn_exe or find_svn() or ""
    svn_exe = shutil.which(svn_exe) or svn_exe      # "svn" без пути - найти по PATH
    if not svn_exe:
        return None
    candidate = os.path.join(os.path.dirname(os.path.abspath(svn_exe)), "svnadmin.exe")
    return candidate if os.path.isfile(candidate) else None


# --------------------------------------------------------------------------
# Настройки пользователя (без паролей!)
# --------------------------------------------------------------------------

HINT_MODES = ("detailed", "short", "progress")     # режимы панели подсказок; первый - по умолчанию (gui/hints.py)


BUTTON_TABS = ("changes", "locks", "lines", "download")    # вкладки с колонкой кнопок справа


@dataclass
class Settings:
    last_dir: str = ""
    username: str = ""
    password_enc: str = ""   # пароль, зашифрованный DPAPI (только если пользователь отметил «Запомнить»)
    config_path: str = ""    # путь или ссылка на общий svn_helper.json ("" = DEFAULT_SHARED_CONFIG)
    # личные переопределения общего конфига ("" / [] / None = брать из общего)
    server: str = ""
    repos: list[str] = field(default_factory=list)
    svn_path: str = ""
    skip_junk: Optional[bool] = None
    show_report: Optional[bool] = None
    recent: list[dict] = field(default_factory=list)       # [{"url", "name", "dest"}], свежие первыми
    favorites: list[str] = field(default_factory=list)     # ссылки на избранные проекты
    remember_password: Optional[bool] = None               # None = не спрашивали (по умолчанию True)
    update_attempt: str = ""                               # версия, до которой уже запускали обновление (защита от зацикливания)
    theme: str = "system"                                   # оформление: system | light | dark
    mirrors: list[dict] = field(default_factory=list)      # свои папки на диске ↔ хранилище (важнее общих)
    opened: dict = field(default_factory=dict)             # путь папки проекта → когда его последний раз открывали (time.time())
    mine_sort: str = "name"                                # сортировка «Мои проекты»: name | opened | created | changed
    recent_comments: list[str] = field(default_factory=list)   # последние комментарии к сохранению, свежие первыми
    # папки для проектов (выпадающий список над «Мои проекты»); текущая - last_dir
    project_dirs: list[str] = field(default_factory=list)
    mine_col2_collapsed: bool = False                      # «Мои проекты»: свёрнута колонка «Проекты»
    mine_col3_collapsed: bool = False                      # «Мои проекты»: свёрнута колонка «Состояние проекта»
    mine_col2_width: int = 0                               # ширина колонки «Проекты» (px до масштаба экрана); 0 - по умолчанию
    mine_col2_download_width: int = 0                      # она же на подвкладке «Скачать проект» (там дерево сервера)
    # панель подсказок внизу окна (меню «Вид → Подсказки внизу»): detailed - 2 строки, short - 1 строка,
    # progress - только ход работы (полоса появляется на время операции); допустимые значения - HINT_MODES
    hint_mode: str = "detailed"
    hint_height: int = 0       # высота панели, которую человек задал мышью (px при масштабе 100%); 0 - по режиму
    # колонки кнопок (меню «Вид → Кнопки», стрелка вверху колонки): True - только значки, False - значки с подписями.
    # left - колонка слева (проект целиком), right - справа («Состояние проекта» и форма «Скачивание»)
    buttons_compact_left: bool = False
    buttons_compact_right: bool = False                    # справа по умолчанию (меню «Вид → Кнопки» ставит всем вкладкам)
    # справа у каждой вкладки свой вид, его переключает «Скрыть» в её колонке: ключ вкладки (changes, locks, lines,
    # download) -> True (только значки) | False; вкладки без записи - по buttons_compact_right
    buttons_compact_tabs: dict = field(default_factory=dict)
    tray_on_close: bool = True                             # крестик окна сворачивает программу в трей, а не закрывает
    tray_hint_shown: bool = False                          # подсказка «программа продолжает работать у часов» уже показана
    # уведомления и фоновая проверка (Task E.2); допустимые значения и проверка - clean_notify
    notify_enabled: bool = True
    notify_incoming: bool = True                           # сообщать о новом на сервере
    notify_check_min: int = 15                             # как часто проверять, минут (0 - только вручную)
    notify_app_update: bool = True                         # сообщать о новой версии программы
    notify_app_update_days: int = 1                        # напоминать о ней не чаще раза в N дней
    notify_unsaved_hours: int = 4                          # правки не сохранены дольше N часов (0 - не напоминать)
    notify_repeat_hours: int = 4                           # повторять напоминание не чаще раза в N часов
    notify_quiet: bool = False                             # «Не беспокоить» по часам
    notify_quiet_from: str = "18:00"
    notify_quiet_to: str = "09:00"
    notify_quiet_weekends: bool = False                    # и в субботу с воскресеньем целиком
    notify_popup_sec: int = 15                             # сколько секунд показывать окошко (0 - пока не закроют)
    notify_important_only: bool = False                    # только конфликты и несохранённые правки
    notify_muted: list[str] = field(default_factory=list)  # папки проектов, о которых не уведомлять
    # локальная история (Task T.6): папка проекта (normcase) -> папка с историей на этом компьютере.
    # Запись остаётся и после отправки проекта на сервер: история лежит там как архив
    local_histories: dict = field(default_factory=dict)

    @property
    def shared_source(self) -> str:
        return self.config_path.strip() or DEFAULT_SHARED_CONFIG


def apply_overrides(cfg: Config, settings: Settings) -> None:
    """Личные настройки пользователя важнее общего конфига."""
    if settings.server.strip():
        cfg.server = settings.server.strip().rstrip("/")
    if settings.repos:
        cfg.repos = list(settings.repos)
    if settings.svn_path.strip():
        cfg.svn_path = settings.svn_path.strip()
    if settings.skip_junk is not None or settings.show_report is not None:
        cfg.filter = dataclasses.replace(
            cfg.filter,
            skip_junk=cfg.filter.skip_junk if settings.skip_junk is None else settings.skip_junk,
            show_report=cfg.filter.show_report if settings.show_report is None else settings.show_report)
    if settings.mirrors:   # свои соответствия проверяются первыми, общие остаются
        cfg.mirrors = list(settings.mirrors) + [m for m in cfg.mirrors if m not in settings.mirrors]


# --------------------------------------------------------------------------
# Уведомления: значения по умолчанию, варианты для окна «Параметры», проверка
# --------------------------------------------------------------------------

NOTIFY_DEFAULTS: dict = {
    "notify_enabled": True, "notify_incoming": True, "notify_check_min": 15, "notify_app_update": True,
    "notify_app_update_days": 1, "notify_unsaved_hours": 4, "notify_repeat_hours": 4, "notify_quiet": False,
    "notify_quiet_from": "18:00", "notify_quiet_to": "09:00", "notify_quiet_weekends": False,
    "notify_popup_sec": 15, "notify_important_only": False, "notify_muted": [],
}

# (значение, подпись) для выпадающих списков окна «Параметры»
CHECK_MIN_CHOICES = ((5, "5 минут"), (15, "15 минут"), (30, "30 минут"), (60, "60 минут"), (0, "только вручную"))
APP_UPDATE_DAYS_CHOICES = ((1, "раз в сутки"), (7, "раз в неделю"))
UNSAVED_HOURS_CHOICES = ((1, "1 час"), (2, "2 часа"), (4, "4 часа"), (8, "8 часов"), (0, "не напоминать"))
REPEAT_HOURS_CHOICES = ((1, "1 час"), (2, "2 часа"), (4, "4 часа"))
POPUP_SEC_CHOICES = ((5, "5 секунд"), (15, "15 секунд"), (30, "30 секунд"), (0, "пока не закрою"))

# имя в сообщении, наименьшее и наибольшее значение (ноль допустим только там, где он что-то значит)
_NOTIFY_INTS = {
    "notify_check_min": ("«Проверять новое на сервере раз в»", 0, 1440),
    "notify_app_update_days": ("«Проверять обновления программы»", 1, 30),
    "notify_unsaved_hours": ("«Напоминать сохранить правки»", 0, 168),
    "notify_repeat_hours": ("«Напоминать повторно не чаще, чем раз в»", 1, 168),
    "notify_popup_sec": ("«Сколько секунд показывать окошко»", 0, 600),
}
_NOTIFY_TIMES = {"notify_quiet_from": "«Не беспокоить с»", "notify_quiet_to": "«Не беспокоить до»"}
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


def parse_clock(text: object) -> Optional[tuple[int, int]]:
    """«9:00» / «18.30» -> (час, минуты); None, если это не время суток."""
    match = _TIME_RE.match(text) if isinstance(text, str) else None
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return int(match.group(1)), int(match.group(2))


def clean_notify(values: dict) -> tuple[dict, list[str]]:
    """Проверяет значения уведомлений (только те ключи, что есть в values).

    Возвращает (чистые значения, сообщения по-русски о том, что заменено). Некорректное (ноль там, где он
    не имеет смысла, текст вместо числа, время не по форме) заменяется значением по умолчанию.
    """
    clean: dict = {}
    problems: list[str] = []
    for key, value in values.items():
        default = NOTIFY_DEFAULTS.get(key)
        if key in _NOTIFY_INTS:
            label, low, high = _NOTIFY_INTS[key]
            number = value
            if isinstance(value, str) and value.strip().isdigit():
                number = int(value.strip())
            if isinstance(number, int) and not isinstance(number, bool) and low <= number <= high:
                clean[key] = number
            else:
                clean[key] = default
                bounds = f"от {low} до {high}" if low else f"целое число до {high}"
                problems.append(f"{label}: нужно число ({bounds}). Возвращено значение по умолчанию: {default}.")
        elif key in _NOTIFY_TIMES:
            clock = parse_clock(value)
            if clock:
                clean[key] = f"{clock[0]:02d}:{clock[1]:02d}"
            else:
                clean[key] = default
                problems.append(f"{_NOTIFY_TIMES[key]}: нужно время вида 18:00. "
                                f"Возвращено значение по умолчанию: {default}.")
        elif key == "notify_muted":
            clean[key] = list(dict.fromkeys(v for v in value if isinstance(v, str) and v.strip())) \
                if isinstance(value, list) else []
        elif key in NOTIFY_DEFAULTS:
            clean[key] = value if isinstance(value, bool) else default
            if not isinstance(value, bool):
                problems.append(f"Параметр уведомлений «{key}»: нужно «да» или «нет». Возвращено значение по умолчанию.")
    return clean, problems


def apply_notify(settings: Settings, values: dict) -> list[str]:
    """Кладёт значения уведомлений из окна «Параметры» в settings (после проверки). Возвращает сообщения о заменах."""
    clean, problems = clean_notify({k: v for k, v in values.items() if k in NOTIFY_DEFAULTS})
    for key, value in clean.items():
        setattr(settings, key, value)
    return problems


RECENT_LIMIT = 10


def add_recent(settings: Settings, url: str, name: str, dest: str) -> None:
    """Добавляет проект в начало списка недавних: без дублей по ссылке, не больше RECENT_LIMIT."""
    settings.recent = [r for r in settings.recent if r.get("url") != url]
    settings.recent.insert(0, {"url": url, "name": name, "dest": dest})
    del settings.recent[RECENT_LIMIT:]


def _clean_recent(raw: object) -> list[dict]:
    """Терпит мусор в settings.json: оставляет только записи с непустой ссылкой."""
    result: list[dict] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"]:
            continue
        if any(r["url"] == item["url"] for r in result):
            continue
        result.append({"url": item["url"],
                       "name": item["name"] if isinstance(item.get("name"), str) else "",
                       "dest": item["dest"] if isinstance(item.get("dest"), str) else ""})
    return result[:RECENT_LIMIT]


def same_dir(a: str, b: str) -> bool:
    """Один и тот же путь папки (без учёта регистра и косых черт в Windows)."""
    return bool(a.strip()) and bool(b.strip()) and \
        os.path.normcase(os.path.normpath(a.strip())) == os.path.normcase(os.path.normpath(b.strip()))


def add_project_dir(dirs: list[str], path: str) -> list[str]:
    """Список папок с path (в конце, если её ещё нет). Пустой path список не меняет."""
    path = os.path.normpath(path.strip()) if path.strip() else ""
    if not path or any(same_dir(d, path) for d in dirs):
        return list(dirs)
    return list(dirs) + [path]


def clean_project_dirs(raw: object, last_dir: str = "") -> list[str]:
    """Терпит мусор в settings.json: только непустые строки без повторов; last_dir добавляется, если его нет."""
    dirs: list[str] = []
    for value in raw if isinstance(raw, list) else []:
        if isinstance(value, str):
            dirs = add_project_dir(dirs, value)
    return add_project_dir(dirs, last_dir)


def tab_compact(settings: "Settings", key: str) -> bool:
    """Вид кнопок справа у вкладки key: только значки (True) или с подписями."""
    return settings.buttons_compact_tabs.get(key, settings.buttons_compact_right)


def _parse_settings(data: dict) -> Settings:
    """Settings из содержимого settings.json; мусорные значения заменяются стандартными.
    Если data не словарь, бросает AttributeError."""

    def text(key: str) -> str:
        return data[key] if isinstance(data.get(key), str) else ""

    def flag(key: str) -> Optional[bool]:
        return data[key] if isinstance(data.get(key), bool) else None

    repos = data.get("repos")
    return Settings(
        last_dir=text("last_dir"), username=text("username"), password_enc=text("password_enc"),
        config_path=text("config_path"), server=text("server"), svn_path=text("svn_path"),
        repos=[r for r in repos if isinstance(r, str) and r] if isinstance(repos, list) else [],
        skip_junk=flag("skip_junk"), show_report=flag("show_report"),
        recent=_clean_recent(data.get("recent")),
        favorites=list(dict.fromkeys(f for f in data["favorites"] if isinstance(f, str) and f))
        if isinstance(data.get("favorites"), list) else [],
        remember_password=flag("remember_password"),
        update_attempt=text("update_attempt"),
        theme=text("theme") if text("theme") in ("system", "light", "dark") else "system",
        mirrors=clean_mirrors(data.get("mirrors")) or [],
        opened={k: float(v) for k, v in data["opened"].items()
                if isinstance(k, str) and isinstance(v, (int, float))}
        if isinstance(data.get("opened"), dict) else {},
        mine_sort=text("mine_sort") if text("mine_sort") in ("name", "opened", "created", "changed")
        else "name",
        recent_comments=[c for c in data["recent_comments"] if isinstance(c, str) and c.strip()][:5]
        if isinstance(data.get("recent_comments"), list) else [],
        # старые настройки знали одну папку (last_dir): она становится первой в списке
        project_dirs=clean_project_dirs(data.get("project_dirs"), text("last_dir")),
        mine_col2_collapsed=flag("mine_col2_collapsed") or False,
        mine_col3_collapsed=flag("mine_col3_collapsed") or False,
        mine_col2_width=data["mine_col2_width"] if type(data.get("mine_col2_width")) is int
        and 100 <= data["mine_col2_width"] <= 2000 else 0,
        mine_col2_download_width=data["mine_col2_download_width"]
        if type(data.get("mine_col2_download_width")) is int and 100 <= data["mine_col2_download_width"] <= 2000
        else 0,
        hint_mode=text("hint_mode") if text("hint_mode") in HINT_MODES else HINT_MODES[0],
        hint_height=data["hint_height"] if type(data.get("hint_height")) is int and 20 <= data["hint_height"] <= 600
        else 0,
        buttons_compact_left=flag("buttons_compact_left") is True,
        buttons_compact_right=flag("buttons_compact_right") is True,
        buttons_compact_tabs={k: v for k, v in data["buttons_compact_tabs"].items()
                              if k in BUTTON_TABS and isinstance(v, bool)}
        if isinstance(data.get("buttons_compact_tabs"), dict) else {},
        tray_on_close=flag("tray_on_close") is not False,
        tray_hint_shown=flag("tray_hint_shown") is True,
        local_histories={k: v for k, v in data["local_histories"].items()
                         if isinstance(k, str) and k and isinstance(v, str) and v}
        if isinstance(data.get("local_histories"), dict) else {},
        **clean_notify({k: data[k] for k in NOTIFY_DEFAULTS if k in data})[0],
    )


# Замок настроек: окно, консольная программа и сервер для ИИ-агента правят один settings.json.
# Внутри процесса потоки ждут друг друга обычным замком (замки файла двух потоков одного процесса конфликтуют),
# между процессами - замок байта в файле рядом с настройками.
_SETTINGS_THREAD_LOCK = threading.Lock()
SETTINGS_LOCK_TIMEOUT = 10.0     # секунд ждать, пока другая программа закончит запись настроек
_WRITE_RETRIES = 3               # попыток заменить файл настроек (антивирус или читатель держит его мгновение)
READ_WAIT_CLI = 0.5              # секунд повторять чтение настроек у консольной программы и сервера помощника
READ_WAIT_WINDOW = 4.0           # то же при запуске окна: оно ждёт дольше, ведь иначе не откроется совсем


def _retry_delays(wait: float) -> list:
    """Паузы между повторами чтения: 0.05, 0.1, 0.15 ... не больше 0.25 с, всего `wait` секунд."""
    delays: list = []
    total = 0.0
    while total < wait - 1e-9:
        delay = min(0.05 * (len(delays) + 1), 0.25, wait - total)
        delays.append(delay)
        total += delay
    return delays


class SettingsUnreadable(OSError):
    """Файл настроек есть, но прочитать его не удалось. Пустые настройки вместо него подставлять нельзя:
    проекты «пропали» бы, а окно потом записало бы пустые настройки поверх настоящих."""


@contextlib.contextmanager
def _settings_file_lock(settings_path: Path, timeout: float = SETTINGS_LOCK_TIMEOUT):
    """Замок файла настроек на msvcrt.locking.

    Намеренно не folderlock.named_lock: тот общий на всю папку данных, а замок настроек привязан к самому файлу.
    Он лежит рядом с ним, поэтому разные файлы настроек (например, в тестах) не мешают друг другу и не трогают
    настоящую папку данных. Не вызывать повторно из того же потока: замок не рекурсивный.
    """
    import msvcrt
    lock_path = settings_path.with_name(settings_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with _SETTINGS_THREAD_LOCK:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Настройки сейчас сохраняет другая программа. Повторите позже.") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


class SettingsStore:
    """settings.json: личные настройки. Пароль хранится только зашифрованным (DPAPI) и только по желанию.

    Файл могут править несколько программ сразу, поэтому запись идёт под замком: `update` перечитывает файл,
    применяет правку и пишет результат, `save` не стирает чужие записи о локальной истории.
    """

    def __init__(self, path: Optional[Path] = None, load_wait: float = READ_WAIT_CLI):
        """load_wait: сколько секунд при создании повторять чтение занятого файла (у окна больше, чем у CLI/MCP)."""
        self.path = Path(path) if path else data_dir() / "settings.json"
        self._broken_seen: Optional[bytes] = None      # содержимое, о повреждении которого уже сказано в журнале
        self.settings = self._load(load_wait)
        self._synced = copy.deepcopy(self.settings)    # как настройки выглядели при последней сверке с файлом

    def _read_bytes(self, wait: float = READ_WAIT_CLI) -> Optional[bytes]:
        """Содержимое файла настроек. None: файла нет (это нормально). Если файл занят, чтение повторяется
        с короткими паузами до `wait` секунд; не вышло - OSError."""
        for delay in [*_retry_delays(wait), None]:
            try:
                return self.path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError:
                if delay is None:
                    raise
                time.sleep(delay)
        return None                                                # недостижимо: последняя попытка бросает

    def _keep_broken_copy(self, raw: bytes) -> None:
        """Повреждённый файл настроек перед перезаписью сохраняется рядом как settings.json.bad-<дата-время>.
        Одно и то же содержимое копируется один раз (его могли сохранить раньше, в том числе другая программа).
        Существующая копия не затирается: при совпадении имени к нему добавляется числовой суффикс."""
        if not raw.strip():
            return                                                 # пустой файл терять нечего
        try:
            for old in self.path.parent.glob(self.path.name + ".bad-*"):
                if old.read_bytes() == raw:
                    return
            base = f"{self.path.name}.bad-{datetime.now():%Y%m%d-%H%M%S}"
            for number in range(1000):
                copy_path = self.path.with_name(base if number == 0 else f"{base}-{number}")
                try:
                    with open(copy_path, "xb") as f:               # "x": занятое имя не перезаписывается
                        f.write(raw)
                except FileExistsError:
                    continue
                logging.getLogger("svnhelper").warning("Копия повреждённого файла настроек: %s", copy_path)
                return
            logging.getLogger("svnhelper").error("Не удалось подобрать имя для копии повреждённого файла настроек")
        except OSError:
            logging.getLogger("svnhelper").exception("Не удалось сохранить копию повреждённого файла настроек")

    def _parse(self, raw: bytes) -> Optional[Settings]:
        """Настройки из содержимого файла; None: файл повреждён (копия сохранена, в журнал записано предупреждение)."""
        try:
            return _parse_settings(json.loads(raw.decode("utf-8-sig")))
        except (ValueError, AttributeError):
            if raw != self._broken_seen:               # сервер помощника перечитывает файл на каждый вызов: не спамить
                self._broken_seen = raw
                logging.getLogger("svnhelper").warning("Файл настроек %s повреждён: он будет записан заново", self.path)
                self._keep_broken_copy(raw)
            return None

    def _load(self, wait: float) -> Settings:
        """Настройки при создании. Нет файла или он повреждён: стандартные настройки (повреждённый файл сохраняется
        копией). Файл есть, но не прочитан: SettingsUnreadable, запись в журнале."""
        try:
            raw = self._read_bytes(wait)
        except OSError as exc:
            logging.getLogger("svnhelper").exception("Не удалось прочитать настройки %s", self.path)
            raise SettingsUnreadable(
                "Не удалось прочитать файл настроек: его, возможно, занимает другая программа. "
                "Повторите через несколько секунд.") from exc
        if raw is None:
            return Settings()
        return self._parse(raw) or Settings()

    def _read_file(self) -> Optional[Settings]:
        """Настройки из файла. None: файла нет или он повреждён (тогда за основу берётся то, что в памяти).
        Другая ошибка чтения (файл занят даже после повторов) пробрасывается: иначе чужие настройки были бы затёрты."""
        raw = self._read_bytes()
        return None if raw is None else self._parse(raw)

    def _write(self, settings: Settings) -> None:
        """Атомарная запись (временный файл и замена), до 3 попыток. Вызывать под замком."""
        payload = json.dumps(dataclasses.asdict(settings), ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        for attempt in range(_WRITE_RETRIES):
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self.path)
                return
            except OSError:
                if attempt == _WRITE_RETRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def update(self, fn: Callable[[Settings], None]) -> None:
        """Под замком перечитывает файл, применяет fn к свежим настройкам и записывает их.

        Правки других программ в файле сохраняются. Объект self.settings (на него ссылается окно) обновляется
        на месте: берутся поля, которые изменила fn, и поля без несохранённых правок окна в памяти.
        Ошибка записи пишется в журнал и пробрасывается.
        """
        try:
            with _settings_file_lock(self.path):
                fresh = self._read_file()
                if fresh is None:
                    fresh = copy.deepcopy(self.settings)
                before = copy.deepcopy(fresh)
                fn(fresh)
                if fresh != before or not self.path.exists():
                    self._write(fresh)
                self._adopt(before, fresh)
        except OSError:
            logging.getLogger("svnhelper").exception("Не удалось сохранить настройки")
            raise

    def _adopt(self, before: Settings, after: Settings) -> None:
        """Переносит в self.settings результат update, не затирая несохранённые правки окна в других полях."""
        for item in dataclasses.fields(Settings):
            name = item.name
            changed_by_fn = getattr(before, name) != getattr(after, name)
            unsaved_edit = getattr(self.settings, name) != getattr(self._synced, name)
            if changed_by_fn or not unsaved_edit:
                setattr(self.settings, name, copy.deepcopy(getattr(after, name)))
                setattr(self._synced, name, copy.deepcopy(getattr(after, name)))

    def save(self) -> None:
        """Записывает настройки из памяти. Записи о локальной истории, которые добавили другие программы,
        остаются (удалять их можно только через update)."""
        try:
            with _settings_file_lock(self.path):
                payload = copy.deepcopy(self.settings)
                on_disk = self._read_file()
                if on_disk is not None:
                    payload.local_histories = {**on_disk.local_histories, **self.settings.local_histories}
                self._write(payload)
                self._synced = copy.deepcopy(self.settings)
        except OSError:
            logging.getLogger("svnhelper").exception("Не удалось сохранить настройки")

    def refresh(self, keys: Sequence[str] = ("local_histories",)) -> None:
        """Подтягивает в память указанные поля из файла (правки других программ).
        Ошибка чтения пишется в журнал и пробрасывается."""
        try:
            with _settings_file_lock(self.path):
                fresh = self._read_file()
        except OSError:
            logging.getLogger("svnhelper").exception("Не удалось перечитать настройки")
            raise
        if fresh is None:
            return
        for key in keys:
            setattr(self.settings, key, copy.deepcopy(getattr(fresh, key)))
            setattr(self._synced, key, copy.deepcopy(getattr(fresh, key)))


# --------------------------------------------------------------------------
# Кэш дерева сервера (чтобы открывать папки мгновенно и искать проекты по названию)
# --------------------------------------------------------------------------

def tree_cache_path() -> Path:
    return data_dir() / "tree_cache.json"


def load_tree_cache() -> dict:
    """{"server": str, "folders": {"repo/a/b": {"dirs": [[name, is_project], ...], "ts": float}}}.

    Любая ошибка чтения (нет файла, мусор) → пустой dict.
    """
    try:
        data = _read_json(tree_cache_path().read_bytes())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data.get("folders"), dict) else {}


def save_tree_cache(data: dict) -> None:
    try:
        path = tree_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logging.getLogger("svnhelper").exception("Не удалось сохранить кэш дерева сервера")


# --------------------------------------------------------------------------
# Версия программы
# --------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)\s*$", re.I)


def is_newer_version(latest: str, current: str) -> bool:
    """'1.2.10' новее '1.2.9'. Пустое значение или мусор → False."""
    parsed = []
    for text in (latest, current):
        match = _VERSION_RE.match(text) if isinstance(text, str) else None
        if not match:
            return False
        parsed.append([int(n) for n in match.group(1).split(".")])
    size = max(len(parsed[0]), len(parsed[1]))
    pad = lambda v: v + [0] * (size - len(v))  # noqa: E731  «1.1» == «1.1.0»
    return pad(parsed[0]) > pad(parsed[1])


# --------------------------------------------------------------------------
# Журнал
# --------------------------------------------------------------------------

def setup_logging() -> Path:
    """Журнал %APPDATA%\\SvnHelper\\svn_helper.log. Возвращает фактический путь."""
    path = data_dir() / "svn_helper.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        path = Path(tempfile.gettempdir()) / "svn_helper.log"
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("svnhelper")
    root.setLevel(logging.INFO)
    root.handlers = [handler]
    return path
