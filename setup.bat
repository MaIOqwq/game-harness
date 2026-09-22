@echo off
rem ============================================================================
rem 装一次就行。做四件事: 建 venv -> 装 harness -> 装两个爬虫 -> 下浏览器内核。
rem 三个服务(8770/8771/8780)共用这一个 venv, 不是三个各装一份。
rem ============================================================================
setlocal
chcp 936 >nul 2>nul
cd /d "%~dp0"

echo == harness 安装 ==
echo.

rem ---------- 1) 找 Python ----------
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
  echo     装完把命令行关掉重开, 再跑一次这个脚本。
  pause
  exit /b 1
)
echo [1/4] 用 %PY% 建 venv .venv ...
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [X] 建 venv 失败。
    pause
    exit /b 1
  )
) else (
  echo     已存在, 跳过。
)
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -V || (echo [X] venv 里的 python 起不来 & pause & exit /b 1)

"%VPY%" -m pip install --upgrade pip --quiet

rem ---------- 2) harness 本体 ----------
echo [2/4] 装 harness 本体(就 tiktoken 一个, 可选) ...
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 echo [!] harness 依赖没装全 —— tiktoken 只是可选, 不影响能不能跑。

rem ---------- 3) 两个爬虫 ----------
echo [3/4] 装爬虫依赖(bili 那份比较重, 要等几分钟) ...
"%VPY%" -m pip install -r crawlers\bili\requirements.txt
if errorlevel 1 (
  echo [X] bili 依赖装失败。把上面的报错留给排障用 —— 常见原因是网络断在下载途中, 重跑一次往往就好。
  pause
  exit /b 1
)
"%VPY%" -m pip install -r crawlers\nga\requirements.txt
if errorlevel 1 (
  echo [X] NGA 依赖装失败。
  pause
  exit /b 1
)

rem ---------- 4) 浏览器内核 ----------
rem 两个都要: bili 那边写死了 channel="chrome", NGA 优先找系统 Chrome、找不到就用 chromium。
echo [4/4] 下浏览器内核 chromium + chrome(几百 MB, 慢是正常的) ...
"%VPY%" -m playwright install chromium chrome
if errorlevel 1 (
  echo [!] 内核没下全。可以手动补: .venv\Scripts\python.exe -m playwright install chromium chrome
)

echo.
echo ============ 装完了 ============
echo.
echo 下一步:
echo   1. 复制 config.example.bat 成 config.bat, 填上 DEEPSEEK_API_KEY
echo   2. 双击 start.bat
echo   3. 浏览器会自己开到 http://127.0.0.1:8780
echo.
echo 详细说明看 INSTALL.md。
pause
