"""Установка TortoiseSVN из msi, который администратор положил на сервер (поле tortoise_installer_path).

msi копируется во временную папку от имени пользователя (повышенный процесс может не видеть сетевую папку),
затем msiexec запускается с запросом прав администратора (UAC) и с ADDLOCAL=ALL, то есть вместе с
«command line client tools» (svn.exe). Модуль не зависит от tkinter.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sys
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("svnhelper.installer")

IS_WINDOWS = sys.platform == "win32"

_SEE_MASK_NOCLOSEPROCESS = 0x40
_SW_SHOWNORMAL = 1
_INFINITE = 0xFFFFFFFF
_ERROR_CANCELLED = 1223        # пользователь отказал в окне UAC

EXIT_OK = 0
EXIT_REBOOT_REQUIRED = 3010
EXIT_REBOOT_INITIATED = 1641
EXIT_USER_CANCELLED = 1602     # отмена в окне самой установки
EXIT_NEWER_INSTALLED = 1638    # уже стоит эта или более новая версия

_OK_CODES = {EXIT_OK, EXIT_REBOOT_REQUIRED, EXIT_REBOOT_INITIATED, EXIT_NEWER_INSTALLED}


@dataclass
class InstallResult:
    ok: bool
    message: str = ""
    reboot: bool = False    # установка прошла, но для окон TortoiseSVN нужна перезагрузка


def msiexec_params(msi: str) -> str:
    """Параметры msiexec: ADDLOCAL=ALL ставит и клиент командной строки (svn.exe)."""
    return f'/i "{msi}" ADDLOCAL=ALL /passive /norestart'


def result_for_exit_code(code: int) -> InstallResult:
    if code in (EXIT_REBOOT_REQUIRED, EXIT_REBOOT_INITIATED):
        return InstallResult(True, "TortoiseSVN установлен. Для окон TortoiseSVN (сохранение, история) "
                                   "перезагрузите компьютер.", reboot=True)
    if code in _OK_CODES:
        return InstallResult(True)
    if code == EXIT_USER_CANCELLED:
        return InstallResult(False, "Установка отменена.")
    return InstallResult(False, f"Установка TortoiseSVN не удалась (код {code}). "
                                "Сообщите администратору.")


def _copy_to_temp(source: str) -> str:
    if not os.path.isfile(source):
        raise FileNotFoundError(f"Файл установки не найден или нет доступа: {source}")
    folder = tempfile.mkdtemp(prefix="svnlite_tsvn_")
    target = os.path.join(folder, os.path.basename(source))
    shutil.copyfile(source, target)
    return target


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def _run_elevated(file: str, params: str) -> Optional[int]:
    """Запускает программу с запросом прав администратора и ждёт код завершения. None — отказ в UAC."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = file
    info.lpParameters = params
    info.nShow = _SW_SHOWNORMAL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        if ctypes.get_last_error() == _ERROR_CANCELLED:
            return None
        raise OSError(f"не удалось запустить установщик (ошибка Windows {ctypes.get_last_error()})")
    kernel32.WaitForSingleObject(info.hProcess, _INFINITE)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(info.hProcess)
    return int(code.value)


def install_tortoise(source: str) -> InstallResult:
    """Копирует msi, запускает установку и ждёт её конца. Вызывать из фонового потока."""
    if not IS_WINDOWS:
        return InstallResult(False, "Установка поддерживается только в Windows.")
    folder = ""
    try:
        local = _copy_to_temp(source)
        folder = os.path.dirname(local)
        log.info("Установка TortoiseSVN из %s", source)
        code = _run_elevated("msiexec.exe", msiexec_params(local))
    except OSError as exc:
        log.error("Установка TortoiseSVN: %s", exc)
        return InstallResult(False, str(exc))
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    if code is None:
        log.warning("Установка TortoiseSVN: отказ в запросе прав администратора")
        return InstallResult(False, "Установка отменена: нужны права администратора. "
                                    "Попросите администратора установить TortoiseSVN.")
    log.info("Установка TortoiseSVN завершена, код %s", code)
    return result_for_exit_code(code)
