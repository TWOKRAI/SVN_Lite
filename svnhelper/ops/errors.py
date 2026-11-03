"""Исключения программы -> ErrorInfo (вид ошибки, текст по-русски, код возврата консольной программы).

Тексты для человека берутся из `texts.ERRORS` (слова сотрудника, без названий кнопок окна); технические
подробности (коды E######, ответ svn после `messages.redact`, адрес) уходят только в `ErrorInfo.svn`
и попадают в ответ при `details`.
"""
from __future__ import annotations

import logging
from typing import Optional

from .. import folderlock, importer, localrepo, newproject, publish, templates
from ..config import SettingsUnreadable
from ..messages import redact
from ..svn_core import ErrorKind, SvnError
from . import texts
from .result import (EXIT_ARGS, EXIT_FOLDER_BUSY, EXIT_HUMAN, EXIT_NEED_LOGIN, EXIT_NO_CONNECTION, EXIT_NO_RIGHTS,
                     EXIT_NO_SVN, EXIT_OTHER, EXIT_POLICY, ErrorInfo)

log = logging.getLogger("svnhelper.ops")

RAW_LIMIT = 2000        # знаков ответа svn в блоке подробностей

# вид ошибки svn -> (kind ответа, код возврата, можно ли повторить, что делать агенту)
_SVN_KINDS = {
    ErrorKind.AUTH: ("auth", EXIT_NEED_LOGIN, False, "human_login"),
    ErrorKind.AUTHZ: ("authz", EXIT_NO_RIGHTS, False, ""),
    ErrorKind.NO_CONNECTION: ("no_connection", EXIT_NO_CONNECTION, True, "wait"),
    ErrorKind.TIMEOUT: ("timeout", EXIT_NO_CONNECTION, True, "wait"),
    ErrorKind.NOT_FOUND: ("not_found", EXIT_OTHER, False, ""),
    ErrorKind.CANCELLED: ("cancelled", EXIT_OTHER, False, ""),
    ErrorKind.LOCKED: ("locked", EXIT_NO_RIGHTS, False, "wait"),
    ErrorKind.OUT_OF_DATE: ("out_of_date", EXIT_HUMAN, False, "get_new"),
    ErrorKind.EXISTS: ("exists", EXIT_OTHER, False, ""),
    ErrorKind.UNRELATED: ("unrelated", EXIT_OTHER, False, ""),
    ErrorKind.BUSY: ("file_busy", EXIT_HUMAN, False, "human_resolve_conflict"),
    ErrorKind.LOCK_REQUIRED: ("lock_required", EXIT_HUMAN, False, "take_lock"),
    ErrorKind.READ_ONLY_LINE: ("read_only_line", EXIT_NO_RIGHTS, False, ""),
    ErrorKind.LOCAL_HISTORY_MISSING: ("local_history_missing", EXIT_HUMAN, False, "human_resolve_conflict"),
    ErrorKind.OTHER: ("failed", EXIT_OTHER, False, ""),
}

# код PublishError -> (kind ответа, код возврата, можно ли повторить)
_PUBLISH_CODES = {
    publish.IN_PROGRESS: ("folder_busy", EXIT_FOLDER_BUSY, True),
    publish.LOCK_UNAVAILABLE: ("lock_unavailable", EXIT_FOLDER_BUSY, True),
    publish.NO_RIGHTS: ("authz", EXIT_NO_RIGHTS, False),
    publish.NEED_LOGIN: ("auth", EXIT_NEED_LOGIN, False),
    publish.NO_CONNECTION: ("no_connection", EXIT_NO_CONNECTION, True),
    publish.LOCAL_CHANGES: ("local_changes", EXIT_HUMAN, False),
    publish.SVN_BUSY: ("file_busy", EXIT_HUMAN, False),
    publish.RESUMABLE: ("publish_failed", EXIT_HUMAN, False),
    publish.UNFINISHED: ("publish_failed", EXIT_HUMAN, False),
}

# код LocalRepoError -> (kind ответа, код возврата, можно ли повторить)
_LOCALREPO_CODES = {
    localrepo.IN_PROGRESS: ("folder_busy", EXIT_FOLDER_BUSY, True),
    localrepo.BUSY: ("file_busy", EXIT_HUMAN, False),
    localrepo.BROKEN_COPY: ("local_history_failed", EXIT_HUMAN, False),
}


class OpsError(Exception):
    """Отказ операции с готовым видом ошибки (проект не найден, неверные параметры, нет svn)."""

    def __init__(self, kind: str, exit_code: int = EXIT_OTHER, message: str = "", next_step: str = "",
                 data: Optional[dict] = None, svn: Optional[dict] = None, retryable: bool = False,
                 untrusted_fields: Optional[list] = None):
        default_message, default_step = texts.ERRORS.get(kind, texts.ERRORS["failed"])
        super().__init__(message or default_message)
        self.kind = kind
        self.exit_code = exit_code
        self.message = message or default_message
        self.next_step = next_step or default_step
        self.data = data or {}
        self.svn = svn or {}
        self.retryable = retryable
        self.untrusted_fields = untrusted_fields or []


