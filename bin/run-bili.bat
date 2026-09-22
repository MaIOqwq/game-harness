@echo off
chcp 936 >nul 2>nul
rem 单独起 bilibili 爬虫服务(端口 8771)。平常**不用**跑这个 —— 网页后端会在真用到它的时候按需拉起。
rem 这个是排障/手工起的时候用的。
rem 必须先 cd 进 crawlers\bili: 那里面 browser_data\(登录态) 和 libs\stealth.min.js 都是按相对路径
rem 找的, 换个目录起它就认不出自己的登录态(等于白扫了码)。
setlocal
set "ROOT=%~dp0.."
if "%HARNESS_PY%"=="" set "HARNESS_PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%HARNESS_PY%" (
  echo [X] 没找到 %HARNESS_PY% —— 先跑一遍 setup.bat
  pause
  exit /b 1
)
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%ROOT%\crawlers\bili"
"%HARNESS_PY%" -u bili_search_server.py --port 8771 >> "%ROOT%\logs\bili.log" 2>&1
