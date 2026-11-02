"""Операция doctor: всё ли готово к работе помощника. Ничего не пишет; связь проверяется одним листингом."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .. import __version__, agentlog, server
from ..config import CONFIG_NAME, app_dir, data_dir, shared_cache_path
from ..svn_core import SvnError
from . import texts
from .context import OpsContext
from .errors import from_exception, no_svn
from .registry import operation
from .result import ErrorInfo, Result, clean_text


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
    if not ctx.config.server or not ctx.config.repos:
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
        "program": {"version": __version__},
        "svn": _svn_data(ctx),
        "config": _config_data(ctx),
        "server": {"configured": bool(ctx.config.server), "roots": len(ctx.config.repos)},
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
    data["ready"] = not problems and error is None
    text = texts.doctor_text(problems, data["ready"])
    svn = {"server": ctx.config.server, "roots": list(ctx.config.repos), "templates_root": ctx.config.templates_root}
    if error is not None:
        return Result("doctor", False, data, text, [], [], error, svn)
    return Result("doctor", data=data, text=text, svn=svn)
