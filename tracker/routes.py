import csv
import io
import os
import re
from datetime import date, datetime

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from . import about as about_info
from . import backups, db, importer, network
from .audit import log
from .models import (
    DUE_SOON_DAYS,
    PALLET_RANK,
    PALLET_STATUSES,
    AuditEvent,
    MapObject,
    STAGES,
    Job,
    Part,
    Pallet,
    PalletItem,
    User,
    Zone,
    job_label,
)

bp = Blueprint("main", __name__)


@bp.app_context_processor
def inject_credit():
    """Makes the author credit available to every page."""
    return {
        "author_name": about_info.AUTHOR_NAME,
        "workplace": about_info.WORKPLACE,
        "github_url": about_info.GITHUB_URL,
    }


@bp.route("/about")
def about():
    return render_template("about.html")


@bp.before_app_request
def require_first_account():
    """Until the first account exists, send everyone to the setup page."""
    if request.endpoint in (None, "static", "main.setup", "main.health", "main.about"):
        return None
    if User.query.first() is None:
        return redirect(url_for("main.setup"))
    return None


_backup_checked = {"day": None}


@bp.before_app_request
def daily_backup():
    """The first visit each day makes a backup if there isn't one for today yet."""
    if request.endpoint in (None, "static", "main.health"):
        return None
    today = date.today()
    if _backup_checked["day"] == today:
        return None
    _backup_checked["day"] = today
    try:
        folder = _backup_dir()
        if not backups.has_backup_on(folder, today):
            backups.make_backup(
                db.engine.url.database,
                folder,
                current_app.config.get("BACKUP_COPY_DIR"),
                current_app.config["BACKUP_KEEP"],
            )
    except Exception:
        current_app.logger.exception("The daily backup failed")
    return None


@bp.route("/health")
def health():
    """For hosting monitors: answers without needing a login."""
    return "ok"


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.first() is not None:
        return redirect(url_for("main.login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip().upper()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not username or len(password) < 6:
            flash("Enter a username and a password of at least 6 characters.")
        elif password != confirm:
            flash("The two passwords don't match.")
        else:
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for("main.home"))
    return render_template("setup.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.home"))
    if request.method == "POST":
        user = User.query.filter_by(username=request.form.get("username", "").strip().upper()).first()
        if user and user.check_password(request.form.get("password", "")):
            login_user(user, remember=True)
            return redirect(url_for("main.home"))
        flash("Wrong username or password.")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    logout_user()
    return redirect(url_for("main.login"))


def _number_or_none(value):
    return value if isinstance(value, int) else None


JOB_SORTS = {
    "job": lambda j: _natural(j.display_name),
    "part": lambda j: j.part_display.lower() or None,
    "qty": lambda j: _number_or_none(j.quantity_display),
    "pallets": lambda j: j.pallet_count,
    "status": lambda j: j.status_display.lower(),
    "due": lambda j: j.due_date,
}
SHIPPED_SORTS = {
    "job": lambda j: _natural(j.display_name),
    "part": lambda j: j.part_display.lower() or None,
    "qty": lambda j: _number_or_none(j.quantity_display),
    "pallets": lambda j: j.pallet_count,
    "complete": lambda j: j.completed_on,
    "wrapped": lambda j: j.wrapped_on,
    "shipped": lambda j: j.shipped_on,
}
PALLET_SORTS = {
    "pallet": lambda p: _natural(p.name),
    "zone": lambda p: p.zone.name.lower() if p.zone else None,
    "status": lambda p: PALLET_RANK[p.status],
    "qty": lambda p: sum(i.quantity for i in p.items),
}


@bp.route("/")
@login_required
def home():
    jobs = (
        Job.query.filter_by(shipped=False)
        .order_by(Job.due_date.is_(None), Job.due_date, Job.number)
        .all()
    )
    overdue = sum(1 for j in jobs if j.due_state == "overdue")
    soon = sum(1 for j in jobs if j.due_state == "soon")
    due = request.args.get("due", "")
    if due == "overdue":
        jobs = [j for j in jobs if j.due_state == "overdue"]
    elif due == "attention":
        jobs = [j for j in jobs if j.due_state]
    else:
        due = ""
    jobs, sort, direction = _sorted_items(jobs, JOB_SORTS)
    return render_template(
        "home.html",
        jobs=jobs,
        sort=sort,
        direction=direction,
        due=due,
        overdue=overdue,
        soon=soon,
        soon_days=DUE_SOON_DAYS,
    )


# ---------------------------------------------------------------- helpers

def _positive_int(value):
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return n if n > 0 else None


def _date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


def _clean(value):
    return (value or "").strip()


def _natural(text):
    """Sort key so that CDP5000 comes before CDP10000 and 2 before 10."""
    return [int(piece) if piece.isdigit() else piece for piece in re.split(r"(\d+)", text.lower())]


def _sorted_items(items, keyfuncs):
    """Sort by the column named in ?sort= (and ?dir=desc). Blank values always go last.
    Returns (items, sort key in use, direction)."""
    key = request.args.get("sort", "")
    direction = "desc" if request.args.get("dir") == "desc" else "asc"
    if key not in keyfuncs:
        return items, "", "asc"
    keyfunc = keyfuncs[key]
    present = [i for i in items if keyfunc(i) is not None]
    missing = [i for i in items if keyfunc(i) is None]
    present.sort(key=keyfunc, reverse=(direction == "desc"))
    return present + missing, key, direction


def _pn(part):
    return part.part_number or "(no part number)"


def _pallet_ref(pallet):
    return pallet.name


# ---------------------------------------------------------------- jobs

@bp.route("/jobs/new", methods=["GET", "POST"])
@login_required
def job_new():
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        number = _clean(form.get("number"))
        total_raw = _clean(form.get("total_quantity"))
        total = _positive_int(total_raw) if total_raw else None
        if not number:
            flash("Enter the job.")
        elif total_raw and total is None:
            flash("Total quantity must be a whole number above 0.")
        else:
            job = Job(
                number=number,
                line=_clean(form.get("line")) or None,
                project=_clean(form.get("project")) or None,
                assigned_date=_date(form.get("assigned_date")),
                due_date=_date(form.get("due_date")),
                total_quantity=total,
            )
            db.session.add(job)
            db.session.flush()
            log(f"Added job {job.display_name}", job_id=job.id)
            db.session.commit()
            return redirect(url_for("main.job_detail", job_id=job.id))
    return render_template("job_new.html", form=form)


@bp.route("/jobs/<int:job_id>")
@login_required
def job_detail(job_id):
    job = db.get_or_404(Job, job_id)
    return render_template(
        "job_detail.html", job=job, stages=STAGES, zones=Zone.query.order_by(Zone.name).all()
    )


@bp.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@login_required
def job_edit(job_id):
    job = db.get_or_404(Job, job_id)
    if request.method == "POST":
        form = request.form
        number = _clean(form.get("number"))
        total_raw = _clean(form.get("total_quantity"))
        total = _positive_int(total_raw) if total_raw else None
        if not number:
            flash("Enter the job.")
        elif total_raw and total is None:
            flash("Total quantity must be a whole number above 0.")
        else:
            old_name = job.display_name
            job.number = number
            job.line = _clean(form.get("line")) or None
            job.project = _clean(form.get("project")) or None
            job.assigned_date = _date(form.get("assigned_date"))
            job.due_date = _date(form.get("due_date"))
            job.total_quantity = total
            job.refresh_status()
            was = f" (was {old_name})" if old_name != job.display_name else ""
            log(f"Edited job {job.display_name}{was}", job_id=job.id)
            db.session.commit()
            return redirect(url_for("main.job_detail", job_id=job.id))
    else:
        form = {
            "number": job.number,
            "line": job.line or "",
            "project": job.project or "",
            "assigned_date": job.assigned_date.isoformat() if job.assigned_date else "",
            "due_date": job.due_date.isoformat() if job.due_date else "",
            "total_quantity": job.total_quantity or "",
        }
    return render_template("job_new.html", form=form, editing=job)


