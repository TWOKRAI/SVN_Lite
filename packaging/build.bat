@echo off
rem Builds a single SVN-lite.exe (requires Python 3.10+ and PyInstaller). Settings live in SVN-lite.spec.
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

rem Put the admin config next to the exe, but never overwrite an edited one.
if not exist "out\dist\svn_helper.json" copy /y "svn_helper.json" "out\dist\svn_helper.json" >nul

rem Put the cheat sheet next to the exe (always refreshed). Wildcard instead of the Cyrillic file name:
rem batch files in a non-OEM code page cannot reliably spell it.
if exist "*.html" copy /y "*.html" "out\dist\" >nul

echo.
echo Done: out\dist\SVN-lite.exe and out\dist\svn_helper.json
exit /b 0

:fail
echo.
echo BUILD FAILED
exit /b 1
