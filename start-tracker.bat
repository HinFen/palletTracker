@echo off
cd /d "%~dp0"

rem --- Optional settings (remove "rem" at the start of a line to use it) ---
rem Copy every backup to another folder, such as a USB drive or shared folder:
rem set TRACKER_BACKUP_COPY_DIR=D:\TrackerBackups
rem Use a different port:
rem set TRACKER_PORT=8000

if not exist "venv\Scripts\python.exe" (
  echo Setting up for the first time. This takes a minute...
  python -m venv venv
  "venv\Scripts\pip" install -r requirements.txt
)

"venv\Scripts\python" serve.py
pause