@bp.route("/jobs/<int:job_id>/delete", methods=["GET", "POST"])
@login_required
def job_delete(job_id):
    job = db.get_or_404(Job, job_id)
    if request.method == "POST":
        name = job.display_name
        log(
            f"Deleted job {name} ({len(job.parts)} part(s), {len(job.pallets)} pallet(s) of its own)",
            job_id=job.id,
        )
        for item in job.pallet_items:  # take its parts off every pallet
            db.session.delete(item)
        for pallet in list(job.pallets):  # its own single-job pallets go with it
            db.session.delete(pallet)
        db.session.delete(job)  # its parts are deleted with it
        db.session.commit()
        flash(f"Deleted job {name}.")
        return redirect(url_for("main.home"))
    mixed = [p for p in job.all_pallets if p.kind == "mixed"]
    return render_template("job_delete.html", job=job, mixed=mixed)


@bp.route("/jobs/<int:job_id>/parts", methods=["POST"])
@login_required
def job_add_part(job_id):
    job = db.get_or_404(Job, job_id)
    quantity = _positive_int(request.form.get("quantity"))
    stage = request.form.get("stage")
    if quantity is None or stage not in STAGES:
        flash("Enter a quantity above 0.")
    else:
        part = Part(
            job=job,
            part_number=_clean(request.form.get("part_number")).upper() or None,
            quantity=quantity,
            stage=stage,
        )
        db.session.add(part)
        part.set_stage(stage)
        job.refresh_status()
        db.session.flush()
        log(f"Added part {_pn(part)} (qty {quantity}, {stage}) to job {job.display_name}", job_id=job.id, part_id=part.id)
        db.session.commit()
    return redirect(url_for("main.job_detail", job_id=job.id))


@bp.route("/parts/<int:part_id>/stage", methods=["POST"])
@login_required
def part_stage(part_id):
    part = db.get_or_404(Part, part_id)
    stage = request.form.get("stage")
    if stage in STAGES:
        old = part.stage
        part.set_stage(stage)
        part.job.refresh_status()
        if old != stage:
            log(
                f"Changed stage of {_pn(part)} in job {part.job.display_name}: {old} -> {stage}",
                job_id=part.job_id,
                part_id=part.id,
            )
        db.session.commit()
    return redirect(url_for("main.job_detail", job_id=part.job_id))


# ---------------------------------------------------------------- add part (can create the job too)

def _matching_jobs(number):
    return (
        Job.query.filter(db.func.lower(Job.number) == number.lower())
        .order_by(Job.created_at.desc())
        .all()
    )


def _render_part_step2(number, line, form):
    matches = _matching_jobs(number)
    choice = form.get("choice")
    if not choice:
        exact = [m for m in matches if (m.line or "").lower() == line.lower()]
        choice = str(exact[0].id) if exact else "new"
    return render_template(
        "part_new.html",
        step=2,
        number=number,
        line=line,
        matches=matches,
        choice=choice,
        new_name=job_label(number, line or None),
        stages=STAGES,
        form=form,
    )


@bp.route("/parts/new", methods=["GET", "POST"])
@login_required
def part_new():
    if request.method == "GET":
        number = _clean(request.args.get("job"))
        line = _clean(request.args.get("line"))
        if not number:
            return render_template("part_new.html", step=1, form={})
        return _render_part_step2(number, line, {})

    form = request.form
    number = _clean(form.get("number"))
    line = _clean(form.get("line"))
    choice = form.get("choice", "")
    quantity = _positive_int(form.get("quantity"))
    stage = form.get("stage")
    if not number or not choice or quantity is None or stage not in STAGES:
        flash("Pick a job and enter a quantity above 0.")
        return _render_part_step2(number, line, form)

    if choice == "new":
        job = Job(
            number=number,
            line=line or None,
            project=_clean(form.get("project")) or None,
            due_date=_date(form.get("due_date")),
        )
        db.session.add(job)
    else:
        try:
            job = db.session.get(Job, int(choice))
        except ValueError:
            job = None
        if job is None:
            flash("That job wasn't found.")
            return _render_part_step2(number, line, form)

    part = Part(
        job=job,
        part_number=_clean(form.get("part_number")).upper() or None,
        quantity=quantity,
        stage=stage,
    )
    db.session.add(part)
    part.set_stage(stage)
    job.refresh_status()
    db.session.flush()
    if choice == "new":
        log(f"Added job {job.display_name}", job_id=job.id)
    log(f"Added part {_pn(part)} (qty {quantity}, {stage}) to job {job.display_name}", job_id=job.id, part_id=part.id)
    db.session.commit()
    return redirect(url_for("main.job_detail", job_id=job.id))


# ---------------------------------------------------------------- shipped jobs

@bp.route("/shipped")
@login_required
def shipped_jobs():
    jobs = (
        Job.query.filter_by(shipped=True)
        .order_by(Job.shipped_on.desc(), Job.id.desc())
        .all()
    )
    jobs, sort, direction = _sorted_items(jobs, SHIPPED_SORTS)
    return render_template("shipped.html", jobs=jobs, sort=sort, direction=direction)


# ---------------------------------------------------------------- zones

def _zone_id(value):
    try:
        zid = int(value)
    except (TypeError, ValueError):
        return None
    return zid if db.session.get(Zone, zid) else None


@bp.route("/zones", methods=["GET", "POST"])
@login_required
def zones():
    if request.method == "POST":
        name = _clean(request.form.get("name")).upper()
        if not name:
            flash("Enter a zone name.")
        elif Zone.query.filter(db.func.lower(Zone.name) == name.lower()).first():
            flash("There is already a zone with that name.")
        else:
            db.session.add(Zone(name=name, notes=_clean(request.form.get("notes")) or None))
            log(f"Added zone {name}")
            db.session.commit()
        return redirect(url_for("main.zones"))
    all_zones = Zone.query.order_by(Zone.name).all()
    unplaced = (
        Pallet.query.filter(Pallet.zone_id.is_(None), Pallet.status != "shipped")
        .order_by(Pallet.id)
        .all()
    )
    return render_template("zones.html", zones=all_zones, unplaced=unplaced)


@bp.route("/zones/<int:zone_id>/edit", methods=["GET", "POST"])
@login_required
def zone_edit(zone_id):
    zone = db.get_or_404(Zone, zone_id)
    form = request.form if request.method == "POST" else {"name": zone.name, "notes": zone.notes or ""}
    if request.method == "POST":
        name = _clean(form.get("name")).upper()
        clash = Zone.query.filter(db.func.lower(Zone.name) == name.lower(), Zone.id != zone.id).first()
        if not name:
            flash("Enter a zone name.")
        elif clash:
            flash("There is already a zone with that name.")
        else:
            old_name = zone.name
            zone.name = name
            zone.notes = _clean(form.get("notes")) or None
            was = f" (was {old_name})" if old_name != name else ""
            log(f"Edited zone {name}{was}")
            db.session.commit()
            return redirect(url_for("main.zones"))
    return render_template("zone_edit.html", zone=zone, form=form)


