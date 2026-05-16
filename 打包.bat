@echo off
chcp 65001 >nul
echo ================================================================================
echo API切换器 - 一键打包工具
echo ================================================================================
echo.

echo [1/3] 创建图标...
python create_icon.py
if errorlevel 1 (
    echo 创建图标失败，但继续打包...
)
echo.

echo [2/3] 准备打包环境...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install pyinstaller pillow >nul 2>&1
echo.

echo [3/3] 开始打包...
python build_exe.py
if errorlevel 1 (
    echo.
    echo 打包失败，请检查上方错误信息。
    echo.
    pause
    exit /b 1
)

if not exist "dist\API切换器\API切换器.exe" (
    echo.
    echo 打包失败：未找到 dist\API切换器\API切换器.exe
    echo.
    pause
    exit /b 1
)

echo.
echo ================================================================================
echo 打包完成！
echo ================================================================================
echo.
echo EXE 文件位置: dist\API切换器\API切换器.exe
echo.
pause
