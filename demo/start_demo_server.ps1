# SVN-lite: локальный демо-сервер для презентации. Боевой сервер 192.168.11.11 не трогается.
#
# Запуск (из папки программы):
#   powershell -ExecutionPolicy Bypass -File demo\start_demo_server.ps1          # поднять (или пересоздать с -Reset)
#   powershell -ExecutionPolicy Bypass -File demo\start_demo_server.ps1 -Stop    # остановить
#
# Что делает: временная папка %TEMP%\svnlite_demo, хранилище «demo» (svnadmin create), пользователи
# ivanov / petrov / boss, svnserve только на 127.0.0.1 и нестандартном порту 13690, тестовый проект
# и папка-«витрина» (как Z:), из которой на показе создаётся новый проект.
# svnadmin и svnserve ищутся так же, как в tests\test_real_svn.py: по PATH (плюс папки TortoiseSVN и VisualSVN).

param(
    [int]$Port = 13690,
    [string]$Root = (Join-Path $env:TEMP "svnlite_demo"),
    [switch]$Reset,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$PidFile = Join-Path $Root "svnserve.pid"
$Users = [ordered]@{ "ivanov" = "demo1"; "petrov" = "demo2"; "boss" = "demo3" }
$Utf8 = New-Object System.Text.UTF8Encoding($false)   # без BOM: svnserve не понимает BOM в своих файлах

function Find-Tool([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @("$env:ProgramFiles\TortoiseSVN\bin", "$env:ProgramFiles\VisualSVN Server\bin",
                       "${env:ProgramFiles(x86)}\VisualSVN Server\bin")) {
        $candidate = Join-Path $dir "$name.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Stop-DemoServer {
    if (Test-Path $PidFile) {
        $id = [int](Get-Content $PidFile)
        $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -like "svnserve*") {
            Stop-Process -Id $id -Force
            Write-Host "Демо-сервер остановлен (процесс $id)."
        }
        Remove-Item $PidFile -Force
    } else {
        Write-Host "Демо-сервер не запущен (нет файла $PidFile)."
    }
}

function Write-Text([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText($path, $text, $Utf8)
}

function Invoke-Svn([string[]]$arguments) {
    $all = $arguments + @("--non-interactive", "--no-auth-cache", "--username", "ivanov", "--password", $Users["ivanov"])
    & $script:Svn @all | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "svn $($arguments[0]) завершился с ошибкой $LASTEXITCODE" }
}

if ($Stop) { Stop-DemoServer; exit 0 }

$Svnadmin = Find-Tool "svnadmin"
$Svnserve = Find-Tool "svnserve"
$script:Svn = Find-Tool "svn"
$missing = @()
if (-not $Svnadmin) { $missing += "svnadmin.exe" }
if (-not $Svnserve) { $missing += "svnserve.exe" }
if (-not $script:Svn) { $missing += "svn.exe" }
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "Не найдены программы: $($missing -join ', ')." -ForegroundColor Red
    Write-Host "Их ставит TortoiseSVN с пунктом «command line client tools» (Установка → Modify)"
    Write-Host "или пакет Subversion для Windows (например, VisualSVN Server / SlikSVN). Добавьте папку bin в PATH."
    exit 1
}

if (Test-Path $Root) {
    if (-not $Reset) {
        Write-Host "Папка демо уже есть: $Root" -ForegroundColor Yellow
        Write-Host "Чтобы пересоздать всё с нуля, запустите с ключом -Reset (папка будет удалена)."
        exit 1
    }
    Stop-DemoServer
    Start-Sleep -Milliseconds 500
    Remove-Item $Root -Recurse -Force
}