# ---------------------------------------------------------------- floor map

MAP_WIDTH, MAP_HEIGHT = 1000, 600
# What can be drawn on the map besides zones, with the size each starts at (width, height)
OBJECT_KINDS = {"wall": (200, 10), "rack": (120, 30), "machine": (80, 60)}
MIN_SIZE = {"zone": (40, 30), "object": (10, 10)}


@bp.route("/zones/<int:zone_id>")
@login_required
def zone_detail(zone_id):
    zone = db.get_or_404(Zone, zone_id)
    pallets = sorted(
        (p for p in zone.pallets if p.status != "shipped"), key=lambda p: (_natural(p.name), p.id)
    )
    return render_template("zone_detail.html", zone=zone, pallets=pallets)


@bp.route("/map")
@login_required
def floor_map():
    zones = Zone.query.order_by(Zone.name).all()
    counts = {z.id: sum(1 for p in z.pallets if p.status != "shipped") for z in zones}
    return render_template(
        "map.html",
        objects=MapObject.query.order_by(MapObject.id).all(),
        placed=[z for z in zones if z.map_x is not None],
        unplaced=[z for z in zones if z.map_x is None],
        counts=counts,
        no_zone=Pallet.query.filter(Pallet.zone_id.is_(None), Pallet.status != "shipped").count(),
        editing=request.args.get("edit") == "1",
        highlight=request.args.get("highlight", type=int),
        width=MAP_WIDTH,
        height=MAP_HEIGHT,
    )


@bp.route("/map/save", methods=["POST"])
@login_required
def map_save():
    """Called by the map page when a zone or an object (wall, rack, machine) is moved or resized."""
    data = request.get_json(silent=True) or {}
    kind = "object" if data.get("type") == "object" else "zone"
    min_w, min_h = MIN_SIZE[kind]
    try:
        item = db.session.get(MapObject if kind == "object" else Zone, int(data.get("id")))
        w = min(max(float(data.get("w")), min_w), MAP_WIDTH)
        h = min(max(float(data.get("h")), min_h), MAP_HEIGHT)
        x = min(max(float(data.get("x")), 0), MAP_WIDTH - w)
        y = min(max(float(data.get("y")), 0), MAP_HEIGHT - h)
    except (TypeError, ValueError):
        return jsonify(ok=False), 400
    if item is None:
        return jsonify(ok=False), 404
    if kind == "object":
        item.x, item.y, item.w, item.h = x, y, w, h
    else:
        item.map_x, item.map_y, item.map_w, item.map_h = x, y, w, h
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/map/objects/add", methods=["POST"])
@login_required
def map_object_add():
    kind = request.form.get("kind")
    if kind not in OBJECT_KINDS:
        flash("Pick what to add: a wall, a rack, or a machine.")
        return redirect(url_for("main.floor_map", edit=1))
    width, height = OBJECT_KINDS[kind]
    step = MapObject.query.count() % 10
    db.session.add(
        MapObject(
            kind=kind,
            label=_clean(request.form.get("label")) or None,
            x=40 + 30 * step,
            y=40 + 30 * step,
            w=width,
            h=height,
        )
    )
    db.session.commit()
    return redirect(url_for("main.floor_map", edit=1))


@bp.route("/map/objects/<int:object_id>/rotate", methods=["POST"])
@login_required
def map_object_rotate(object_id):
    """Turn it a quarter turn (swap width and height), keeping it on the map."""
    item = db.get_or_404(MapObject, object_id)
    item.w, item.h = item.h, item.w
    item.x = min(item.x, MAP_WIDTH - item.w)
    item.y = min(item.y, MAP_HEIGHT - item.h)
    db.session.commit()
    return redirect(url_for("main.floor_map", edit=1))


@bp.route("/map/objects/<int:object_id>/label", methods=["POST"])
@login_required
def map_object_label(object_id):
    item = db.get_or_404(MapObject, object_id)
    item.label = _clean(request.form.get("label")) or None
    db.session.commit()
    return redirect(url_for("main.floor_map", edit=1))


@bp.route("/map/objects/<int:object_id>/delete", methods=["POST"])
@login_required
def map_object_delete(object_id):
    db.session.delete(db.get_or_404(MapObject, object_id))
    db.session.commit()
    return redirect(url_for("main.floor_map", edit=1))


@bp.route("/map/place/<int:zone_id>", methods=["POST"])
@login_required
def map_place(zone_id):
    """Put a zone on the map in a free-ish default spot; the user then drags it where it belongs."""
    zone = db.get_or_404(Zone, zone_id)
    placed = Zone.query.filter(Zone.map_x.isnot(None)).count()
    step = placed % 10
    zone.map_x, zone.map_y, zone.map_w, zone.map_h = 20 + 30 * step, 20 + 30 * step, 160, 100
    db.session.commit()
    return redirect(url_for("main.floor_map", edit=1))


@bp.route("/map/remove/<int:zone_id>", methods=["POST"])
@login_required
def map_remove(zone_id):
    zone = db.get_or_404(Zone, zone_id)
    zone.map_x = zone.map_y = zone.map_w = zone.map_h = None
    db.session.commit()
    return redirect(url_for("main.floor_map", edit=1))


# ---------------------------------------------------------------- pallets

def _refresh_jobs_for_pallet(pallet):
    jobs = {}
    if pallet.job:
        jobs[pallet.job.id] = pallet.job
    for item in pallet.items:
        jobs[item.part.job_id] = item.part.job
    newly_shipped = []
    for job in jobs.values():
        was_shipped = job.shipped
        job.refresh_status()
        if job.shipped and not was_shipped:
            newly_shipped.append(job)
    return newly_shipped


@bp.route("/pallets")
@login_required
def pallets():
    items = Pallet.query.filter(Pallet.status != "shipped").order_by(Pallet.id.desc()).all()
    items, sort, direction = _sorted_items(items, PALLET_SORTS)
    return render_template("pallets.html", pallets=items, sort=sort, direction=direction)


@bp.route("/pallets/new-mixed", methods=["GET", "POST"])
@login_required
def pallet_new_mixed():
    if request.method == "POST":
        pallet = Pallet(
            kind="mixed",
            custom_name=_clean(request.form.get("name")).upper() or None,
            assigned_date=_date(request.form.get("assigned_date")),
            project=_clean(request.form.get("project")) or None,
            zone_id=_zone_id(request.form.get("zone_id")),
        )
        pallet.number_in_job = _next_pallet_number(pallet)
        db.session.add(pallet)
        db.session.flush()
        log(f"Created mixed pallet {_pallet_ref(pallet)}", pallet_id=pallet.id)
        db.session.commit()
        return redirect(url_for("main.pallet_detail", pallet_id=pallet.id))
    return render_template("pallet_new_mixed.html", zones=Zone.query.order_by(Zone.name).all())


def _next_pallet_number(pallet):
    """Pallets with the same name are numbered #1, #2, #3... The lowest number not in use is
    taken, so the number of a deleted pallet gets reused."""
    base = pallet.base_name.upper()
    used = {
        p.number_in_job
        for p in Pallet.query.all()
        if p is not pallet and p.base_name.upper() == base and p.number_in_job
    }
    number = 1
    while number in used:
        number += 1
    return number