def no_svn() -> OpsError:
    return OpsError("no_svn", EXIT_NO_SVN)


def invalid_args(message: str) -> OpsError:
    return OpsError("invalid_args", EXIT_ARGS, message)


def refused(reason: str, ref: str = "") -> OpsError:
    return OpsError("path_refused", EXIT_POLICY, f"{texts.ERRORS['path_refused'][0]} {reason}",
                    svn={"ref": ref} if ref else None)


def svn_block(exc: Optional[BaseException]) -> dict:
    """Подробности для администратора: коды и ответ svn без паролей."""
    if isinstance(exc, SvnError):
        block = {"kind": exc.kind.value, "codes": sorted(exc.codes)}
        if exc.raw:
            block["raw"] = redact(exc.raw)[-RAW_LIMIT:]
        return block
    return {"detail": redact(str(exc))[-RAW_LIMIT:]} if exc is not None else {}


def _build(kind: str, exit_code: int, retryable: bool, next_action: str = "", message: str = "",
           next_step: str = "", svn: Optional[dict] = None) -> ErrorInfo:
    default_message, default_step = texts.ERRORS.get(kind, texts.ERRORS["failed"])
    return ErrorInfo(kind, message or default_message, next_step or default_step, retryable, exit_code, next_action,
                     svn or {})


def from_exception(exc: BaseException) -> ErrorInfo:
    """Ошибка для ответа. Неизвестное исключение: kind=internal, подробности в журнале."""
    if isinstance(exc, OpsError):
        return ErrorInfo(exc.kind, exc.message, exc.next_step, exc.retryable, exc.exit_code, svn=exc.svn)
    if isinstance(exc, SvnError):
        kind, code, retry, action = _SVN_KINDS[exc.kind]
        return _build(kind, code, retry, action, svn=svn_block(exc))
    if isinstance(exc, publish.PublishError):
        kind, code, retry = _PUBLISH_CODES.get(exc.code, ("publish_failed", EXIT_OTHER, False))
        block = {"code": exc.code, "stage": exc.stage, **svn_block(exc.error)}
        return _build(kind, code, retry, "wait" if retry else "", exc.message, svn=block)
    if isinstance(exc, localrepo.LocalRepoError):
        kind, code, retry = _LOCALREPO_CODES.get(exc.code, ("local_history_failed", EXIT_OTHER, False))
        block = {"code": exc.code, "rolled_back": exc.rolled_back, **svn_block(exc.error)}
        return _build(kind, code, retry, "wait" if retry else "", exc.message, svn=block)
    if isinstance(exc, importer.CreateError):
        base = from_exception(exc.error)
        base.kind, base.svn = "create_failed", {**base.svn, "stage": exc.stage, "rolled_back": exc.rolled_back}
        return base
    if isinstance(exc, newproject.ServerCreateFailed):
        kind, code, retry, _action = _SVN_KINDS[exc.kind]
        return _build("create_failed", code, retry, message=exc.text,
                      svn={"kind": exc.kind.value, "rolled_back": exc.rolled_back, "raw": redact(exc.raw)[-RAW_LIMIT:]})
    if isinstance(exc, newproject.ServerCreatedNotDownloaded):
        # адрес на сервере в сообщении не нужен: он только в блоке подробностей
        return _build("created_not_downloaded", EXIT_HUMAN, False, svn={"url": exc.project_url, **svn_block(exc.error)})
    if isinstance(exc, folderlock.FolderBusy):
        return _build("folder_busy", EXIT_FOLDER_BUSY, True, "wait", svn={"path": exc.path, "holder": exc.holder or {}})
    if isinstance(exc, folderlock.LockUnavailable):
        return _build("lock_unavailable", EXIT_FOLDER_BUSY, True, "wait", svn=svn_block(exc.error))
    if isinstance(exc, folderlock.LockTimeout):
        return _build("settings_busy", EXIT_FOLDER_BUSY, True, "wait", svn={"name": exc.name})
    if isinstance(exc, SettingsUnreadable):         # файл настроек занят другой программой: как «settings_busy», можно повторить
        return _build("settings_busy", EXIT_FOLDER_BUSY, True, "wait", svn=svn_block(exc))
    if isinstance(exc, templates.TemplateError):
        return _build("template_error", EXIT_OTHER, False, message=str(exc))
    if isinstance(exc, OSError):
        log.warning("Ошибка файловой операции: %s", exc)
        return _build("io_error", EXIT_OTHER, False, svn=svn_block(exc))
    log.error("Непредвиденная ошибка операции", exc_info=exc)
    return _build("internal", EXIT_OTHER, False, svn={"type": type(exc).__name__, **svn_block(exc)})
