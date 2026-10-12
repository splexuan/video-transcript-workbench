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
cd /d "%~dp0backend"

rem ------------------------------------------------------------
rem  一键准备运行环境：建虚拟环境 -> 应用国内镜像 -> 安装依赖
rem
rem  为什么需要这个脚本：pip 只读「虚拟环境目录下的 pip.ini」
rem  （site 级配置），不读项目目录里的 backend\pip.ini。那份镜像
rem  配置必须复制进 venv 才生效，否则 pip install 走的是
rem  PyPI 官方源。
rem
rem  可重复执行：虚拟环境已存在就跳过创建，只补齐镜像与依赖。
rem ------------------------------------------------------------

where python >nul 2>nul
if errorlevel 1 goto :no_python

if not exist ".venv\Scripts\python.exe" (
    echo 正在创建虚拟环境 backend\.venv ...
    python -m venv .venv
    if errorlevel 1 goto :fail
) else (
    echo 虚拟环境已存在，跳过创建。
)

rem 镜像配置进 venv——pip 的 site 级配置只认这个位置
copy /y "pip.ini" ".venv\pip.ini" >nul
if errorlevel 1 echo [警告] 未能写入 .venv\pip.ini，本次安装可能走官方源。

set "PY=.venv\Scripts\python.exe"

echo.
echo 正在安装基础依赖（走国内镜像，首次约 1~2 分钟）...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -e ".[dev]"
if errorlevel 1 goto :fail

echo.
echo [完成] 基础环境就绪，现在可以双击 start.bat 启动。
echo.
echo 可选能力（按需再装，不装不影响启动，启动时会提示缺哪一项）：
echo   精准时间轴识别       "%PY%" -m pip install -e ".[accurate]"
echo   WorkBuddy 本机登录   "%PY%" -m pip install -e ".[ai]"
goto :done


:no_python
echo [错误] 未找到 python，请先安装 Python 3.11 / 3.12 并加入 PATH。
goto :fail


:fail
echo.
echo [错误] 环境准备失败，请看上面的输出。
if not defined VTW_NO_PAUSE pause
endlocal & exit /b 1


:done
if not defined VTW_NO_PAUSE pause
endlocal & exit /b 0