def _new_single_pallet(job, zone_id):
    """New pallet for one job, numbered among pallets with the same name (not committed yet)."""
    pallet = Pallet(kind="single", job=job, zone_id=zone_id)
    pallet.number_in_job = _next_pallet_number(pallet)
    db.session.add(pallet)
    job.refresh_status()
    return pallet


def _create_single_pallet(job, zone_id):
    pallet = _new_single_pallet(job, zone_id)
    db.session.flush()
    log(f"Created pallet {_pallet_ref(pallet)}", job_id=job.id, pallet_id=pallet.id)
    db.session.commit()
    return redirect(url_for("main.pallet_detail", pallet_id=pallet.id))


@bp.route("/jobs/<int:job_id>/pallets", methods=["POST"])
@login_required
def job_add_pallet(job_id):
    job = db.get_or_404(Job, job_id)
    return _create_single_pallet(job, _zone_id(request.form.get("zone_id")))


@bp.route("/pallets/new-single", methods=["GET", "POST"])
@login_required
def pallet_new_single():
    if request.method == "POST":
        job = db.session.get(Job, request.form.get("job_id", type=int) or 0)
        if job is None:
            flash("Pick a job.")
        else:
            return _create_single_pallet(job, _zone_id(request.form.get("zone_id")))
    return render_template(
        "pallet_new_single.html",
        jobs=_open_jobs(),
        zones=Zone.query.order_by(Zone.name).all(),
    )


@bp.route("/jobs/<int:job_id>/complete", methods=["POST"])
@login_required
def job_toggle_complete(job_id):
    job = db.get_or_404(Job, job_id)
    job.complete = not job.complete
    job.refresh_status()
    log(f"{'Marked job ' + job.display_name + ' complete' if job.complete else 'Reopened job ' + job.display_name}", job_id=job.id)
    db.session.commit()
    return redirect(url_for("main.job_detail", job_id=job.id))


@bp.route("/pallets/<int:pallet_id>")
@login_required
def pallet_detail(pallet_id):
    pallet = db.get_or_404(Pallet, pallet_id)
    other_pallets = (
        Pallet.query.filter(Pallet.status != "shipped", Pallet.id != pallet.id)
        .order_by(Pallet.id)
        .all()
    )
    return render_template(
        "pallet_detail.html",
        pallet=pallet,
        zones=Zone.query.order_by(Zone.name).all(),
        statuses=PALLET_STATUSES,
        other_pallets=other_pallets,
    )


@bp.route("/pallets/<int:pallet_id>/rename", methods=["POST"])
@login_required
def pallet_rename(pallet_id):
    pallet = db.get_or_404(Pallet, pallet_id)
    if pallet.kind == "mixed":
        old_name = pallet.name
        old_base = pallet.base_name.upper()
        pallet.custom_name = _clean(request.form.get("name")).upper() or None
        if pallet.base_name.upper() != old_base:  # moved to a different name: number it there
            pallet.number_in_job = _next_pallet_number(pallet)
        if pallet.name != old_name:
            log(f"Renamed pallet {old_name} to {pallet.name}", pallet_id=pallet.id)
        db.session.commit()
    return redirect(url_for("main.pallet_detail", pallet_id=pallet.id))


@bp.route("/pallets/<int:pallet_id>/zone", methods=["POST"])
@login_required
def pallet_zone(pallet_id):
    pallet = db.get_or_404(Pallet, pallet_id)
    old_zone = pallet.zone.name if pallet.zone else "no zone"
    new_id = _zone_id(request.form.get("zone_id"))
    new_zone = db.session.get(Zone, new_id) if new_id else None
    pallet.zone_id = new_id
    if (new_zone.name if new_zone else "no zone") != old_zone:
        log(
            f"Moved pallet {_pallet_ref(pallet)} from {old_zone} to {new_zone.name if new_zone else 'no zone'}",
            job_id=pallet.job_id,
            pallet_id=pallet.id,
        )
    db.session.commit()
    return redirect(url_for("main.pallet_detail", pallet_id=pallet.id))


@bp.route("/pallets/<int:pallet_id>/status", methods=["POST"])
@login_required
def pallet_status(pallet_id):
    pallet = db.get_or_404(Pallet, pallet_id)
    status = request.form.get("status")
    if status in PALLET_STATUSES:
        old_status = pallet.status
        pallet.set_status(status)
        newly_shipped = _refresh_jobs_for_pallet(pallet)
        if old_status != status:
            log(
                f"Pallet {_pallet_ref(pallet)}: {old_status} -> {status}",
                job_id=pallet.job_id,
                pallet_id=pallet.id,
            )
        for job in newly_shipped:
            log(f"Job {job.display_name} is now shipped ({job.pallet_count} pallet(s))", job_id=job.id)
        db.session.commit()
        for job in newly_shipped:
            flash(f"Job {job.display_name} is now shipped: {job.pallet_count} pallet(s) shipped.")
    return redirect(url_for("main.pallet_detail", pallet_id=pallet.id))


def _open_jobs():
    return Job.query.filter_by(shipped=False).order_by(Job.number, Job.line).all()


@bp.route("/pallets/<int:pallet_id>/add", methods=["GET", "POST"])
@login_required
def pallet_add_item(pallet_id):
    pallet = db.get_or_404(Pallet, pallet_id)
    if request.method == "POST":
        return _save_pallet_item(pallet)
    job = pallet.job if pallet.kind == "single" else None
    if job is None:
        job_id = request.args.get("job", type=int)
        job = db.session.get(Job, job_id) if job_id else None
    if job is None:  # mixed pallet: pick the job first
        return render_template("pallet_add.html", pallet=pallet, step=1, jobs=_open_jobs())
    return render_template("pallet_add.html", pallet=pallet, step=2, job=job, form={})


@bp.route("/pallets/<int:pallet_id>/add-all", methods=["POST"])
@login_required
def pallet_add_all(pallet_id):
    """Put everything of the job that is not on a pallet yet onto this pallet."""
    pallet = db.get_or_404(Pallet, pallet_id)
    job = db.session.get(Job, request.form.get("job_id", type=int) or 0)
    if job is None or (pallet.kind == "single" and job.id != pallet.job_id):
        flash("That job can't go on this pallet.")
        return redirect(url_for("main.pallet_detail", pallet_id=pallet.id))

    moved = 0
    moved_parts = []
    for part in job.parts:
        remaining = part.remaining_quantity
        if remaining <= 0:
            continue
        item = next((i for i in pallet.items if i.part is part), None)
        if item:
            item.quantity += remaining
        else:
            db.session.add(PalletItem(pallet=pallet, part=part, quantity=remaining))
        part.set_stage("Complete")  # its full quantity is now on pallets
        moved += remaining
        moved_parts.append((part, remaining))

    if not moved:
        flash("Everything for this job is already on pallets.")
        return redirect(url_for("main.pallet_add_item", pallet_id=pallet.id, job=job.id))
    job.refresh_status()
    for moved_part, count in moved_parts:
        log(
            f"Put {count} of {_pn(moved_part)} (job {job.display_name}) on pallet {_pallet_ref(pallet)} (add all)",
            job_id=job.id,
            pallet_id=pallet.id,
            part_id=moved_part.id,
        )
    db.session.commit()
    flash(f"Added {moved} piece(s) from {job.display_name} to this pallet.")
    return redirect(url_for("main.pallet_detail", pallet_id=pallet.id))


