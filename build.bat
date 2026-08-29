@echo off
chcp 65001 >nul
REM ============================================================
REM  五子棋 · Rapfi 人机对战 —— Windows 一键打包脚本
REM  产物输出到 dist\五子棋\ 目录（exe + _internal 配套，整体分发）
REM ============================================================

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.8+ 并加入 PATH。
    pause
    exit /b 1
)

python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [提示] 未检测到 PyInstaller，正在安装...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [错误] PyInstaller 安装失败，请检查网络或 pip 配置。
        pause
        exit /b 1
    )
)

echo [信息] 开始打包...
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
    --name "五子棋" ^
    --add-data "engine;engine" ^
    gomoku.py

if errorlevel 1 (
    echo [错误] 打包失败，请查看上方日志。
    pause
    exit /b 1
)

echo.
echo [完成] 打包成功，产物位于： dist\五子棋\
echo        分发时请将该文件夹整体拷贝（exe 与 _internal 缺一不可）。
pause
