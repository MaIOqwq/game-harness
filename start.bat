@echo off
rem ============================================================================
rem 起网页后端 + 开网页。爬虫**不在这里起** —— 网页后端会在真要用到的时候按需把它们拉起来,
rem 闲下来自己关掉(见 harness\supervisor.py)。所以平时只有一个进程, 不会让两个浏览器干坐着。
rem 关掉它们: 跑 stop.bat。
rem ============================================================================
setlocal
chcp 936 >nul 2>nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [X] 还没装过。先双击 setup.bat。
  pause
  exit /b 1
)

rem 8780 被占 = 网页后端已经起过一份; 8770/8771 被占 = 上一轮留下的爬虫没收干净
rem (那种爬虫网页后端认不得、也不会去管它)。两种情况都先 stop.bat 再来。
set "BUSY="
for %%P in (8770 8771 8780) do (
  netstat -ano | findstr /r /c:":%%P .*LISTENING" >nul 2>nul
  if not errorlevel 1 (
    echo [!] 端口 %%P 被占了。
    set "BUSY=1"
  )
)
if defined BUSY (
  echo     8780 = 网页后端已经起过; 8770/8771 = 上一轮的爬虫没收干净。
  echo     都先跑 stop.bat, 再跑这个。
  pause
  exit /b 1
)

if exist "config.bat" (
  call config.bat
) else (
  echo [!] 没有 config.bat —— 照 config.example.bat 复制一份填上 DEEPSEEK_API_KEY, 否则问不出答案。
  echo     现在只起服务, 网页能看到状态, 但问不了题。
)
if "%DEEPSEEK_API_KEY%"=="" echo [!] DEEPSEEK_API_KEY 是空的, 问不出答案。

if not exist "logs" mkdir "logs"

echo 起网页后端 (8780) ...
start "harness-web" /min cmd /c ""%~dp0bin\run-web.bat""
timeout /t 3 /nobreak >nul

echo 开浏览器 ...
start "" http://127.0.0.1:8780/

echo.
echo 起来了。两个爬虫是按需的: 详情窗里那两盏灯平时是灰的「未启动」, 问第一题时才会自动拉起来
echo (那题会多等十几秒 —— 它要拉一次浏览器, 属正常), 问完闲一会儿自己关, 不用你管。
echo 日志在 logs\ 下: web.log / nga.log / bili.log
echo 关服务跑 stop.bat。
timeout /t 8 /nobreak >nul
