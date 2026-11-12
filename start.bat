@echo off
title Macro helper
cd /d "%~dp0"
start "" http://127.0.0.1:8765
:run
python -m tools.macro_helper
if errorlevel 3 if not errorlevel 4 (echo Restarting... & goto run)
pause
