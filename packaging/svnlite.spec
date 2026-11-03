# -*- mode: python ; coding: utf-8 -*-
# Console program svnlite.exe: CLI over svnhelper.ops plus the "mcp" subcommand (one exe for both).
# Unlike SVN-lite.spec: console=True (stdout is the result / the MCP protocol), upx=False (antivirus dislikes packed
# console exes), tkinter excluded (the window is a separate exe).
import importlib.util
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))   # source root; this spec lives in packaging/

# The "mcp" package is required: svnlite.exe is the one exe that carries the assistant server ("svnlite mcp"), so a
# build without it must fail loudly instead of producing an exe that cannot serve the assistant.
if importlib.util.find_spec('mcp') is None:
    raise SystemExit("svnlite.spec: the 'mcp' package is not installed, the exe would not contain the assistant "
                     "server. Run: python -m pip install -r requirements-mcp.txt")

# svnhelper.mcp_server is imported lazily (by name) from svnhelper.cli, so PyInstaller cannot see it. The rest of "mcp"
# is found by PyInstaller from the byte code; the list below is what is loaded by name, the exact list must be checked
# by running the built exe ("svnlite mcp" over stdio). anyio loads its backends by name, so its submodules are collected.
hiddenimports = ['svnhelper.mcp_server', 'mcp.server.lowlevel', 'mcp.server.stdio']
hiddenimports += collect_submodules('anyio')
# pywin32 (a dependency of mcp on Windows) is used for child processes; add the modules that exist in this environment
hiddenimports += [name for name in ('pywintypes', 'win32api', 'win32job', 'win32con')
                  if importlib.util.find_spec(name) is not None]

a = Analysis(
    [os.path.join(ROOT, 'svnlite_cli.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='svnlite',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
