"""Создание проекта из шаблона и отправка папки на сервер (Task AG.5, разрешения `create_local`, `create_server`, `publish`).

Тонкая обёртка над ядром окна: `newproject.create_local_project`, `newproject.create_server_project`, `publish.publish`.
Журнал задач (`jobs.py`) пишет само ядро, поэтому убитый посреди работы процесс разбирает окно при следующем запуске.
Папку занимает само ядро (`own_lock=True`). Удалять на сервере помощник не умеет.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .. import config as config_module
from .. import newproject, projectmeta, publish, server, templates
from ..svn_core import CheckoutProgress
from . import texts
from .context import OpsContext, deadline, is_url
from .errors import OpsError, invalid_args
from .remote import _server_segments
from .resolve import resolve_project
from .result import Result, clean_name
from .writing import Prepared, WriteSpec, early_policy, mark_comment, register, signature_of

log = logging.getLogger("svnhelper.ops")

SERVER_ONLY_STORAGE = {"local": "create_local", "server": "create_server"}


def _values(manifest: templates.Manifest, fields: Optional[list], author: str) -> dict:
    """Значения полей шаблона: умолчания, поверх них заданное «имя=значение». Неизвестное поле: ошибка."""
    values = templates.default_values(manifest, author)
    known = {f.id for f in manifest.fields}
    for item in fields or []:
        key, sep, value = str(item).partition("=")
        key = key.strip()
        if not sep or key not in known:
            raise invalid_args(f"Поле «{clean_name(key, 60)}» в шаблоне не найдено. Поля: {', '.join(sorted(known))}.")
        values[key] = value.strip()
    return values


def _parent(ctx: OpsContext, manifest: templates.Manifest, place: Optional[str]) -> tuple:
    segments = _server_segments(ctx, place or manifest.parent or "")
    if not segments:
        raise OpsError("place_unknown", 1)
    return segments, server.build_url(ctx.config.server, segments)


def prepare_create(ctx: OpsContext, template: str, fields: Optional[list] = None, storage: str = "local",
                   base_dir: Optional[str] = None, place: Optional[str] = None) -> Prepared:
    storage = (storage or "local").strip().lower()
    if storage not in SERVER_ONLY_STORAGE:
        raise invalid_args("Где создать: «local» (на компьютере) или «server» (сразу на сервере).")
    action = SERVER_ONLY_STORAGE[storage]
    early_policy(ctx, action)
    root = ctx.config.templates_root.strip()
    if not root:
        raise OpsError("not_configured", 1, texts.TEMPLATES_NOT_CONFIGURED)
    if not is_url(root):
        raise OpsError("not_configured", 1, texts.TEMPLATES_ROOT_BAD)
    base = (base_dir or (ctx.project_dirs[0] if ctx.project_dirs else "")).strip().strip('"')
    if not base:
        raise OpsError("not_configured", 1, texts.NO_PROJECT_DIRS)
    problem = newproject.check_base_dir(base)
    if problem:
        raise invalid_args(problem)
    base = os.path.normpath(base)
    ctx.policy.check(action, base, base_dirs=ctx.project_dirs, consume=False)        # папка до скачивания шаблона
    with deadline(ctx) as token:
        listing = templates.load_templates(ctx.client, root, cancel=token)
        info = next((t for t in listing.templates if t.id == template), None)
        if info is None:
            raise OpsError("template_unknown", 1)
        loaded = newproject.load_template(ctx.client, info, None, token, list(ctx.config.repos),
                                          ctx.config.project_folder_format)
        author = projectmeta.default_author(ctx.settings.username or ctx.config.username)
        values = _values(loaded.manifest, fields, author)
        plan = templates.build_plan(loaded.manifest, values, author, template_paths=loaded.paths, base_dir=base,
                                    warn_length=ctx.config.filter.path_warn_length,
                                    staging_extra=len(".") + len(newproject.CREATING_SUFFIX))
        if plan.errors:
            raise OpsError("invalid_args", 2, "Проект по этим данным создать нельзя: " + " ".join(plan.errors),
                           untrusted_fields=[])
        dest = os.path.join(base, plan.folder)
        if os.path.lexists(dest):
            raise OpsError("exists", 1, "Папка с таким именем уже есть на компьютере.", "Измените поля шаблона.")
        parent_url, project_url = "", ""
        where = "на компьютере"
        if storage == "server":
            segments, parent_url = _parent(ctx, loaded.manifest, place)
            project_url = server.build_url(parent_url, [plan.folder])
            if ctx.client.url_exists(project_url):
                raise OpsError("exists", 1, "Проект с таким именем уже есть на сервере.", "Измените поля шаблона.")
            where = f"на сервере ({'/'.join(segments)}) и на компьютере"
    summary = (f"Создать проект «{clean_name(plan.folder)}» из шаблона «{clean_name(loaded.manifest.title or info.title)}» "
               f"{where}: {dest}.")
    detail = {"folder": clean_name(plan.folder), "path": clean_name(dest), "storage": storage,
              "renames": len(plan.renames), "template_revision": loaded.fetched.revision}
    return Prepared(action, base, summary, signature_of(action, dest, info.revision, values, parent_url), detail,
                    warnings=list(plan.warnings), untrusted=["folder", "path"], audit={"files": [(dest, "создан")]},
                    state={"loaded": loaded, "plan": plan, "values": values, "base": base, "dest": dest,
                           "parent_url": parent_url, "storage": storage, "info": info})


def execute_create(ctx: OpsContext, prepared: Prepared, template: str, fields: Optional[list] = None,
                   storage: str = "local", base_dir: Optional[str] = None, place: Optional[str] = None) -> Result:
    state = prepared.state
    loaded, plan, values = state["loaded"], state["plan"], state["values"]
    login = ctx.settings.username or ctx.config.username
    meta = projectmeta.new_meta(values.get("name", ""), plan.folder, login=login,
                                template={"id": state["info"].id, "title": loaded.manifest.title or state["info"].title,
                                          "revision": loaded.fetched.revision}, fields=values)
    client = ctx.client
    svnadmin = config_module.find_svnadmin(getattr(client, "svn_path", ""))
    with deadline(ctx) as token:
        if state["storage"] == "server":
            comment = mark_comment(ctx, f"Новый проект из шаблона «{loaded.manifest.title or state['info'].title}»")
            result = newproject.create_server_project(
                client, state["base"], plan, loaded.fetched.path, meta, state["parent_url"], comment, ctx.config.filter,
                token, CheckoutProgress(), None, None, svnadmin=svnadmin, store=ctx.store)
            data = {"path": clean_name(result.path), "on_server": result.on_server, "history": result.history,
                    "note": clean_name(result.note, 300)}
            text = (f"Проект «{clean_name(plan.folder)}» создан на сервере и скачан на компьютер." if result.on_server
                    else f"Проект «{clean_name(plan.folder)}» создан только на компьютере. {clean_name(result.note, 300)}")
        else:
            result = newproject.create_local_project(client, state["base"], plan, loaded.fetched.path, meta, token, None,
                                                     svnadmin, ctx.config.filter, ctx.store)
            data = {"path": clean_name(result.path), "history": result.history, "note": clean_name(result.note, 300)}
            text = f"Проект «{clean_name(plan.folder)}» создан на компьютере."
    return Result("create_project_apply", data=data, text=text, untrusted_fields=["path", "note"])


def prepare_publish(ctx: OpsContext, folder: str, place: str, message: Optional[str] = None) -> Prepared:
    early_policy(ctx, "publish")
    ref = resolve_project(ctx, folder, strict=True)
    ctx.policy.check("publish", ref.path, base_dirs=ctx.project_dirs, consume=False)
    segments = _server_segments(ctx, place)
    if not segments:
        raise OpsError("place_unknown", 1)
    parent_url = server.build_url(ctx.config.server, segments)
    plan = publish.check_publish(ctx.client, ref.path, parent_url, None, ctx.config.filter)
    final = mark_comment(ctx, (message or "").strip() or "Проект отправлен на сервер")
    summary = (f"Отправить папку «{clean_name(ref.name)}» на сервер в «{'/'.join(segments)}» как проект "
               f"«{clean_name(plan.name)}». Папка на компьютере привяжется к проекту на сервере.")
    return Prepared("publish", ref.path, summary, signature_of("publish", ref.path, plan.project_url, final),
                    {"name": clean_name(plan.name), "place": "/".join(segments)}, untrusted=["name"],
                    audit={"comment": final, "files": [(ref.path, "отправлена")]},
                    state={"ref": ref, "parent_url": parent_url, "message": final, "plan": plan})


def execute_publish(ctx: OpsContext, prepared: Prepared, folder: str, place: str,
                    message: Optional[str] = None) -> Result:
    state = prepared.state
    with deadline(ctx) as token:
        result = publish.publish(ctx.client, state["ref"].path, state["parent_url"], state["message"], None,
                                 ctx.config.filter, CheckoutProgress(), token)
    ctx.audit.update(rev_after=result.revision)
    data = {"number": result.revision, "entries": result.entries, "read_only": len(result.read_only),
            "not_uploaded": len(result.not_uploaded)}
    text = (f"Папка «{clean_name(state['ref'].name)}» отправлена на сервер, сохранение № {result.revision}. "
            + newproject.publish_summary(result))
    return Result("publish_apply", data=data, text=text.strip(), svn={"url": result.project_url})


register(WriteSpec("create_project", prepare_create, execute_create, own_lock=True))
register(WriteSpec("publish", prepare_publish, execute_publish, own_lock=True))
