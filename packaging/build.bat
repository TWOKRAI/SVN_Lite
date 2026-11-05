@echo off
rem Builds SVN-lite.exe (window, no console) and svnlite.exe (console: CLI + "mcp" subcommand for AI assistants).
rem Requires Python 3.10+. Settings live in SVN-lite.spec and svnlite.spec.
rem Everything PyInstaller produces goes to out\ (out\work = temporary files, out\dist = what to hand out).
rem The sources stay untouched. out\ can be deleted at any time (the build environment is created again).
rem
rem The build runs in its own virtual environment out\venv_build, not in the global Python: PyInstaller follows every
rem package it finds, and a global Python with several Qt bindings (PyQt5 and PySide6) or other big libraries makes
rem it fail ("multiple Qt bindings") or bloats the exe. The environment holds only requirements-build.txt (PyInstaller,
rem pinned) and requirements-mcp.txt.
rem A half-created or broken environment (its python or pip does not start, the base Python was removed) is deleted
rem and created again, once. STEP names the current step: BUILD FAILED says which one failed.
cd /d "%~dp0.."

set "VENV=out\venv_build"
set "PY=%VENV%\Scripts\python.exe"
set "VENV_RETRIED="

:venv
set "STEP=create the build environment %VENV%. Is Python 3.10+ installed and does python start from this console?"
if not exist "%PY%" (
    echo Creating build environment %VENV% ...
    python -m venv "%VENV%"
    if errorlevel 1 goto :venv_broken
)
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 goto :venv_broken

set "STEP=install requirements-build.txt and requirements-mcp.txt into %VENV%. See the pip messages above: is the internet available?"
"%PY%" -m pip install --disable-pip-version-check --quiet -r requirements-build.txt -r requirements-mcp.txt
if errorlevel 1 goto :fail
set "STEP=import the mcp package in %VENV%"
"%PY%" -c "import mcp"
if errorlevel 1 goto :fail

set "STEP=build out\dist\SVN-lite.exe. See the PyInstaller messages above"
"%PY%" -m PyInstaller --clean --noconfirm --workpath "out\work" --distpath "out\dist" "packaging\SVN-lite.spec"
if errorlevel 1 goto :fail

rem Second exe: console program svnlite.exe. It must contain the assistant server ("svnlite mcp"), so the "mcp"
rem package is mandatory (installed and checked above; the spec itself stops the build if it is missing).
set "STEP=build out\dist\svnlite.exe. See the PyInstaller messages above"
"%PY%" -m PyInstaller --clean --noconfirm --workpath "out\work" --distpath "out\dist" "packaging\svnlite.spec"
if errorlevel 1 goto :fail

rem Smoke check of the built console exe (the assistant server itself is checked by hand).
set "STEP=start the built svnlite.exe: it should print its version"
out\dist\svnlite.exe --version
if errorlevel 1 goto :fail

rem Put the admin config next to the exe, but never overwrite an edited one.
if not exist "out\dist\svn_helper.json" copy /y "svn_helper.json" "out\dist\svn_helper.json" >nul

rem Put the cheat sheet next to the exe (always refreshed). Wildcard instead of the Cyrillic file name:
rem batch files in a non-OEM code page cannot reliably spell it.
if exist "*.html" copy /y "*.html" "out\dist\" >nul

echo.
echo Done: out\dist\SVN-lite.exe, out\dist\svnlite.exe and out\dist\svn_helper.json
exit /b 0

:venv_broken
rem The environment does not work: python or pip of out\venv_build does not start. Recreate it, but only once.
if defined VENV_RETRIED goto :venv_failed
set "VENV_RETRIED=1"
echo.
echo Build environment %VENV% is broken or half-created: its python or pip does not start.
echo Deleting it and creating it again ...
if exist "%VENV%" rmdir /s /q "%VENV%"
if exist "%VENV%" goto :venv_failed
goto :venv

:venv_failed
echo.
echo Could not create a working build environment %VENV%.
echo Close programs that may use files in it, check that "python --version" works in this console
echo (Python 3.10 or newer), delete the folder %VENV% by hand if it is still there, and run build.bat again.

:fail
echo.
echo BUILD FAILED: could not %STEP%
exit /b 1
