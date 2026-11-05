"""Операция doctor: всё ли готово к работе помощника. Ничего не пишет; связь проверяется одним листингом."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .. import __version__, agentlog, selfupdate, server
from ..config import CONFIG_NAME, app_dir, data_dir, shared_cache_path
from ..svn_core import SvnError
from . import texts, texts_write
from .context import OpsContext, is_url
from .errors import from_exception, no_svn
from .registry import operation
from .result import ErrorInfo, Result, clean_text


def _program_data() -> dict:
    """Версия программы. У собранного svnlite.exe ещё версия окна (его запись при запуске) и совпадение версий:
    при запуске из исходников окно и помощник это один пакет, сравнивать нечего."""
    program: dict = {"version": __version__}
    if selfupdate.is_supported():
        window = selfupdate.window_version()
        program.update(cli_version=__version__, window_version=window,
                       versions_match=not window or window == __version__,
                       update_pending=selfupdate.pending_cli_update())
    return program


def _svn_data(ctx: OpsContext) -> dict:
    if ctx.client is None:
        return {"found": False, "version": "", "path": ""}
    major, minor = ctx.client.version()
    return {"found": True, "version": f"{major}.{minor}", "path": ctx.client.svn_path}


def _config_data(ctx: OpsContext) -> dict:
    cache = shared_cache_path()
    cached = cache.is_file()
    stamp = datetime.fromtimestamp(cache.stat().st_mtime).isoformat(timespec="seconds") if cached else ""
    return {"next_to_program": (app_dir() / CONFIG_NAME).is_file(), "shared_cache": cached,
            "shared_cache_date": stamp, "warnings": [clean_text(w, 300) for w in ctx.config.warnings[:10]]}


def _check_connection(ctx: OpsContext, wanted: bool) -> tuple:
    """(данные о связи, ошибка или None). Один листинг первой корневой папки за connect_timeout секунд."""
    if not wanted:
        return {"state": "skipped"}, None
    if ctx.client is None:
        return {"state": "no_svn"}, None
    if not is_url(ctx.config.server) or not ctx.config.repos:
        return {"state": "not_configured"}, None
    url = server.build_url(ctx.config.server, [ctx.config.repos[0]])
    try:
        ctx.client.list_entries(url + "@", timeout=ctx.connect_timeout)
    except SvnError as exc:
        error = from_exception(exc)
        return {"state": error.kind}, error
    return {"state": "ok"}, None


@operation("doctor", needs_svn=False)
def doctor(ctx: OpsContext, check_connection: bool = True) -> Result:
    settings = ctx.settings
    connection, conn_error = _check_connection(ctx, check_connection)
    dirs = ctx.project_dirs
    data = {
        "program": _program_data(),
        "svn": _svn_data(ctx),
        "config": _config_data(ctx),
        "server": {"configured": is_url(ctx.config.server) and bool(ctx.config.repos), "roots": len(ctx.config.repos)},
        "login": {"username": clean_text(settings.username or ctx.config.username, 100)},
        "project_dirs": [clean_text(d, 260) for d in dirs],
        "connection": connection,
        "templates": {"configured": bool(ctx.config.templates_root.strip())},
        "policy": ctx.policy.describe(),
        "logs": {"agent_log": str(data_dir() / "logs" / agentlog.agent_log_name(datetime.now())),
                 "window_log": str(data_dir() / "svn_helper.log"), "data_dir": str(data_dir())},
    }
    problems: list = []
    error: Optional[ErrorInfo] = None
    if ctx.client is None:
        error = from_exception(no_svn())
        problems.append(error.message)
    if not dirs:
        problems.append(texts.DOCTOR_NO_PROJECT_DIRS)
    if conn_error is not None:
        problems.append(conn_error.message)
        error = error or conn_error
    elif connection["state"] == "not_configured":
        problems.append(texts.ERRORS["not_configured"][0])
    elif connection["state"] == "skipped":
        problems.append(texts.DOCTOR_NOT_CHECKED)
    data["ready"] = not problems and error is None
    text = texts.doctor_text(problems, data["ready"])
    warnings = []
    note = texts.POLICY_SIGNATURE_NOTES.get(data["policy"].get("signature", ""))
    if note:                                               # подпись политики не принята: запись выключена, причина своими словами
        warnings.append(note)
        text = f"{text} {note}"
    if not data["program"].get("versions_match", True):
        template = texts.VERSIONS_DIFFER_PENDING if data["program"]["update_pending"] else texts.VERSIONS_DIFFER
        differ = template.format(window=data["program"]["window_version"], cli=data["program"]["cli_version"])
        warnings.append(differ)
        text = f"{text} {differ}"
    text = f"{text} {texts_write.mode_text(data['policy'].get('file_mode', ''), data['policy'].get('mode', ''))}".rstrip()
    svn = {"server": ctx.config.server, "roots": list(ctx.config.repos), "templates_root": ctx.config.templates_root}
    if error is not None:
        return Result("doctor", False, data, text, warnings, [], error, svn)
    return Result("doctor", data=data, text=text, warnings=warnings, svn=svn)
