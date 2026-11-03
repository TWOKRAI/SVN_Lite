@echo off
rem Builds SVN-lite.exe (window, no console) and svnlite.exe (console: CLI + "mcp" subcommand for AI assistants).
rem Requires Python 3.10+ and PyInstaller. Settings live in SVN-lite.spec and svnlite.spec.
rem Everything PyInstaller produces goes to out\ (out\work = temporary files, out\dist = what to hand out).
rem The sources stay untouched. out\ can be deleted at any time.
cd /d "%~dp0.."

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found, installing...
    python -m pip install pyinstaller
    if errorlevel 1 goto :fail
)

python -m PyInstaller --clean --noconfirm --workpath "out\work" --distpath "out\dist" "packaging\SVN-lite.spec"
if errorlevel 1 goto :fail

rem Second exe: console program svnlite.exe. If the "mcp" package is installed it is bundled, so "svnlite mcp"
rem works; otherwise the exe still builds and "svnlite mcp" reports that the assistant server is unavailable.
python -m PyInstaller --clean --noconfirm --workpath "out\work" --distpath "out\dist" "packaging\svnlite.spec"
if errorlevel 1 goto :fail

rem Put the admin config next to the exe, but never overwrite an edited one.
if not exist "out\dist\svn_helper.json" copy /y "svn_helper.json" "out\dist\svn_helper.json" >nul

rem Put the cheat sheet next to the exe (always refreshed). Wildcard instead of the Cyrillic file name:
rem batch files in a non-OEM code page cannot reliably spell it.
if exist "*.html" copy /y "*.html" "out\dist\" >nul

echo.
echo Done: out\dist\SVN-lite.exe, out\dist\svnlite.exe and out\dist\svn_helper.json
exit /b 0

:fail
echo.
echo BUILD FAILED
exit /b 1