def _save_pallet_item(pallet):
    f = request.form
    job = db.session.get(Job, f.get("job_id", type=int) or 0)
    if job is None or (pallet.kind == "single" and job.id != pallet.job_id):
        flash("That job can't go on this pallet.")
        return redirect(url_for("main.pallet_detail", pallet_id=pallet.id))

    def again(message):
        flash(message)
        return render_template("pallet_add.html", pallet=pallet, step=2, job=job, form=f)

    quantity = _positive_int(f.get("quantity"))
    if quantity is None:
        return again("Enter a quantity above 0.")

    choice = f.get("part", "")
    if choice == "new":
        finish = f.get("finish") if f.get("finish") in ("straight", "bent") else "straight"
        part_number = _clean(f.get("part_number")).upper() or None
        part = None
        if part_number:  # same job + part number + finish: add to that part instead of a duplicate
            part = next(
                (
                    p
                    for p in job.parts
                    if (p.part_number or "").lower() == part_number.lower() and p.finish == finish
                ),
                None,
            )
        if part:
            part.quantity += quantity
        else:
            part = Part(
                job=job, part_number=part_number, quantity=quantity, stage="Complete", finish=finish
            )
            db.session.add(part)
            part.set_stage("Complete")
    else:
        try:
            part = db.session.get(Part, int(choice))
        except ValueError:
            part = None
        if part is None or part.job_id != job.id:
            return again("Pick a part, or choose a new part.")

    item = next((i for i in pallet.items if i.part is part), None)
    if item:
        item.quantity += quantity
    else:
        db.session.add(PalletItem(pallet=pallet, part=part, quantity=quantity))

    # A part is complete once its full quantity is on pallets (skips any stages not yet clicked)
    on_pallets = part.quantity_on_pallets + (part.taken_quantity or 0)
    if on_pallets >= part.quantity:
        part.set_stage("Complete")
    if on_pallets > part.quantity:
        flash(f"Warning: {on_pallets} of this part are on pallets but its quantity is {part.quantity}.")
    if job.total_quantity and job.quantity_accounted > job.total_quantity:
        flash(
            f"Warning: {job.quantity_accounted} are on pallets but the job total is {job.total_quantity}."
        )
    job.refresh_status()
    new_note = " (new part)" if choice == "new" else ""
    db.session.flush()
    log(
        f"Put {quantity} of {_pn(part)}{new_note} (job {job.display_name}) on pallet {_pallet_ref(pallet)}",
        job_id=job.id,
        pallet_id=pallet.id,
        part_id=part.id,
    )
    db.session.commit()
    return redirect(url_for("main.pallet_detail", pallet_id=pallet.id))


# ---------------------------------------------------------------- search

@bp.route("/search")
@login_required
def search():
    q = _clean(request.args.get("q"))
    jobs, parts, found_pallets = [], [], []
    if q:
        like = f"%{q}%"
        jobs = Job.query.filter(Job.number.ilike(like)).order_by(Job.number, Job.line).all()
        parts = Part.query.filter(Part.part_number.ilike(like)).order_by(Part.part_number).all()
        found = {}
        for job in jobs:
            for pallet in job.all_pallets:
                found[pallet.id] = pallet
        named = Pallet.query.filter(db.or_(Pallet.project.ilike(like), Pallet.custom_name.ilike(like)))
        for pallet in named.all():
            found[pallet.id] = pallet
        found_pallets = sorted(found.values(), key=lambda p: p.id)
    return render_template("search.html", q=q, jobs=jobs, parts=parts, pallets=found_pallets)


# ---------------------------------------------------------------- pallets by group

@bp.route("/pallet-groups")
@login_required
def pallet_groups():
    """Every pallet grouping (each job's pallets, each mixed-pallet name) with what is on each pallet."""
    show_shipped = request.args.get("shipped") == "1"
    groups = {}
    for pallet in Pallet.query.all():
        key = (pallet.kind, pallet.base_name.upper())
        group = groups.setdefault(key, {"kind": pallet.kind, "name": key[1], "pallets": []})
        group["pallets"].append(pallet)

    sections = {"single": [], "mixed": []}
    for group in groups.values():
        pallets = sorted(group["pallets"], key=lambda p: (p.number_in_job or 0, p.id))
        visible = [p for p in pallets if show_shipped or p.status != "shipped"]
        if not visible:
            continue
        sections[group["kind"]].append(
            {
                "name": group["name"],
                "total": len(pallets),
                "not_shipped": sum(1 for p in pallets if p.status != "shipped"),
                "pallets": visible,
            }
        )
    for kind in sections:
        sections[kind].sort(key=lambda g: _natural(g["name"]))
    return render_template(
        "pallet_groups.html",
        job_groups=sections["single"],
        mixed_groups=sections["mixed"],
        show_shipped=show_shipped,
    )


# ---------------------------------------------------------------- move / take off / delete

@bp.route("/pallet-items/<int:item_id>/move", methods=["POST"])
@login_required
def item_move(item_id):
    """Move some of a part to another pallet (or a new one), or take it off (used up / taken away)."""
    item = db.get_or_404(PalletItem, item_id)
    source = item.pallet
    part = item.part
    job = part.job
    back = redirect(url_for("main.pallet_detail", pallet_id=source.id))

    qty = _positive_int(request.form.get("quantity"))
    if qty is None:
        flash("Enter how many, above 0.")
        return back
    if qty > item.quantity:
        flash(f"Warning: only {item.quantity} were on this pallet, so {item.quantity} were moved.")
        qty = item.quantity

    dest_value = request.form.get("dest", "")
    dest = None
    if dest_value not in ("remove", "new", "new_mixed"):
        try:
            dest = db.session.get(Pallet, int(dest_value))
        except ValueError:
            dest = None
        if (
            dest is None
            or dest.id == source.id
            or dest.status == "shipped"
            or (dest.kind == "single" and dest.job_id != job.id)
        ):
            flash("Pick a different pallet for these parts, or take them off.")
            return back
    elif dest_value == "new":
        dest = _new_single_pallet(job, None)
    elif dest_value == "new_mixed":
        new_name = _clean(request.form.get("new_name")).upper()
        if not new_name:
            flash("Type a name for the new mixed pallet.")
            return back
        dest = Pallet(kind="mixed", custom_name=new_name)
        dest.number_in_job = _next_pallet_number(dest)  # same name as another pallet: it gets the next number
        db.session.add(dest)

    item.quantity -= qty
    if item.quantity <= 0:
        db.session.delete(item)

    label = part.part_number or "the part"
    if dest is None:
        part.taken_quantity = (part.taken_quantity or 0) + qty
        flash(f"Took {qty} of {label} off the pallet (used up or taken away).")
    else:
        dest_item = next((i for i in dest.items if i.part is part), None)
        if dest_item:
            dest_item.quantity += qty
        else:
            db.session.add(PalletItem(pallet=dest, part=part, quantity=qty))
        flash(f"Moved {qty} of {label} to {dest.name}.")

    job.refresh_status()
    db.session.flush()
    if dest is None:
        log(
            f"Took {qty} of {_pn(part)} (job {job.display_name}) off pallet {_pallet_ref(source)} (used up or taken away)",
            job_id=job.id,
            pallet_id=source.id,
            part_id=part.id,
        )
    else:
        log(
            f"Moved {qty} of {_pn(part)} (job {job.display_name}) from pallet {_pallet_ref(source)} to pallet {_pallet_ref(dest)}",
            job_id=job.id,
            pallet_id=source.id,
            part_id=part.id,
        )
    db.session.commit()
    return back


