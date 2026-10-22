@echo off
setlocal
cd /d "%~dp0"
"%~dp0.venv-kiro\Scripts\python.exe" "%~dp0scripts\play_candidate.py" --session "%~dp0sessions\ide-02"
if errorlevel 1 pause
