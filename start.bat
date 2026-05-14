@echo off
title 聊天记录提取工具
cd /d "%~dp0"

echo.
echo ============================================
echo   聊天记录提取工具
echo   WeChat ^& QQ Chat Extractor
echo ============================================
echo.

:: Find Python - try explicit path first
set PYTHON=

:: Check known Python paths
if exist "C:\Users\28205\AppData\Local\Python\bin\python.exe" (
    set PYTHON=C:\Users\28205\AppData\Local\Python\bin\python.exe
    echo [OK] 使用: %PYTHON%
    goto :found
)

:: Try PATH
for %%p in (python3 python py) do (
    %%p --version >nul 2>&1
    if not errorlevel 1 (
        set PYTHON=%%p
        echo [OK] 检测到: %%p
        goto :found
    )
)

echo [ERROR] 未找到 Python！
echo.
echo 请安装 Python 3.10+ : https://www.python.org/downloads/
echo 安装时务必勾选 "Add Python to PATH"
echo.
echo 安装完成后重新双击 start.bat
pause
exit /b 1

:found
echo.
%PYTHON% --version

:: Install/update dependencies
echo.
echo [信息] 安装依赖 (首次可能需要几分钟) ...
%PYTHON% -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo [WARNING] 部分依赖安装失败，尝试继续...
)

:: Start server
echo.
echo ============================================
echo   服务启动中...
echo.
echo   请在浏览器打开: http://localhost:5000
echo   手机同 WiFi 也可访问此地址
echo.
echo   按 Ctrl+C 停止服务
echo ============================================
echo.

%PYTHON% server.py %*

echo.
echo [信息] 服务已停止
pause
