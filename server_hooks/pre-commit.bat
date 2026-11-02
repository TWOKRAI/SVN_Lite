@echo off
rem SVN-lite: версии (теги) только для чтения (вариант для Windows). Правила - как в pre-commit (sh):
rem   версия = <X>/tags/<имя>/..., если в транзакции или в HEAD есть <X>/trunk;
rem   создать версию копированием можно; внутри существующей версии A/U/D/_U нельзя;
rem   D/R папки <X>/tags и <X>/tags/<имя> нельзя; tags вне проекта (нет <X>/trunk) не затрагиваются.
rem Если svnlook не отработал - коммит отклоняется (fail-closed).
setlocal DisableDelayedExpansion
set "REPOS=%~1"
set "TXN=%~2"
set "LIST=%TEMP%\svnhook_%RANDOM%_%RANDOM%.txt"
set "FILT=%LIST%.f"
set "ERR=%LIST%.e"
set "DENY="
set "LOOKFAIL="

rem svnlook печатает пути в ANSI-кодовой странице системы, а cmd читает файл в кодовой странице консоли.
rem Без chcp кириллица в путях искажается, и проверка trunk не находит проект.
set "ACP="
for /f "tokens=3" %%c in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Nls\CodePage" /v ACP 2^>nul') do set "ACP=%%c"
if defined ACP chcp %ACP% >nul 2>&1
svnlook changed -t "%TXN%" "%REPOS%" > "%LIST%" 2>"%ERR%"
if errorlevel 1 goto lookfail
set "HEAD="
for /f "delims=" %%r in ('svnlook youngest "%REPOS%"') do set "HEAD=%%r"
if not defined HEAD goto lookfail

rem быстрый отсев: нужен сегмент tags целиком (после 4 символов статуса путь начинается с 5-го)
findstr /R /C:"^....tags/" /C:"^....tags$" /C:"/tags/" /C:"/tags$" "%LIST%" > "%FILT%"

for /f "usebackq delims=" %%L in ("%FILT%") do call :one "%%L"
if defined LOOKFAIL goto lookfail
if defined DENY goto deny

call :cleanup
exit /b 0

:one
if defined DENY exit /b 0
if defined LOOKFAIL exit /b 0
set "LINE=%~1"
set "ACT=%LINE:~0,1%"
set "REST=%LINE:~4%"
if "%REST:~-1%"=="/" set "REST=%REST:~0,-1%"
set "PREFIX="
:walk
set "SEG="
set "TAIL="
for /f "tokens=1* delims=/ eol=/" %%a in ("%REST%") do (
  set "SEG=%%a"
  set "TAIL=%%b"
)
if "%SEG%"=="tags" call :tags
if defined DENY exit /b 0
if defined LOOKFAIL exit /b 0
if not defined TAIL exit /b 0
set "PREFIX=%PREFIX%%SEG%/"
set "REST=%TAIL%"
goto walk

:tags
rem X = PREFIX без последнего "/"; проект ли X
set "X="
if defined PREFIX set "X=%PREFIX:~0,-1%"
set "TP=trunk"
if defined X set "TP=%X%/trunk"
call :exists T "%TP%"
if defined LOOKFAIL exit /b 0
if "%RES%"=="0" call :exists H "%TP%"
if defined LOOKFAIL exit /b 0
if "%RES%"=="0" exit /b 0
rem это проект. сама папка X/tags
if not defined TAIL (
  if "%ACT%"=="D" set "DENY=1"
  if "%ACT%"=="R" set "DENY=1"
  exit /b 0
)
rem TAIL = <имя> или <имя>/<внутри>
set "NAME="
set "INNER="
for /f "tokens=1* delims=/ eol=/" %%a in ("%TAIL%") do (
  set "NAME=%%a"
  set "INNER=%%b"
)
if not defined INNER (
  rem сама версия: добавить можно, остальное нельзя
  if not "%ACT%"=="A" set "DENY=1"
  exit /b 0
)
rem внутри версии: нельзя, если версия уже была в HEAD
set "TAGPATH=tags/%NAME%"
if defined X set "TAGPATH=%X%/tags/%NAME%"
call :exists H "%TAGPATH%"
if defined LOOKFAIL exit /b 0
if "%RES%"=="1" set "DENY=1"
exit /b 0

:exists
rem %1 = T (транзакция) или H (HEAD), %2 = путь. Итог в RES: 1 есть, 0 нет. Иная ошибка svnlook - LOOKFAIL.
set "RES=0"
if "%~1"=="T" (
  svnlook tree -t "%TXN%" "%REPOS%" "%~2" --non-recursive >nul 2>"%ERR%"
) else (
  svnlook tree -r %HEAD% "%REPOS%" "%~2" --non-recursive >nul 2>"%ERR%"
)
if not errorlevel 1 (
  set "RES=1"
  exit /b 0
)
findstr /C:"E160013" "%ERR%" >nul
if errorlevel 1 set "LOOKFAIL=1"
exit /b 0

:cleanup
del "%LIST%" "%FILT%" "%ERR%" 2>nul
exit /b 0

:lookfail
echo svnlook failed: the hook could not read the changes. Contact the administrator. 1>&2
rem svnserve может читать вывод хука не в UTF-8: русский текст идёт после английского
chcp 65001 >nul
echo Хук не смог прочитать изменения (svnlook). Сообщите администратору. 1>&2
if exist "%ERR%" type "%ERR%" 1>&2
call :cleanup
exit /b 1

:deny
call :cleanup
echo A version (tag) is read-only. Make changes in trunk or in a branch. 1>&2
rem svnserve может читать вывод хука не в UTF-8: русский текст идёт после английского
chcp 65001 >nul
echo Версия (тег) только для чтения. Правки делайте в основной линии или ветке. 1>&2
exit /b 1
