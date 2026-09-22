@echo off
rem ============================================================================
rem 把两个爬虫取到 crawlers\nga 与 crawlers\bili —— 已经在了就跳过。
rem 取法按顺序试: git clone 直连 -> git clone 走镜像 -> 下 ZIP 走镜像。
rem （国内直连 github.com 常被断; 直连能通的地方镜像根本不参与。）
rem 正常是被 setup.bat 调的, 一般不用自己点。
rem ============================================================================
setlocal
chcp 936 >nul 2>nul
cd /d "%~dp0"

set "OWNER=MaIOqwq"
rem 第三方镜像, 只在直连失败时才用得上。想换就改这一行。
set "MIRROR=https://ghfast.top/"

call :fetch nga  game-harness-crawler-nga
if errorlevel 1 goto fail
call :fetch bili game-harness-crawler-bili
if errorlevel 1 goto fail

echo [OK] 两个爬虫都在 crawlers\ 下了。
exit /b 0

:fail
echo.
echo [X] 爬虫没取全。可以手工补：去 https://github.com/%OWNER% 下对应仓库的 ZIP，
echo     解压后把**里面的文件**放进 crawlers\nga（或 crawlers\bili）——
echo     注意别把解压出来的那一层文件夹整个塞进去。
exit /b 1

rem ---------------------------------------------------------------------------
:fetch
rem %1 = 平台目录名(nga/bili), %2 = 仓库名
set "PLAT=%~1"
set "REPO=%~2"
set "DEST=crawlers\%PLAT%"

if exist "%DEST%\*_search_server.py" goto already

rem 有登录态的目录一律不碰：那里面是扫过的码，清掉就得重扫。宁可让人工处理。
if exist "%DEST%\browser_data" goto keep
if exist "%DEST%\.tool_token" goto keep

where git >nul 2>nul
if errorlevel 1 goto zip

set "GURL1=https://github.com/%OWNER%/%REPO%.git"
set "GURL2=%MIRROR%https://github.com/%OWNER%/%REPO%.git"

echo [1/3] git clone 直连 %REPO% ...
call :trygit "%GURL1%"
if exist "%DEST%\*_search_server.py" goto fetched

echo [2/3] git clone 走镜像 %REPO% ...
call :trygit "%GURL2%"
if exist "%DEST%\*_search_server.py" goto fetched

:zip
echo [3/3] 下 ZIP 解压 %REPO% ...
rmdir /s /q "%DEST%" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $u='%MIRROR%https://github.com/%OWNER%/%REPO%/archive/refs/heads/main.zip'; $z=Join-Path $env:TEMP '%REPO%.zip'; $t=Join-Path $env:TEMP '%REPO%-x'; if(Test-Path $t){Remove-Item -Recurse -Force $t}; if(Test-Path $z){Remove-Item -Force $z}; Invoke-WebRequest -Uri $u -OutFile $z; Expand-Archive -Path $z -DestinationPath $t -Force; $inner=(Get-ChildItem $t -Directory)[0]; New-Item -ItemType Directory -Force '%DEST%' | Out-Null; Copy-Item (Join-Path $inner.FullName '*') '%DEST%' -Recurse -Force; Remove-Item -Recurse -Force $t; Remove-Item -Force $z"
if errorlevel 1 exit /b 1
if exist "%DEST%\*_search_server.py" goto fetched
echo [X] %PLAT% 取回来不对：里面没有 *_search_server.py。
exit /b 1

:trygit
rem %1 = 仓库地址。成不成看 DEST 里有没有东西, 不看这里的返回值。
rmdir /s /q "%DEST%" 2>nul
git clone --depth 1 "%~1" "%DEST%"
if errorlevel 1 exit /b 1
if exist "%DEST%\.git" rmdir /s /q "%DEST%\.git"
exit /b 0

:already
echo [skip] crawlers\%PLAT% 已经有了。
exit /b 0

:keep
echo [!] crawlers\%PLAT% 里有登录态(browser_data / .tool_token)，不覆盖。
echo     真要重取，先把那个目录自己删掉再跑一遍。
exit /b 0

:fetched
echo [ok] %PLAT% 取好了。
exit /b 0
