@echo off
chcp 936 >nul 2>nul
rem 单独起 NGA 爬虫服务(端口 8770)。平常**不用**跑这个 —— 网页后端会在真用到它的时候按需拉起。
rem 这个是排障/手工起的时候用的: 单独看它的日志、单独试它的接口。
rem 日志写 logs\nga.log; 带 -u 是必须的, 不然 Python 缓冲着不写, 出了问题看不见。
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
"%HARNESS_PY%" -u "%ROOT%\crawlers\nga\nga_search_server.py" --port 8770 >> "%ROOT%\logs\nga.log" 2>&1
