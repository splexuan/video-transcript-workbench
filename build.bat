@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  文案工作台 一键构建
rem    build.bat            完整构建：更新代码 → 构建前端 → 打包 exe
rem    build.bat frontend   只构建前端（开发时用，产物在 frontend\dist）
rem  产物：frontend\dist\  与  backend\dist\文案工作台\文案工作台.exe
rem ============================================================

set "VTW_PYTHON=%~dp0backend\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem 清空 PYTHONPATH：受限环境注入的 sitecustomize 会干扰 pip 与 PyInstaller
set "PYTHONPATH="

set "FRONTEND_ONLY=0"
if /i "%~1"=="frontend" set "FRONTEND_ONLY=1"

echo ============================================================
echo  文案工作台 一键构建
echo ============================================================
echo  开始时间：%TIME%
echo.

if not exist "%VTW_PYTHON%" (
    echo [错误] 没找到后端虚拟环境：%VTW_PYTHON%
    echo        先按 README「开发启动」创建，再重新运行本脚本。
    goto :fail
)
where npm >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 npm，请先安装 Node.js 并保证它在 PATH 里。
    goto :fail
)

echo [1/5] 更新代码
where git >nul 2>nul
if errorlevel 1 (
    echo    跳过：本机没有 git
) else (
    git pull --ff-only
    if errorlevel 1 echo    跳过：拉取失败（有未提交改动或没有上游），继续用当前代码
)

echo.
echo [2/5] 构建前端
pushd frontend
set "NEED_INSTALL=no"
for /f %%i in ('powershell -NoProfile -Command "if (-not (Test-Path 'node_modules')) { 'yes' } elseif ((Get-Item package.json).LastWriteTime -gt (Get-Item node_modules).LastWriteTime) { 'yes' } else { 'no' }"') do set "NEED_INSTALL=%%i"
if /i "%NEED_INSTALL%"=="yes" (
    echo    安装前端依赖（依赖缺失或 package.json 有更新）...
    call npm install --no-audit --no-fund
    if errorlevel 1 ( popd & goto :fail )
) else (
    echo    依赖没有变化，跳过 npm install
)
call npm run build
if errorlevel 1 ( popd & goto :fail )
popd
if not exist "frontend\dist\index.html" (
    echo [错误] 前端产物缺失：frontend\dist\index.html
    goto :fail
)
echo    前端产物：%CD%\frontend\dist

if "%FRONTEND_ONLY%"=="1" (
    echo.
    echo 前端构建完成（本次只构建前端）。
    goto :done
)

echo.
echo [3/5] 检查打包依赖
rem pystray / PIL 是系统托盘的运行依赖，缺了产物只会「没有托盘入口」，容易漏看
"%VTW_PYTHON%" -c "import PyInstaller, faster_whisper, pystray, PIL" >nul 2>nul
if errorlevel 1 (
    echo    缺少打包、精准识别或托盘依赖，安装 .[dev,accurate] ...
    "%VTW_PYTHON%" -m pip install -e ".[dev,accurate]"
    if errorlevel 1 goto :fail
) else (
    echo    依赖齐备
)

echo.
echo [4/5] 关闭正在运行的旧版本
taskkill /im "文案工作台.exe" /f >nul 2>nul && echo    已关闭旧版本（不关的话产物目录被占用，打包会失败）
rem 等文件句柄释放；用 ping 而不是 timeout，后者在输入被重定向时会直接报错
ping -n 3 127.0.0.1 >nul

echo.
echo [5/5] 打包 Windows 程序（PyInstaller 分析依赖，首次会比较慢）
pushd backend
"%VTW_PYTHON%" -m PyInstaller "文案工作台.spec" --noconfirm
set "BUILD_CODE=%ERRORLEVEL%"
popd
if not "%BUILD_CODE%"=="0" goto :fail
if not exist "backend\dist\文案工作台\文案工作台.exe" (
    echo [错误] 打包结束但没找到产物，请看上面的 PyInstaller 输出。
    goto :fail
)

:done
echo.
echo ============================================================
echo  构建完成
echo ============================================================
echo  前端产物：%CD%\frontend\dist
if "%FRONTEND_ONLY%"=="0" echo  程序产物：%CD%\backend\dist\文案工作台\文案工作台.exe
echo  结束时间：%TIME%
echo.
echo  提示：产物目录整个拷给别人即可运行，首次使用仍需下载识别模型。
rem 从命令行或自动化脚本里调用时可设 VTW_NO_PAUSE=1 跳过等待
if not defined VTW_NO_PAUSE pause
exit /b 0

:fail
echo.
echo 构建失败：请查看上面最后一段错误输出。
if not defined VTW_NO_PAUSE pause
exit /b 1
