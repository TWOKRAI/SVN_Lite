"""Точка входа svnlite.exe: консольная программа для чтения (и подкоманда mcp для ИИ-помощника).

Собирает её PyInstaller (packaging/svnlite.spec). Окно SVN-lite (svn_helper.py) это отдельный exe без консоли.
"""
import sys

from svnhelper.cli import main

if __name__ == "__main__":
    sys.exit(main())