@bp.route("/parts/<int:part_id>/edit", methods=["GET", "POST"])
@login_required
def part_edit(part_id):
    part = db.get_or_404(Part, part_id)
    if request.method == "POST":
        form = request.form
        quantity = _positive_int(form.get("quantity"))
        if quantity is None:
            flash("Quantity must be a whole number above 0.")
        else:
            part.part_number = _clean(form.get("part_number")).upper() or None
            part.quantity = quantity
            finish = form.get("finish")
            part.finish = finish if finish in ("straight", "bent") else None
            accounted = part.quantity_on_pallets + (part.taken_quantity or 0)
            if accounted > quantity:
                flash(f"Warning: {accounted} of this part are on or taken off pallets, but its quantity is now {quantity}.")
            part.job.refresh_status()
            log(f"Edited part {_pn(part)} (qty {part.quantity}) in job {part.job.display_name}", job_id=part.job_id, part_id=part.id)
            db.session.commit()
            return redirect(url_for("main.job_detail", job_id=part.job_id))
    else:
        form = {
            "part_number": part.part_number or "",
            "quantity": part.quantity,
            "finish": part.finish or "",
        }
    return render_template("part_edit.html", part=part, form=form)


@bp.route("/parts/<int:part_id>/delete", methods=["GET", "POST"])
@login_required
def part_delete(part_id):
    part = db.get_or_404(Part, part_id)
    job = part.job
    if request.method == "POST":
        label = part.part_number or "the part"
        log(f"Deleted part {_pn(part)} (qty {part.quantity}) from job {job.display_name}", job_id=job.id, part_id=part.id)
        for item in list(part.pallet_items):
            db.session.delete(item)
        db.session.delete(part)
        db.session.flush()
        db.session.expire_all()
        job.refresh_status()
        db.session.commit()
        flash(f"Deleted {label} from job {job.display_name}.")
        return redirect(url_for("main.job_detail", job_id=job.id))
    return render_template("part_delete.html", part=part)


@bp.route("/pallets/<int:pallet_id>/delete", methods=["GET", "POST"])
@login_required
def pallet_delete(pallet_id):
    pallet = db.get_or_404(Pallet, pallet_id)
    if request.method == "POST":
        jobs = {}
        if pallet.job:
            jobs[pallet.job.id] = pallet.job
        for item in pallet.items:
            jobs[item.part.job_id] = item.part.job
        name = pallet.name
        log(
            f"Deleted pallet {name} ({len(pallet.items)} item(s))",
            job_id=pallet.job_id,
            pallet_id=pallet.id,
        )
        db.session.delete(pallet)  # its items go with it
        db.session.flush()
        db.session.expire_all()
        for job in jobs.values():
            job.refresh_status()
        db.session.commit()
        flash(f"Deleted pallet {name}.")
        return redirect(url_for("main.pallets"))
    return render_template("pallet_delete.html", pallet=pallet)


@bp.route("/zones/<int:zone_id>/delete", methods=["GET", "POST"])
@login_required
def zone_delete(zone_id):
    zone = db.get_or_404(Zone, zone_id)
    if request.method == "POST":
        name = zone.name
        log(f"Deleted zone {name} ({len(zone.pallets)} pallet(s) now have no zone)")
        for pallet in list(zone.pallets):
            pallet.zone = None  # back to "no zone yet"
        db.session.delete(zone)
        db.session.commit()
        flash(f"Deleted zone {name}.")
        return redirect(url_for("main.zones"))
    return render_template("zone_delete.html", zone=zone)


# ---------------------------------------------------------------- activity log

@bp.route("/log")
@login_required
def audit_log():
    q = _clean(request.args.get("q"))
    user = _clean(request.args.get("user"))
    job_id = request.args.get("job", type=int)
    pallet_id = request.args.get("pallet", type=int)
    part_id = request.args.get("part", type=int)
    person_id = request.args.get("person", type=int)
    page = max(request.args.get("page", 1, type=int), 1)

    query = AuditEvent.query
    if q:
        query = query.filter(AuditEvent.summary.ilike(f"%{q}%"))
    if user:
        query = query.filter(AuditEvent.username == user)
    if job_id:
        query = query.filter(AuditEvent.job_id == job_id)
    if pallet_id:
        query = query.filter(AuditEvent.pallet_id == pallet_id)
    if part_id:
        query = query.filter(AuditEvent.part_id == part_id)
    if person_id:  # what this person did, and changes made to their account
        query = query.filter(db.or_(AuditEvent.user_id == person_id, AuditEvent.target_user_id == person_id))

    title = None
    if job_id:
        job = db.session.get(Job, job_id)
        title = f"job {job.display_name}" if job else "a deleted job"
    elif pallet_id:
        pallet = db.session.get(Pallet, pallet_id)
        title = f"pallet {pallet.name}" if pallet else "a deleted pallet"
    elif part_id:
        part = db.session.get(Part, part_id)
        title = f"part {_pn(part)} (job {part.job.display_name})" if part else "a deleted part"
    elif person_id:
        person = db.session.get(User, person_id)
        title = f"user {person.username}" if person else "a deleted user"

    per_page = 100
    events = (
        query.order_by(AuditEvent.id.desc()).offset((page - 1) * per_page).limit(per_page + 1).all()
    )
    more = len(events) > per_page
    return render_template(
        "audit_log.html",
        events=events[:per_page],
        q=q,
        user=user,
        job_id=job_id,
        pallet_id=pallet_id,
        part_id=part_id,
        person_id=person_id,
        title=title,
        page=page,
        more=more,
        usernames=[u.username for u in User.query.order_by(User.username)],
    )


# ---------------------------------------------------------------- user accounts

@bp.route("/users", methods=["GET", "POST"])
@login_required
def users():
    if request.method == "POST":
        action = request.form.get("action")
        password = request.form.get("password", "")
        if len(password) < 6:
            flash("Passwords need at least 6 characters.")
        elif action == "add":
            username = _clean(request.form.get("username")).upper()
            if not username:
                flash("Enter a username.")
            elif User.query.filter(db.func.lower(User.username) == username.lower()).first():
                flash("That username already exists.")
            else:
                user = User(username=username)
                user.set_password(password)
                db.session.add(user)
                db.session.flush()
                log(f"Added user {username}", target_user_id=user.id)
                db.session.commit()
                flash(f"Added user {username}.")
        elif action == "reset":
            user = db.session.get(User, request.form.get("user_id", type=int) or 0)
            if user is None:
                flash("That user wasn't found.")
            else:
                user.set_password(password)
                log(f"Reset the password for {user.username}", target_user_id=user.id)
                db.session.commit()
                flash(f"Password changed for {user.username}.")
        return redirect(url_for("main.users"))
    return render_template("users.html", users=User.query.order_by(User.username).all())


# ---------------------------------------------------------------- spreadsheet upload

def _upload_dir():
    return os.path.join(current_app.instance_path, "uploads")


def _load_upload(token):
    state = importer.load_state(_upload_dir(), token)
    if state is None:
        flash("That upload has expired. Please upload the file again.")
    return state


@bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Choose a file to upload.")
        else:
            try:
                headers, rows = importer.parse_file(file.filename, file.read())
            except ValueError as error:
                flash(str(error))
            except Exception:
                flash("That file couldn't be read. Is it a valid .xlsx or .csv file?")
            else:
                importer.cleanup_old(_upload_dir())
                token = importer.new_token()
                importer.save_state(
                    _upload_dir(),
                    token,
                    {"filename": file.filename, "headers": headers, "rows": rows,
                     "mapping": None, "date_format": "ymd"},
                )
                return redirect(url_for("main.upload_map", token=token))
    return render_template("upload.html")


