import os
import secrets

import click
from flask import Flask
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "main.login"


def _load_secret_key(instance_path):
    """Use TRACKER_SECRET_KEY if set; otherwise create a random key on first run and keep it
    in the instance folder."""
    if os.environ.get("TRACKER_SECRET_KEY"):
        return os.environ["TRACKER_SECRET_KEY"]
    path = os.path.join(instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def _add_missing_columns():
    """create_all() never alters existing tables, so add columns introduced since the
    database was first created. Safe to run every start."""
    wanted = [
        ("job", "total_quantity", "INTEGER"),
        ("part", "finish", "VARCHAR(10)"),
        ("job", "complete", "BOOLEAN NOT NULL DEFAULT 0"),
        ("pallet", "custom_name", "VARCHAR(120)"),
        ("job", "completed_on", "DATE"),
        ("job", "wrapped_on", "DATE"),
        ("job", "shipped_on", "DATE"),
        ("part", "completed_on", "DATE"),
        ("pallet", "wrapped_on", "DATE"),
        ("pallet", "shipped_on", "DATE"),
        ("zone", "notes", "TEXT"),
        ("part", "taken_quantity", "INTEGER DEFAULT 0"),
        ("zone", "map_x", "FLOAT"),
        ("zone", "map_y", "FLOAT"),
        ("zone", "map_w", "FLOAT"),
        ("zone", "map_h", "FLOAT"),
        ("audit_event", "part_id", "INTEGER"),
        ("audit_event", "target_user_id", "INTEGER"),
    ]
    with db.engine.begin() as conn:
        for table, column, ddl in wanted:
            existing = [row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")]
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _uppercase_existing(models):
    """Names are kept in capitals. Convert anything entered before that rule existed.
    Two zones or users that differ only by capitals are left as they were."""
    for part in models.Part.query.filter(models.Part.part_number.isnot(None)).all():
        if part.part_number != part.part_number.upper():
            part.part_number = part.part_number.upper()
    for pallet in models.Pallet.query.filter(models.Pallet.custom_name.isnot(None)).all():
        if pallet.custom_name != pallet.custom_name.upper():
            pallet.custom_name = pallet.custom_name.upper()
    for model, attr in ((models.Zone, "name"), (models.User, "username")):
        rows = model.query.all()
        taken = {getattr(row, attr) for row in rows}
        for row in rows:
            value = getattr(row, attr)
            upper = value.upper()
            if upper != value and upper not in taken:
                setattr(row, attr, upper)
                taken.add(upper)
    db.session.commit()


def _number_existing_mixed(models):
    """Mixed pallets made before numbering existed get #1, #2... within each name, oldest first."""
    counts = {}
    changed = False
    for pallet in models.Pallet.query.filter_by(kind="mixed").order_by(models.Pallet.id).all():
        base = pallet.base_name.upper()
        if pallet.number_in_job is None:
            counts[base] = counts.get(base, 0) + 1
            pallet.number_in_job = counts[base]
            changed = True
        else:
            counts[base] = max(counts.get(base, 0), pallet.number_in_job)
    if changed:
        db.session.commit()


def create_app(test_config=None):
    # TRACKER_DATA_DIR moves the database, backups and secret key somewhere else
    data_dir = os.environ.get("TRACKER_DATA_DIR")
    app = Flask(
        __name__,
        instance_path=os.path.abspath(data_dir) if data_dir else None,
        instance_relative_config=True,
    )
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.from_mapping(
        SECRET_KEY=_load_secret_key(app.instance_path),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # spreadsheet uploads up to 10 MB
        SESSION_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SAMESITE="Lax",
        # set TRACKER_COOKIE_SECURE=1 when the site is served over https
        SESSION_COOKIE_SECURE=os.environ.get("TRACKER_COOKIE_SECURE") == "1",
        REMEMBER_COOKIE_SECURE=os.environ.get("TRACKER_COOKIE_SECURE") == "1",
        BACKUP_COPY_DIR=os.environ.get("TRACKER_BACKUP_COPY_DIR") or None,
        BACKUP_KEEP=int(os.environ.get("TRACKER_BACKUP_KEEP", "60")),
        SQLALCHEMY_DATABASE_URI="sqlite:///"
        + os.path.join(app.instance_path, "tracker.db"),
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    login_manager.init_app(app)

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        from sqlalchemy import event

        with app.app_context():

            @event.listens_for(db.engine, "connect")
            def _sqlite_settings(dbapi_connection, _record):
                # WAL lets several people read and write at once; wait up to 5 s if busy
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

    from . import models  # noqa: F401  (registers the tables)
    from .routes import bp

    app.register_blueprint(bp)

    with app.app_context():
        db.create_all()
        _add_missing_columns()
        _uppercase_existing(models)
        _number_existing_mixed(models)

    @app.cli.command("create-user")
    @click.argument("username")
    @click.password_option()
    def create_user(username, password):
        """Add another login:  flask --app tracker create-user NAME"""
        username = username.strip().upper()
        if models.User.query.filter(db.func.lower(models.User.username) == username.lower()).first():
            raise click.ClickException("That username already exists.")
        user = models.User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Created user {username}.")

    return app
