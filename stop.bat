@echo off
rem ============================================================================
rem 关掉网页后端 + 它按需拉起来的那些爬虫。
rem 按端口找进程杀, 连它拉起来的那个浏览器一起带走
rem (不带走的话会留下没主的 Chromium 进程, 白占内存)。
rem ============================================================================
setlocal
chcp 936 >nul 2>nul

set "FOUND="
rem 先关 8780(网页后端): 它退出时会把按需拉起来的爬虫带走, 这是最干净的顺序;
rem 再扫 8770/8771 兜底 —— 万一网页后端是被硬杀的、没来得及收尾, 那上面还留着爬虫。
for %%P in (8780 8770 8771) do (
  for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%%P .*LISTENING"') do (
    echo 关 %%P ^(PID %%A^) ...
    taskkill /PID %%A /T /F >nul 2>nul
    set "FOUND=1"
  )
)

if not defined FOUND (
  echo 端口上都没进程在听 —— 本来就没起。
) else (
  echo 关完了。
)
timeout /t 3 /nobreak >nul
