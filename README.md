# Pallet + Parts Tracker

Built by HiFen Kong, Metal Fab, Van 1. The credit and GitHub link are set in `tracker/about.py`.

## Run it (normal use)
Windows: double-click `start-tracker.bat`. The first time it sets itself up (needs Python installed).
Mac/Linux: run `./start-tracker.sh`.

Open http://localhost:5000 on that computer. Phones on the shop wifi use http://<computer-address>:5000.
The first visit asks you to create the first login. Leave the window open while people use the app.

## Run it for testing
    python -m venv venv
    venv\Scripts\activate        (Windows)   or   source venv/bin/activate   (Mac/Linux)
    pip install -r requirements.txt
    python run.py

## Add another login
Use the Users page in the app, or:  flask --app tracker create-user NAME

## Backups, starting on boot, and moving to a cloud host
See HOSTING.md.

## Upgrading from an earlier version
Unzip the new version over the old one and keep your `instance/` folder (it holds the database,
backups, and secret key). New database columns are added automatically when the app starts.
