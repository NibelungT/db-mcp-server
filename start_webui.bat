@echo off
rem ---------------------------------------------------------------
rem  DB MCP Config Center launcher
rem  Pure ASCII on purpose: a .bat with non-ASCII text breaks when the
rem  console code page differs from the file encoding.
rem ---------------------------------------------------------------
cd /d "%~dp0"

if defined DB_MCP_PYTHON goto custom

py -3.12 -c "import mcp" >nul 2>&1
if %errorlevel%==0 goto use_py
goto try_python

:use_py
py -3.12 webui_server.py
goto end

:try_python
python -c "import mcp" >nul 2>&1
if %errorlevel%==0 goto use_python
goto nopy

:use_python
python webui_server.py
goto end

:nopy
echo.
echo [ERROR] No Python interpreter found with required packages.
echo         Required: mcp, fastapi, uvicorn, mysql-connector-python
echo.
echo Fix: point DB_MCP_PYTHON to a python.exe that has them installed
echo      setx DB_MCP_PYTHON "C:/path/to/python.exe"
echo.
pause
exit /b 1

:custom
"%DB_MCP_PYTHON%" webui_server.py
goto end

:end
echo.
pause
