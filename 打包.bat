@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
echo ================================================================================
echo API切换器 - 一键打包工具
echo ================================================================================
echo.

echo [1/3] 检查构建依赖...
python -X utf8 -c "import PyInstaller, pytest, ruff, release_check; raise SystemExit(0 if release_check.check_runtime_dependencies() else 1)" >nul 2>&1
if errorlevel 1 (
    echo 检测到依赖缺失，正在安装...
    python -X utf8 -m pip install -r requirements.txt pyinstaller pytest ruff
    if errorlevel 1 (
        echo.
        echo 依赖安装失败，请检查网络和 Python 环境。
        popd
        pause
        exit /b 1
    )
)
echo.

echo [2/3] 执行发布检查并打包...
python -X utf8 release_check.py --build
if errorlevel 1 (
    echo.
    echo 打包失败，请检查上方错误信息。
    echo.
    popd
    pause
    exit /b 1
)

echo [3/3] 验证发布产物...
if not exist "dist\API切换器.exe" (
    echo.
    echo 打包失败：未找到 dist\API切换器.exe
    echo.
    popd
    pause
    exit /b 1
)

echo.
echo ================================================================================
echo 打包完成！
echo ================================================================================
echo.
echo EXE 文件位置: dist\API切换器.exe
echo.
popd
pause
