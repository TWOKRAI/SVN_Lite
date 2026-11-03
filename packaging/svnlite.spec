# -*- mode: python ; coding: utf-8 -*-
# Console program svnlite.exe: CLI over svnhelper.ops plus the "mcp" subcommand (one exe for both).
# Unlike SVN-lite.spec: console=True (stdout is the result / the MCP protocol), upx=False (antivirus dislikes packed
# console exes), tkinter excluded (the window is a separate exe).
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))   # source root; this spec lives in packaging/

# svnhelper.mcp_server is imported lazily (by name) from svnhelper.cli, so PyInstaller cannot see it.
hiddenimports = ['svnhelper.mcp_server']
try:
    # The "mcp" package is optional on the build machine: without it the exe still builds, "svnlite mcp" then reports
    # that the helper server is unavailable. anyio loads its backends by name, so its submodules are collected too.
    hiddenimports += collect_submodules('mcp')
    hiddenimports += collect_submodules('anyio')
except ImportError:
    pass

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
