@echo off
rem ============================================================================
rem 装一次就行。本文件干五件事: 取爬虫 -> 建 venv -> 装 harness 依赖 -> 装爬虫依赖 -> 装浏览器内核。
rem 三个服务(8770/8771/8780)共用这一个 venv, 所以依赖只装一份。
rem
rem 包里要是有 vendor\ (那是"离线包"), 依赖轮子、浏览器内核、两个爬虫都从那儿拿, 装的时候不用联网;
rem 没有 vendor\ 就是普通包, 该下什么现下。
rem ============================================================================
setlocal
chcp 936 >nul 2>nul
cd /d "%~dp0"

echo == harness 安装 ==
echo.

rem ---------- 1) 取两个爬虫 ----------
rem 爬虫不在这个包里, 是两个独立仓库; 一般由 fetch_crawlers.bat 现取(没有 git 就下 ZIP)。
echo [1/5] 取两个爬虫 ...
if exist "vendor\crawlers\nga\nga_search_server.py" goto vendor_crawlers
call fetch_crawlers.bat
if errorlevel 1 goto fail_crawlers
goto step_venv

:vendor_crawlers
echo     离线包: 爬虫用包里带的。
if exist "crawlers\nga\nga_search_server.py" goto vc_bili
if exist "crawlers\nga\browser_data" goto vc_bili
xcopy /e /i /q /y "vendor\crawlers\nga" "crawlers\nga" >nul
:vc_bili
if exist "crawlers\bili\bili_search_server.py" goto step_venv
if exist "crawlers\bili\browser_data" goto step_venv
xcopy /e /i /q /y "vendor\crawlers\bili" "crawlers\bili" >nul
goto step_venv

:fail_crawlers
echo [X] 爬虫没取全。先把网络弄通, 再跑一遍本脚本。
pause
exit /b 1

rem ---------- 2) 建 venv ----------
:step_venv
set "PY="
py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3.11"
if not defined PY (
  py -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [X] 没找到 Python。去 python.org 装 3.11, 装的时候记得勾 "Add python.exe to PATH"。
  echo     装完再回来点这个脚本。
  pause
  exit /b 1
)
echo [2/5] 建 %PY% 的 venv .venv ...
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [X] 建 venv 失败。
    pause
    exit /b 1
  )
) else (
  echo     已存在, 跳过创建。
)
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -V || (echo [X] venv 里的 python 起不来 & pause & exit /b 1)

rem 依赖从哪来: 有 vendor\wheels 就只用它(不联网), 否则走 PyPI。
set "PIPSRC="
if exist "vendor\wheels" set "PIPSRC=--no-index --find-links vendor\wheels"
if not defined PIPSRC "%VPY%" -m pip install --upgrade pip --quiet

rem ---------- 3) harness 依赖 ----------
echo [3/5] 装 harness 依赖(就 tiktoken 一个, 可选) ...
"%VPY%" -m pip install %PIPSRC% -r requirements.txt
if errorlevel 1 echo [!] harness 依赖没装全。tiktoken 只是可选, 不影响能不能跑。

rem ---------- 4) 爬虫依赖 ----------
echo [4/5] 装爬虫依赖(bili 那份比较多, 要等几分钟) ...
"%VPY%" -m pip install %PIPSRC% -r crawlers\bili\requirements.txt
if errorlevel 1 (
  echo [X] bili 依赖装失败。常见原因是网络。换个源重来就行, 例如:
  echo     .venv\Scripts\python.exe -m pip install -r crawlers\bili\requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  pause
  exit /b 1
)
"%VPY%" -m pip install %PIPSRC% -r crawlers\nga\requirements.txt
if errorlevel 1 (
  echo [X] NGA 依赖装失败。
  pause
  exit /b 1
)

rem ---------- 5) 浏览器内核 ----------
rem 两个都要: bili 那份写死了 channel="chrome", NGA 用系统 Chromium。
rem playwright 认固定那几个落点(Windows 上是 %LOCALAPPDATA%\ms-playwright), 所以离线包是把
rem 内核**拷到那个落点** —— 这样起服务时不用额外设环境变量, 跟在线装出来的完全一样。
echo [5/5] 装浏览器内核 chromium + chrome(几百 MB, 看网速慢慢下) ...
if exist "vendor\ms-playwright" goto vendor_browsers
"%VPY%" -m playwright install chromium chrome
if errorlevel 1 echo [!] 内核没装全。可以手动补: .venv\Scripts\python.exe -m playwright install chromium chrome
goto done

:vendor_browsers
echo     离线包: 把包里的内核拷到 %LOCALAPPDATA%\ms-playwright ...
if not exist "%LOCALAPPDATA%\ms-playwright" mkdir "%LOCALAPPDATA%\ms-playwright"
xcopy /e /i /q /y "vendor\ms-playwright" "%LOCALAPPDATA%\ms-playwright" >nul
call :ensure_chrome
if errorlevel 1 echo [!] 浏览器没装全，可以手动补：.venv\Scripts\python.exe -m playwright install chromium chrome
:done
echo.
echo ============ 装完了 ============
echo.
echo 下一步:
echo   1. 复制 config.example.bat 成 config.bat, 填上 DEEPSEEK_API_KEY
echo   2. 双击 start.bat
echo   3. 浏览器打开 http://127.0.0.1:8780
echo.
echo 详细说明见 INSTALL.md。
pause

rem ---------- Chrome 通道(只有离线包这条路才走得到这里) ----------
rem bili 那条腿把 channel="chrome" 写死了, 要系统里有 Chrome。联网装的时候
rem playwright install chrome 会自己下官方 MSI 静默装上; 离线包搬不动那个"安装动作",
rem 就把官方 MSI 一起带上, 这里替它装。系统里已经有 Chrome 就整个跳过 ——
rem Google 只发布最新稳定版那一份 MSI, 拿旧包去装更新的版本会被 msiexec 拒掉。
:ensure_chrome
set "HAVE="
reg query "HKLM\SOFTWARE\Google\Chrome\BLBeacon" /v version >nul 2>nul
if not errorlevel 1 set "HAVE=1"
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "HAVE=1"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "HAVE=1"
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "HAVE=1"
if defined HAVE (
  echo     Chrome 已在系统里, 跳过。
  exit /b 0
)
set "MSI="
for %%f in ("vendor\chrome\*.msi") do set "MSI=%%~ff"
if not defined MSI (
  echo     [!] 系统里没 Chrome, 包里也没带它的安装包 —— bilibili 那条腿起不来, 其它照常。
  echo         补装一句就行: .venv\Scripts\python.exe -m playwright install chrome
  exit /b 0
)
net session >nul 2>&1
if errorlevel 1 (
  echo     [!] 系统里没 Chrome。包里带了官方安装包, 但装它**要管理员权限**。
  echo         关掉这个窗口, 右键 setup.bat 选「以管理员身份运行」, 再跑一遍;
  echo         或者自己下一个 Chrome 装上。两条路都行。
  echo         不装也不影响 NGA, 只是 bilibili 那条腿起不来。
  exit /b 1
)
echo     系统里没 Chrome, 用包里那份官方安装包装上(一两分钟, 别关窗口) ...
msiexec /i "%MSI%" /quiet /norestart NOGOOGLEUPDATEPING=1
if errorlevel 1 (
  echo     [!] Chrome 没装上。自己下一个 Chrome 装上也行。
  exit /b 1
)
echo     Chrome 装好了。
exit /b 0

