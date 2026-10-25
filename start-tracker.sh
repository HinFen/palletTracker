#!/bin/sh
cd "$(dirname "$0")"

# Optional settings (remove the # at the start of a line to use it):
# export TRACKER_BACKUP_COPY_DIR=/mnt/usb/TrackerBackups
# export TRACKER_PORT=8000

if [ ! -x venv/bin/python ]; then
  echo "Setting up for the first time. This takes a minute..."
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt
fi

exec venv/bin/python serve.py
