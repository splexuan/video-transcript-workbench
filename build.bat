@echo off
rem ============================================================
rem  Console-codepage bootstrap. Keep this block ASCII-only, and
rem  keep this file CRLF: cmd.exe keeps re-reading a batch file it
rem  is running, so a chcp done while it is still reading garbles
rem  the remaining lines. Re-run this script in a fresh cmd
rem  process that already uses the UTF-8 codepage.
rem ============================================================
if defined VTW_UTF8 goto vtw_body
setlocal
chcp 65001 >nul
set "VTW_UTF8=1"
cmd /c ""%~f0" %*"
endlocal & exit /b %ERRORLEVEL%

:vtw_body
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  文案工作台 一键构建
rem    build.bat            构建前端和后端，生成完整应用与发行包
rem    build.bat frontend   只构建前端（开发时使用，产物在 frontend\dist）
rem    build.bat package    只打包发行包：用当前产物，不重新构建
rem  产物：frontend\dist\
rem        backend\dist\文案工作台\文案工作台.exe
rem        release\video-transcript-workbench-v<版本>-win64.zip
rem ============================================================
rem ------------------------------------------------------------
rem  本文件必须保持：UTF-8 编码 + CRLF 换行。
rem  开头的 ASCII 引导段不能删、也不能与下面的逻辑合并：cmd.exe 在读取
rem  批处理文件的过程中执行 chcp 会让读取位置错位（中文行被截断、行尾被
rem  当成命令执行，表现为脚本闪退），所以必须先切到 UTF-8 代码页，再用
rem  一个全新的 cmd 进程重新执行本脚本。
rem ------------------------------------------------------------

set "VTW_PYTHON=%~dp0backend\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem 清空 PYTHONPATH，避免环境变量注入 sitecustomize 干扰 pip 和 PyInstaller
set "PYTHONPATH="

set "FRONTEND_ONLY=0"
set "PACKAGE_ONLY=0"
if /i "%~1"=="frontend" set "FRONTEND_ONLY=1"
if /i "%~1"=="package" set "PACKAGE_ONLY=1"

echo ============================================================
echo  文案工作台 一键构建
echo ============================================================
echo  开始时间：%TIME%
echo.

if not exist "%VTW_PYTHON%" (
    echo [错误] 未找到虚拟环境：%VTW_PYTHON%
    echo        请先按 README.md 安装开发依赖，再运行此构建脚本。
    goto :fail
)

rem 版本号只从 backend/app/config.py 取（由打包脚本读出），交给发行包文件名与结束提示用。
rem 外层的 `"..."` 是必须的：命令首token是带引号的路径时，cmd 需要再多一层引号才不当成文件名。
for /f "usebackq delims=" %%v in (`""%VTW_PYTHON%" "%~dp0backend\build_release.py" --print-version"`) do set "VTW_VERSION=%%v"
if defined VTW_VERSION echo  版本：v%VTW_VERSION%

if "%PACKAGE_ONLY%"=="1" (
    echo.
    echo 只打包发行包：跳过代码更新、前端构建与 PyInstaller。
    goto :package
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 npm，请先安装 Node.js 并确认它已加入 PATH。
    goto :fail
)

echo [1/6] 更新代码
where git >nul 2>nul
if errorlevel 1 (
    echo    未找到 git，跳过代码更新。
) else (
    git pull --ff-only
    if errorlevel 1 echo    更新失败；未提交的改动不会丢失，将继续使用当前代码。
)

echo.
echo [2/6] 构建前端
pushd frontend
set "NEED_INSTALL=no"
for /f %%i in ('powershell -NoProfile -Command "if (-not (Test-Path 'node_modules')) { 'yes' } elseif ((Get-Item package.json).LastWriteTime -gt (Get-Item node_modules).LastWriteTime) { 'yes' } else { 'no' }"') do set "NEED_INSTALL=%%i"
if /i "%NEED_INSTALL%"=="yes" (
    echo    前端依赖缺失或 package.json 已更新，正在安装...
    call npm install --no-audit --no-fund
    if errorlevel 1 ( popd & goto :fail )
) else (
    echo    依赖没有变化，跳过 npm install。
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
    echo 前端构建完成。按要求只构建了前端。
    goto :done
)

echo.
echo [3/6] 检查后端依赖
rem pystray / PIL 用于系统托盘；缺少时仅会没有托盘入口，不影响程序运行。
"%VTW_PYTHON%" -c "import PyInstaller, faster_whisper, pystray, PIL" >nul 2>nul
if errorlevel 1 (
    echo    缺少打包或精准识别依赖，正在安装 .[dev,accurate] ...
    "%VTW_PYTHON%" -m pip install -e ".[dev,accurate]"
    if errorlevel 1 goto :fail
) else (
    echo    后端依赖已齐备。
)

echo.
echo [4/6] 关闭正在运行的旧版本
taskkill /im "文案工作台.exe" /f >nul 2>nul && echo    已关闭旧版本；若文件仍被占用，后续构建可能失败。
rem 等待文件句柄释放；用 ping 代替 timeout，避免输出被重定向时直接退出。
ping -n 3 127.0.0.1 >nul

echo.
echo [5/6] 构建 Windows 应用
rem PyInstaller 首次运行可能稍慢。
pushd backend
"%VTW_PYTHON%" -m PyInstaller "文案工作台.spec" --noconfirm
set "BUILD_CODE=%ERRORLEVEL%"
popd
if not "%BUILD_CODE%"=="0" goto :fail
if not exist "backend\dist\文案工作台\文案工作台.exe" (
    echo [错误] 未找到构建产物，请查看上方 PyInstaller 输出。
    goto :fail
)

:package
echo.
echo [6/6] 打包发行包
rem 名称里的 -win64.zip 是工作台「检查更新」的识别依据，所以交给脚本打包而不是手动压缩。
pushd backend
"%VTW_PYTHON%" build_release.py
set "PACKAGE_CODE=%ERRORLEVEL%"
popd
if not "%PACKAGE_CODE%"=="0" goto :fail
if defined VTW_VERSION (
    if not exist "release\video-transcript-workbench-v%VTW_VERSION%-win64.zip" (
        echo [错误] 未找到发行包，请查看上方打包脚本输出。
        goto :fail
    )
)

:done
echo.
echo ============================================================
echo  构建完成
echo ============================================================
echo  前端产物：%CD%\frontend\dist
if "%FRONTEND_ONLY%"=="0" echo  应用产物：%CD%\backend\dist\文案工作台\文案工作台.exe
if "%FRONTEND_ONLY%"=="0" echo  发行包：%CD%\release\video-transcript-workbench-v%VTW_VERSION%-win64.zip
echo  结束时间：%TIME%
echo.
echo  提示：发行包直接上传到 Releases 即可分发；首次使用时程序会下载识别模型。
rem 双击运行时自动暂停；自动化脚本可设置 VTW_NO_PAUSE=1 跳过等待。
if not defined VTW_NO_PAUSE pause
exit /b 0

:fail
echo.
echo 构建失败，请查看上方最后一条错误信息。
if not defined VTW_NO_PAUSE pause
exit /b 1
