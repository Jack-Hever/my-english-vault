@echo off
REM ===================================================================
REM  MyEnglishVault 一键启动（Windows）
REM  双击本文件即可：首次自动安装依赖，随后打开桌面窗口。
REM  已安装过依赖时，会直接启动（安装命令幂等、很快）。
REM ===================================================================
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python。请先安装 Python 3.11+ 并务必勾选 "Add python.exe to PATH"。
    echo        下载地址：https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [1/2] 正在安装/校验依赖（首次运行稍慢，之后很快）...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络连接与 Python 版本（需 3.11+）。
    pause
    exit /b 1
)
echo [2/2] 启动 MyEnglishVault 桌面窗口...
echo        （若启动后黑屏或没反应，日志在 %TEMP%\EnPractice-startup.log）
python run.py
pause