@bp.route("/upload/<token>/map", methods=["GET", "POST"])
@login_required
def upload_map(token):
    state = _load_upload(token)
    if state is None:
        return redirect(url_for("main.upload"))
    headers = state["headers"]
    date_format = state.get("date_format", "ymd")
    if request.method == "POST":
        mapping = {}
        for field, _label in importer.FIELDS:
            value = request.form.get(f"col_{field}", "")
            mapping[field] = int(value) if value.isdigit() and int(value) < len(headers) else None
        date_format = request.form.get("date_format")
        if date_format not in importer.DATE_FORMATS:
            date_format = "ymd"
        if mapping["job"] is None:
            flash("Pick the column that holds the job.")
        else:
            state["mapping"] = mapping
            state["date_format"] = date_format
            importer.save_state(_upload_dir(), token, state)
            return redirect(url_for("main.upload_review", token=token))
    else:
        mapping = state.get("mapping") or importer.guess_mapping(headers)
    return render_template(
        "upload_map.html",
        token=token,
        filename=state["filename"],
        headers=headers,
        mapping=mapping,
        preview=state["rows"][:5],
        fields=importer.FIELDS,
        date_formats=importer.DATE_FORMAT_LABELS,
        date_format=date_format,
    )


def _fmt(value):
    if value is None:
        return "(none)"
    return value.strftime("%b %d, %Y") if hasattr(value, "strftime") else str(value)


def _diff(p, job, part):
    """How the spreadsheet's values differ from an existing job (new values only count if filled in)."""
    changes = []

    def note(field, label, old, new):
        if new is not None and new != old:
            changes.append({"field": field, "label": label, "old": _fmt(old), "new": _fmt(new), "value": new})

    if p["kind"] == "big":
        note("total_quantity", "Total quantity", job.total_quantity, p["quantity"])
    else:
        note("part_quantity", "Quantity", part.quantity, p["quantity"])
    note("due_date", "Due date", job.due_date, p["due_date"])
    note("project", "Project", job.project, p["project"])
    note("assigned_date", "Assigned date", job.assigned_date, p["assigned_date"])
    return changes


def _match_proposals(proposals):
    """Compare each proposed job with what is already in the app.
    new       - nothing like it exists yet
    same      - an existing job already has these values
    changed   - it exists but the values differ; applying creates a NEW copy and the old job stays
    shipped   - it only exists as shipped jobs, so it is skipped
    duplicate - the same job appears twice in this file"""
    seen = set()
    for p in proposals:
        p["name"] = job_label(p["job"], p["label"])
        p["key"] = importer.proposal_key(p)

        pairs = []  # (existing job, its matching part or None)
        if p["kind"] == "big":
            for job in Job.query.filter(
                db.func.lower(Job.number) == p["job"].lower(),
                db.func.lower(db.func.coalesce(Job.line, "")) == p["label"].lower(),
            ).all():
                pairs.append((job, None))
        else:
            wanted = (p["part_number"] or "").lower()
            for job in Job.query.filter(
                db.func.lower(Job.number) == p["job"].lower(),
                db.or_(Job.line.is_(None), Job.line == ""),
            ).all():
                for part in job.parts:
                    if (part.part_number or "").lower() == wanted:
                        pairs.append((job, part))
                        break

        changes = []
        if p["key"] in seen:
            status = "duplicate"
        elif not pairs:
            status = "new"
        elif any(not _diff(p, job, part) for job, part in pairs):
            status = "same"
        else:
            open_pairs = [(job, part) for job, part in pairs if not job.shipped]
            if not open_pairs:
                status = "shipped"
            else:
                status = "changed"
                job, part = max(open_pairs, key=lambda pair: pair[0].id)  # newest open copy
                changes = _diff(p, job, part)
        seen.add(p["key"])
        p["status"], p["changes"] = status, changes
    return proposals


def _create_job_from_proposal(p, note=""):
    job = Job(
        number=p["job"],
        line=p["label"],
        project=p["project"],
        assigned_date=p["assigned_date"],
        due_date=p["due_date"],
        total_quantity=p["quantity"] if p["kind"] == "big" else None,
    )
    db.session.add(job)
    if p["kind"] == "single":
        part = Part(job=job, part_number=p["part_number"], quantity=p["quantity"], stage=STAGES[0])
        db.session.add(part)
        part.set_stage(STAGES[0])
    job.refresh_status()
    db.session.flush()
    log(
        f"Added job {job.display_name} from spreadsheet{note}",
        job_id=job.id,
        part_id=job.parts[0].id if job.parts else None,
    )
    return job


def _apply_proposals(proposals, apply_keys, filename):
    created = copies = 0
    for p in proposals:
        if p["status"] == "new":
            _create_job_from_proposal(p)
            created += 1
        elif p["status"] == "changed" and p["key"] in apply_keys:
            what = ", ".join(c["label"].lower() for c in p["changes"])
            _create_job_from_proposal(p, note=f" as a new copy (changed: {what}); the old job was kept")
            copies += 1
    log(f"Spreadsheet upload {filename}: {created} new job(s), {copies} copy(ies) of changed jobs")
    return created, copies


@bp.route("/upload/<token>/review", methods=["GET", "POST"])
@login_required
def upload_review(token):
    state = _load_upload(token)
    if state is None:
        return redirect(url_for("main.upload"))
    if not state.get("mapping"):
        return redirect(url_for("main.upload_map", token=token))

    records, skipped = importer.normalize_rows(state["rows"], state["mapping"], state["date_format"])

    # jobs that have line rows (these can be combined)
    line_jobs = []
    index_by_key = {}
    for rec in records:
        if not rec["line"]:
            continue
        key = rec["job"].lower()
        if key not in index_by_key:
            index_by_key[key] = len(line_jobs)
            line_jobs.append({"index": len(line_jobs), "key": key, "name": rec["job"], "records": []})
        line_jobs[index_by_key[key]]["records"].append(rec)

    form = request.form if request.method == "POST" else {}
    groups, labels = {}, {}
    if request.method == "POST":
        for rec in records:
            group = form.get(f"grp_{rec['row']}", "").strip()
            if group:
                groups[rec["row"]] = group
        for name, value in form.items():
            if name.startswith("label_"):
                try:
                    _prefix, job_index, group = name.split("_", 2)
                    labels[(line_jobs[int(job_index)]["key"], group)] = value
                except (ValueError, IndexError):
                    continue

    proposals = _match_proposals(importer.build_proposals(records, groups, labels))

    if request.method == "POST" and form.get("action") == "apply":
        created, copies = _apply_proposals(proposals, set(form.getlist("apply")), state["filename"])
        db.session.commit()
        importer.delete_state(_upload_dir(), token)
        flash(f"Upload finished: {created} new job(s) created, {copies} changed job(s) saved as new copies.")
        return redirect(url_for("main.home"))

    multi = []
    for job in line_jobs:
        if len(job["records"]) < 2:
            continue
        used = {groups[r["row"]] for r in job["records"] if r["row"] in groups}
        job["label_groups"] = sorted({"1", "2", "3"} | used)
        multi.append(job)

    counts = {s: sum(1 for p in proposals if p["status"] == s) for s in ("new", "changed", "same", "shipped", "duplicate")}
    return render_template(
        "upload_review.html",
        token=token,
        filename=state["filename"],
        form=form,
        proposals=proposals,
        skipped=skipped,
        warned=[r for r in records if r["warnings"]],
        multi=multi,
        group_ids=["1", "2", "3", "4", "5", "6"],
        counts=counts,
    )


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def user_edit(user_id):
    user = db.get_or_404(User, user_id)
    form = request.form if request.method == "POST" else {"username": user.username}
    if request.method == "POST":
        username = _clean(form.get("username")).upper()
        password = form.get("password", "")
        clash = User.query.filter(
            db.func.lower(User.username) == username.lower(), User.id != user.id
        ).first()
        if not username:
            flash("Enter a username.")
        elif clash:
            flash("That username already exists.")
        elif password and len(password) < 6:
            flash("Passwords need at least 6 characters.")
        else:
            old_name = user.username
            if username != old_name:
                user.username = username
                log(f"Renamed user {old_name} to {username}", target_user_id=user.id)
            if password:
                user.set_password(password)
                log(f"Reset the password for {username}", target_user_id=user.id)
            db.session.commit()
            flash(f"Saved {username}.")
            return redirect(url_for("main.users"))
    return render_template("user_edit.html", user=user, form=form)


