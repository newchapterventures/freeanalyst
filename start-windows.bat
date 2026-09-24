@echo off
rem FreeAnalyst 启动器（Windows）
rem
rem 给不懂命令行的同事用：**双击这个文件**就行。
rem
rem 三条刻意的设计（和 macOS 那份一致）：
rem   1. 不替安装任何东西 —— 缺 Python 就说明缺，给官方下载页
rem   2. 出错不闪退 —— 用 pause 停住，让人看得见错误
rem   3. 关掉这个窗口 = 停止服务
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

echo ----------------------------------------------
echo  FreeAnalyst . 本地估值向导
echo  目录：%CD%
echo ----------------------------------------------
echo.

set "PY="
for %%C in (py python python3) do (
  if not defined PY (
    %%C -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=%%C"
  )
)

if not defined PY (
  echo 没有找到 Python 3.10 或更新的版本 —— 这个工具需要它。
  echo.
  echo 怎么办：
  echo   1^) 打开 https://www.python.org/downloads/ 下载 Windows 安装包
  echo   2^) 双击安装（**务必勾选 Add Python to PATH**）
  echo   3^) 再双击本文件
  echo.
  pause
  exit /b 1
)

echo 正在启动……浏览器会自动打开 http://127.0.0.1:8765/
echo **关掉这个窗口就是停止服务。**
echo.
%PY% freeanalyst.py ui
set code=%errorlevel%

echo.
if not "%code%"=="0" (
  echo 启动失败（退出码 %code%）。把上面这段信息发给做这个工具的人。
) else (
  echo 已停止。
)
pause >nul
exit /b %code%
