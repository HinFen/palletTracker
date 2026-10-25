"""Database backups. Pure Python (no Flask), so it is easy to test.

A backup is a consistent copy of the SQLite database made with SQLite's own backup
function, so it is safe to take while the app is running."""
import os
import re
import shutil
import sqlite3
from datetime import datetime

NAME = re.compile(r"^tracker-(\d{4}-\d{2}-\d{2})-(\d{6})\.db$")


def is_backup_name(name):
    return bool(NAME.match(name or ""))


def make_backup(db_path, folder, copy_dir=None, keep=60):
    """Copy the database into `folder`. If `copy_dir` is given, also copy the file there.
    Returns (backup path, error text or None). The error only covers the extra copy."""
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    dest = os.path.join(folder, f"tracker-{stamp}.db")
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    copy_error = None
    if copy_dir:
        try:
            os.makedirs(copy_dir, exist_ok=True)
            shutil.copy2(dest, os.path.join(copy_dir, os.path.basename(dest)))
        except OSError as error:
            copy_error = str(error)
    prune(folder, keep)
    return dest, copy_error


def list_backups(folder):
    """Newest first: [(file name, when, size in bytes)]."""
    if not os.path.isdir(folder):
        return []
    found = []
    for name in os.listdir(folder):
        match = NAME.match(name)
        if match:
            when = datetime.strptime(match.group(1) + match.group(2), "%Y-%m-%d%H%M%S")
            found.append((name, when, os.path.getsize(os.path.join(folder, name))))
    return sorted(found, key=lambda item: item[0], reverse=True)


def has_backup_on(folder, day):
    return any(when.date() == day for _name, when, _size in list_backups(folder))


def prune(folder, keep):
    """Keep only the newest `keep` backups."""
    for name, _when, _size in list_backups(folder)[max(keep, 1):]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