# порт свободен?
$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
try { $listener.Start(); $listener.Stop() } catch {
    Write-Host "Порт $Port занят. Укажите другой: -Port 13691" -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Path $Root | Out-Null
$Repos = Join-Path $Root "repos"
New-Item -ItemType Directory -Path $Repos | Out-Null
$Repo = Join-Path $Repos "demo"
& $Svnadmin create $Repo
if ($LASTEXITCODE -ne 0) { throw "svnadmin create не удался" }

Write-Text (Join-Path $Repo "conf\svnserve.conf") @"
[general]
anon-access = none
auth-access = write
password-db = passwd
authz-db = authz
realm = SVN-lite demo
"@
$passwd = "[users]`n" + (($Users.GetEnumerator() | ForEach-Object { "$($_.Key) = $($_.Value)" }) -join "`n") + "`n"
Write-Text (Join-Path $Repo "conf\passwd") $passwd
# простой режим: все правят основной вариант (с блокировкой файлов), начальник видит журнал и может откатить
Write-Text (Join-Path $Repo "conf\authz") @"
[groups]
staff = ivanov, petrov
bosses = boss

[/]
@staff = rw
@bosses = rw
"@

$proc = Start-Process -FilePath $Svnserve -PassThru -WindowStyle Hidden -ArgumentList @(
    "-d", "--foreground", "--listen-host", "127.0.0.1", "--listen-port", "$Port", "-r", "`"$Repos`"")
Set-Content -Path $PidFile -Value $proc.Id
Start-Sleep -Seconds 1
if ($proc.HasExited) { throw "svnserve не запустился (порт $Port?)" }

$Url = "svn://127.0.0.1:$Port/demo"
$Work = Join-Path $Root "tmp_wc"

# правила хранилища: чертежи и документы нужно «занимать» перед правкой (только чтение, пока не занят)
Invoke-Svn @("checkout", "--depth", "empty", $Url, $Work)
$autoProps = "*.cdw = svn:needs-lock=*`n*.m3d = svn:needs-lock=*`n*.a3d = svn:needs-lock=*`n*.spw = svn:needs-lock=*`n" +
             "*.dwg = svn:needs-lock=*`n*.docx = svn:needs-lock=*`n*.xlsx = svn:needs-lock=*`n"
$propFile = Join-Path $Root "autoprops.txt"
Write-Text $propFile $autoProps
Invoke-Svn @("propset", "svn:auto-props", "-F", $propFile, $Work)
Invoke-Svn @("commit", "-m", "Правила хранилища: чертежи занимаются перед правкой", $Work)
Remove-Item $Work -Recurse -Force

# «витрина» вместо Z:\Проекты_SVN (на показе это обычная локальная папка)
$Showcase = Join-Path $Root "Z\Проекты_SVN"
function New-SampleProject([string]$path, [string]$title) {
    New-Item -ItemType Directory -Path (Join-Path $path "Сборки") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $path "Документы") -Force | Out-Null
    Write-Text (Join-Path $path "Сборки\Рама.cdw") "Демо-чертёж: рама ($title)"
    Write-Text (Join-Path $path "Сборки\Привод.cdw") "Демо-чертёж: привод ($title)"
    Write-Text (Join-Path $path "Документы\Спецификация.txt") "Спецификация $title`r`nПоз. 1  Рама  1 шт.`r`n"
    Write-Text (Join-Path $path "Документы\Заметки.txt") "Заметки по проекту $title`r`n"
}

# 1) проект, который уже есть в хранилище (его скачивают по пути с «витрины»)
$Existing = Join-Path $Showcase "Азов\Конвейер линия 1"
New-SampleProject $Existing "Конвейер линия 1"
$ExistingUrl = "$Url/Проекты_SVN/Азов/Конвейер линия 1"
Invoke-Svn @("mkdir", "--parents", "-m", "Создан проект Конвейер линия 1", "$ExistingUrl/trunk", "$ExistingUrl/branches", "$ExistingUrl/tags")
Push-Location $Existing
try { Invoke-Svn @("import", ".", "$ExistingUrl/trunk", "-m", "Первая версия проекта") } finally { Pop-Location }

# 2) папка, которой ещё нет в хранилище: из неё на показе создаём проект (с мусором, чтобы показать фильтр)
$New = Join-Path $Showcase "Азов\Отбраковщик и делитель"
New-SampleProject $New "Отбраковщик и делитель"
Write-Text (Join-Path $New "Thumbs.db") "мусор"
Write-Text (Join-Path $New "Документы\Спецификация.bak") "старая копия"

$Projects = Join-Path $Root "Мои проекты"
New-Item -ItemType Directory -Path $Projects -Force | Out-Null

# Готовые настройки для демо: отдельная папка данных (SVNLITE_DATA_DIR), личные настройки не затрагиваются.
# Общий конфиг здесь локальный файл, поэтому боевой \\192.168.11.11\common не читается.
$DataDir = Join-Path $Root "data"
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
$SharedFile = Join-Path $DataDir "svn_helper.json"
$shared = [ordered]@{
    server  = "svn://127.0.0.1:$Port"
    repos   = @("demo")
    work_dir = $Projects
    username = "ivanov"
    mirrors = @(@{ path = $Showcase; repo = "demo/Проекты_SVN" })
}
[System.IO.File]::WriteAllText($SharedFile, ($shared | ConvertTo-Json -Depth 5), $Utf8)
$settings = [ordered]@{ config_path = $SharedFile; username = "ivanov"; last_dir = $Projects; theme = "system" }
[System.IO.File]::WriteAllText((Join-Path $DataDir "settings.json"), ($settings | ConvertTo-Json), $Utf8)

Write-Host ""
Write-Host "Демо-сервер запущен: $Url (только на этом компьютере)" -ForegroundColor Green
Write-Host ""
Write-Host "Проще всего: запустите demo\start_demo.bat. Настройки вводить не нужно, программа откроется"
Write-Host "с демо-настройками (папка данных: $DataDir)."
Write-Host "Вручную: Настройки → Параметры (F12) → «Общие настройки (svn_helper.json)» = $SharedFile"
Write-Host "(остальные поля «Свои значения» оставьте пустыми)."
Write-Host ""
Write-Host "Пользователи (логин / пароль):"
foreach ($u in $Users.GetEnumerator()) { Write-Host "  $($u.Key) / $($u.Value)" }
Write-Host ""
Write-Host "Пути для показа (вставлять в поле «Ссылка на проект»):"
Write-Host "  уже в хранилище:  $Existing"
Write-Host "  ещё нет:          $New"
Write-Host ""
Write-Host "Остановить: powershell -ExecutionPolicy Bypass -File demo\start_demo_server.ps1 -Stop"
Write-Host "Личные настройки программы при запуске через start_demo.bat не меняются."
