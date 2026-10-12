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

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ------------------------------------------------------------
rem  创作工作室 本地启动
rem    start.bat                启动服务并自动打开浏览器
rem    start.bat --no-browser   只启动服务，不开浏览器
rem    start.bat --check        只检查启动环境
rem  从命令行或自动化脚本调用时可先设 VTW_NO_PAUSE=1 跳过出错时的等待。
rem
rem  解释器查找顺序：backend\.venv → backend\venv → PATH 上的 python。
rem  前两条是 README 推荐的虚拟环境，第三条让「不建虚拟环境、用全局
rem  Python 3.11/3.12 装好依赖」也能直接双击启动。
rem ------------------------------------------------------------

set "VTW_PYTHON="
for %%D in (.venv venv) do if not defined VTW_PYTHON if exist "%~dp0backend\%%D\Scripts\python.exe" set "VTW_PYTHON=%~dp0backend\%%D\Scripts\python.exe"
if not defined VTW_PYTHON for /f "delims=" %%P in ('where python 2^>nul') do if not defined VTW_PYTHON set "VTW_PYTHON=%%P"
if not defined VTW_PYTHON goto :no_python

rem 后端要求 Python 3.11 / 3.12（3.13 起装不上「精准时间轴」引擎）。这里只提示，不拦截。
"%VTW_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) else 1)" >nul 2>nul
if errorlevel 1 echo [警告] 当前解释器不是 Python 3.11 / 3.12，「精准时间轴」可能不可用：%VTW_PYTHON%

rem 依赖检查：缺了就直接给出安装命令，免得只看到一句「启动失败」。
rem 这里挑几个最代表「环境没装全」的导入：框架本身，加上 websockets.sync——uvicorn[standard]
rem 只要求 websockets>=10.4，旧版能装上但导入 app.main 时才会抛 ModuleNotFoundError。
"%VTW_PYTHON%" -c "import uvicorn, fastapi, websockets.sync.client" >nul 2>nul
if errorlevel 1 goto :no_deps

echo 使用解释器：%VTW_PYTHON%
"%VTW_PYTHON%" "%~dp0launcher.py" %*

if errorlevel 1 (
    echo.
    echo 启动失败，请看上面的错误信息。
    if not defined VTW_NO_PAUSE pause
)
endlocal & exit /b %ERRORLEVEL%


:no_python
echo [错误] 未找到 Python 解释器：没有 backend\.venv，PATH 上也没有 python。
echo        两种方式任选其一（详见 README.md 的「开发启动」）：
echo          1) 建虚拟环境：cd backend ^&^& python -m venv .venv ^&^& .\.venv\Scripts\python -m pip install -e ".[dev]"
echo          2) 用全局 Python 3.11/3.12：cd backend ^&^& python -m pip install -e ".[dev]"，之后运行 python launcher.py
goto :fail


:no_deps
echo [错误] 当前解释器缺少后端依赖：%VTW_PYTHON%
echo        先安装依赖，再重新运行本脚本：
echo          cd backend
echo          "%VTW_PYTHON%" -m pip install -e ".[dev]"
goto :fail


:fail
if not defined VTW_NO_PAUSE pause
endlocal & exit /b 1
