@echo off
chcp 936 >nul 2>nul
rem 单独起网页后端(端口 8780)。start.bat 会调它; 排障时也可以单独跑这一个。
rem 它自己不爬东西 —— 爬取是转发给 8770/8771 那两个服务干的。那两个是**按需拉起**的:
rem 没事的时候网页上那两盏灯是灰的「未启动」, 真要用到时网页后端会自动把它们拉起来, 闲了自己关。
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
cd /d "%ROOT%"
"%HARNESS_PY%" -u -m harness.webapp --port 8780 >> "%ROOT%\logs\web.log" 2>&1