@bp.route("/users/<int:user_id>/delete", methods=["GET", "POST"])
@login_required
def user_delete(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You can't delete the account you are logged in with.")
        return redirect(url_for("main.users"))
    if User.query.count() <= 1:
        flash("The last account can't be deleted.")
        return redirect(url_for("main.users"))
    if request.method == "POST":
        name = user.username
        log(f"Deleted user {name}", target_user_id=user.id)
        db.session.delete(user)
        db.session.commit()
        flash(f"Deleted user {name}.")
        return redirect(url_for("main.users"))
    return render_template("user_delete.html", user=user)


# ---------------------------------------------------------------- per-job summary

@bp.route("/jobs/<int:job_id>/summary")
@login_required
def job_summary(job_id):
    job = db.get_or_404(Job, job_id)
    part_rows = [
        {
            "part": part,
            "on_pallets": part.quantity_on_pallets,
            "taken": part.taken_quantity or 0,
            "remaining": part.remaining_quantity,
        }
        for part in job.parts
    ]
    pallet_rows = [
        (pallet, sum(i.quantity for i in pallet.items if i.part.job_id == job.id))
        for pallet in job.all_pallets
    ]
    if job.total_quantity:
        not_yet = max(job.total_quantity - job.quantity_accounted, 0)
    else:
        not_yet = sum(row["remaining"] for row in part_rows)
    return render_template(
        "job_summary.html", job=job, part_rows=part_rows, pallet_rows=pallet_rows, not_yet=not_yet
    )


# ---------------------------------------------------------------- CSV export

def _csv_cell(value):
    """Plain text for a spreadsheet. Text that starts with = + - @ is made safe so Excel
    doesn't treat it as a formula."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _csv_response(name, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_cell(v) for v in row])
    filename = f"{name}-{date.today().isoformat()}.csv"
    return Response(
        "\ufeff" + buffer.getvalue(),  # BOM so Excel reads it as UTF-8
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/export")
@login_required
def export():
    return render_template("export.html")


@bp.route("/export/jobs.csv")
@login_required
def export_jobs():
    jobs = Job.query.order_by(Job.number, Job.line).all()
    rows = [
        [
            j.number, j.line, j.project, j.assigned_date, j.due_date, j.quantity_display,
            j.part_display, j.pallet_count, j.status_display, j.completed_on, j.wrapped_on,
            j.shipped_on, "Yes" if j.shipped else "No",
        ]
        for j in jobs
    ]
    header = ["Job", "Line", "Project", "Assigned date", "Due date", "Quantity", "Part", "Pallets",
              "Status", "Complete on", "Wrapped on", "Shipped on", "Shipped"]
    return _csv_response("jobs", header, rows)


@bp.route("/export/parts.csv")
@login_required
def export_parts():
    rows = []
    for job in Job.query.order_by(Job.number, Job.line).all():
        for part in job.parts:
            rows.append(
                [
                    job.number, job.line, part.part_number, part.finish, part.quantity, part.stage,
                    part.completed_on, part.quantity_on_pallets, part.taken_quantity or 0,
                    part.remaining_quantity,
                ]
            )
    header = ["Job", "Line", "Part", "Straight/bent", "Quantity", "Stage", "Complete on",
              "On pallets", "Taken off pallets", "Not yet on a pallet"]
    return _csv_response("parts", header, rows)


@bp.route("/export/pallets.csv")
@login_required
def export_pallets():
    rows = []
    for pallet in Pallet.query.order_by(Pallet.id).all():
        base = [
            pallet.name, "Mixed" if pallet.kind == "mixed" else "Job",
            pallet.zone.name if pallet.zone else "", pallet.status, pallet.wrapped_on, pallet.shipped_on,
        ]
        if not pallet.items:
            rows.append(base + ["", "", "", ""])
        for item in pallet.items:
            rows.append(
                base + [item.part.job.display_name, item.part.part_number, item.part.finish, item.quantity]
            )
    header = ["Pallet", "Type", "Zone", "Status", "Wrapped on", "Shipped on", "Job", "Part",
              "Straight/bent", "Quantity"]
    return _csv_response("pallets", header, rows)


@bp.route("/export/activity.csv")
@login_required
def export_activity():
    events = AuditEvent.query.order_by(AuditEvent.id).all()
    rows = [
        [e.at.strftime("%Y-%m-%d %H:%M") if e.at else "", e.username, e.summary] for e in events
    ]
    return _csv_response("activity-log", ["When", "Who", "What"], rows)


# ---------------------------------------------------------------- backups

def _backup_dir():
    return os.path.join(current_app.instance_path, "backups")


@bp.route("/backups", methods=["GET", "POST"])
@login_required
def backups_page():
    if request.method == "POST":
        try:
            path, copy_error = backups.make_backup(
                db.engine.url.database,
                _backup_dir(),
                current_app.config.get("BACKUP_COPY_DIR"),
                current_app.config["BACKUP_KEEP"],
            )
        except Exception:
            current_app.logger.exception("Manual backup failed")
            flash("The backup failed. Check that the disk has space and try again.")
        else:
            log(f"Created a backup ({os.path.basename(path)})")
            db.session.commit()
            message = "Backup created."
            if copy_error:
                message += f" The extra copy to the other folder failed: {copy_error}"
            flash(message)
        return redirect(url_for("main.backups_page"))
    return render_template(
        "backups.html",
        files=backups.list_backups(_backup_dir()),
        folder=_backup_dir(),
        copy_dir=current_app.config.get("BACKUP_COPY_DIR"),
        keep=current_app.config["BACKUP_KEEP"],
    )


@bp.route("/backups/<name>")
@login_required
def backup_download(name):
    if not backups.is_backup_name(name):
        abort(404)
    return send_from_directory(_backup_dir(), name, as_attachment=True)


# ---------------------------------------------------------------- how to connect

@bp.route("/connect")
@login_required
def connect():
    """Shows the addresses other devices can type in. Open it on the tracker computer."""
    return render_template(
        "connect.html",
        addresses=network.local_addresses(),
        name=network.computer_name(),
        port=request.environ.get("SERVER_PORT", "5000"),
    )
