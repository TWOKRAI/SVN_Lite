@echo off
chcp 65001 >nul
rem One click demo: starts a local demo server (recreated from scratch every time) and opens SVN-lite
rem with demo settings. Your own settings and the real server are not touched.
rem Stop the demo server later with: powershell -ExecutionPolicy Bypass -File demo\start_demo_server.ps1 -Stop
cd /d "%~dp0.."

echo Готовлю демо-сервер, подождите...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_demo_server.ps1" -Reset
if errorlevel 1 (
    echo.
    echo Не получилось подготовить демо. Прочтите сообщение выше.
    pause
    exit /b 1
)

set "SVNLITE_DATA_DIR=%TEMP%\svnlite_demo\data"
echo.
echo Открываю SVN-lite с демо-настройками. Логин ivanov, пароль demo1.
if exist "out\dist\SVN-lite.exe" (
    start "" "out\dist\SVN-lite.exe"
) else (
    start "" pythonw "svn_helper.py"
)
exit /b 0
