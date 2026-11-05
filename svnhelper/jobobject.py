"""Объект «задание» Windows: дочерние процессы (svn.exe) завершаются вместе с программой, которая их запустила.

Нужен консольной программе `svnlite` и серверу MCP: клиент (Claude Desktop, Claude Code), убивший программу посреди
вызова, не должен оставить висящий svn.exe. Раньше код жил в `mcp_server.py` и защищал только сервер; теперь он общий
(`agent_env.harden` подключает его и в `cli.main`). На других системах ничего не делает.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("svnhelper.jobobject")

_JOB_HANDLES: list = []         # дескриптор задания живёт до конца процесса: закрытие убивает дочерние процессы
_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9


def bind_children_to_process() -> bool:
    """Windows: процессы, запущенные этим процессом, завершаются вместе с ним (признак KILL_ON_JOB_CLOSE).
    Повторный вызов ничего не делает. Возвращает True, если привязка есть."""
    if sys.platform != "win32":
        return False
    if _JOB_HANDLES:
        return True
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes",
                                                         "OtherBytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("Basic", BasicLimits), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.Basic.LimitFlags = _KILL_ON_JOB_CLOSE
    ok = bool(job) and kernel.SetInformationJobObject(job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                                                      ctypes.sizeof(limits)) \
        and kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())
    if not ok:
        log.warning("Не удалось привязать svn.exe к процессу программы (код %s)", ctypes.get_last_error())
        return False
    _JOB_HANDLES.append(job)
    return True
