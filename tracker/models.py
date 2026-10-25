from datetime import date, datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, login_manager

# Edit this list to match your real stages. Order is the order shown in the dropdown.
STAGES = [
    "Waiting to cut",
    "Cut",
    "Threading",
    "Bending",
    "Notching",
    "Deburring",
    "Complete",
]


# A job is "due soon" when it is due within this many days (and not yet overdue)
DUE_SOON_DAYS = 7

PALLET_STATUSES = ["open", "wrapped", "shipped"]
PALLET_RANK = {"open": 0, "wrapped": 1, "shipped": 2}
# What a one-part job shows once its part is on a pallet (earliest pallet status wins)
PALLET_STATUS_LABEL = {"open": "On pallet", "wrapped": "Wrapped", "shipped": "Shipped"}


def utcnow():
    return datetime.now(timezone.utc)


def job_label(number, line):
    """'125' or '125 - line 2' or '1 - lines 500-505' (plural when the label is a range/list)."""
    if not line:
        return number
    plural = any(ch in line for ch in "-/,&")
    return f"{number} - {'lines' if plural else 'line'} {line}"


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class Job(db.Model):
    """A job number plus an optional free-text line label (e.g. "2", "1-5", "2/5").
    Job numbers are NOT unique; number + line is only how people tell jobs apart."""

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(50), nullable=False, index=True)
    line = db.Column(db.String(50))
    project = db.Column(db.String(120))
    assigned_date = db.Column(db.Date)
    due_date = db.Column(db.Date)
    # Total quantity for all parts of a big job (parts get entered when put on a pallet)
    total_quantity = db.Column(db.Integer)
    # Big jobs are marked complete by hand once all their parts are on pallets
    complete = db.Column(db.Boolean, default=False, nullable=False)
    shipped = db.Column(db.Boolean, default=False, nullable=False)
    # Dates are set automatically when the job reaches each point (local date)
    completed_on = db.Column(db.Date)
    wrapped_on = db.Column(db.Date)
    shipped_on = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=utcnow)

    parts = db.relationship("Part", back_populates="job", cascade="all, delete-orphan")
    pallets = db.relationship("Pallet", back_populates="job")

    @property
    def display_name(self):
        return job_label(self.number, self.line)

    # ---- due date
    @property
    def days_until_due(self):
        return (self.due_date - date.today()).days if self.due_date else None

    @property
    def due_state(self):
        """"overdue", "soon", or "" (not due soon, no due date, or already shipped)."""
        days = self.days_until_due
        if self.shipped or days is None:
            return ""
        if days < 0:
            return "overdue"
        return "soon" if days <= DUE_SOON_DAYS else ""

    @property
    def due_note(self):
        days = self.days_until_due
        if self.shipped or days is None:
            return ""
        if days < 0:
            return f"overdue by {-days} day{'s' if days != -1 else ''}"
        if days == 0:
            return "due today"
        return f"due in {days} day{'s' if days != 1 else ''}" if days <= DUE_SOON_DAYS else ""

    # ---- pallets and progress
    @property
    def pallet_items(self):
        return [item for part in self.parts for item in part.pallet_items]

    @property
    def quantity_on_pallets(self):
        return sum(item.quantity for item in self.pallet_items)

    @property
    def quantity_taken(self):
        return sum((part.taken_quantity or 0) for part in self.parts)

    @property
    def quantity_accounted(self):
        """On pallets plus taken off pallets: everything that has been dealt with."""
        return self.quantity_on_pallets + self.quantity_taken

    @property
    def all_pallets(self):
        """Its own pallets plus any mixed pallets holding its parts."""
        pallets = list(self.pallets)
        for item in self.pallet_items:
            if item.pallet not in pallets:
                pallets.append(item.pallet)
        return pallets

    @property
    def pallet_count(self):
        return len(self.all_pallets)

    def refresh_status(self):
        """Recompute complete / wrapped / shipped and stamp the dates the first time each
        is reached (cleared again if the job goes back).
        A big job (with a total quantity) only counts as wrapped or shipped once it is marked
        complete or has its full total on pallets, so shipping the first pallet doesn't hide it."""
        today = date.today()
        pallets = self.all_pallets
        all_on_pallets = not self.total_quantity or self.complete or (
            self.quantity_accounted >= self.total_quantity
        )

        if self.total_quantity:
            is_complete = self.complete
        else:
            is_complete = bool(self.parts) and all(p.stage == "Complete" for p in self.parts)
        self.completed_on = (self.completed_on or today) if is_complete else None

        is_wrapped = (
            bool(pallets)
            and all_on_pallets
            and all(p.status in ("wrapped", "shipped") for p in pallets)
        )
        self.wrapped_on = (self.wrapped_on or today) if is_wrapped else None

        is_shipped = bool(pallets) and all_on_pallets and all(p.status == "shipped" for p in pallets)
        self.shipped = is_shipped
        self.shipped_on = (self.shipped_on or today) if is_shipped else None

    # ---- columns for the main job list
    @property
    def part_display(self):
        """The part number if the job has exactly one part, otherwise blank."""
        return (self.parts[0].part_number or "") if len(self.parts) == 1 else ""

    @property
    def quantity_display(self):
        """Total quantity if given, otherwise the sum of the parts' quantities."""
        if self.total_quantity:
            return self.total_quantity
        if self.parts:
            return sum(p.quantity for p in self.parts)
        return ""

    @property
    def status_display(self):
        if self.shipped:
            return "Shipped"
        if self.total_quantity:  # big job: progress onto pallets
            if self.complete:
                return "Complete"
            text = f"{self.quantity_on_pallets} of {self.total_quantity} on pallets"
            if self.quantity_taken:
                text += f", {self.quantity_taken} taken"
            return text
        if not self.parts:
            return "No parts yet"
        if len(self.parts) == 1:  # one-part job: the part's stage, then its pallet's status
            part = self.parts[0]
            pallets = []
            for item in part.pallet_items:
                if item.pallet not in pallets:
                    pallets.append(item.pallet)
            if part.stage == "Complete" and pallets:
                earliest = min(pallets, key=lambda pal: PALLET_RANK[pal.status])
                return PALLET_STATUS_LABEL[earliest.status]
            return part.stage
        stages = {p.stage for p in self.parts}
        return stages.pop() if len(stages) == 1 else "Mixed"


class Part(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), nullable=False)
    part_number = db.Column(db.String(80))  # may be blank
    quantity = db.Column(db.Integer, nullable=False)
    stage = db.Column(db.String(40), nullable=False, default=STAGES[0])
    finish = db.Column(db.String(10))  # "straight" / "bent" (used for parts entered at pallet time)
    completed_on = db.Column(db.Date)  # set when the part reaches Complete
    # How many were taken off pallets (used up / taken away) rather than moved to another pallet
    taken_quantity = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)

    job = db.relationship("Job", back_populates="parts")
    pallet_items = db.relationship("PalletItem", back_populates="part")

    @property
    def quantity_on_pallets(self):
        return sum(item.quantity for item in self.pallet_items)

    @property
    def remaining_quantity(self):
        """How many of this part are not on any pallet yet."""
        return max(self.quantity - self.quantity_on_pallets - (self.taken_quantity or 0), 0)

    def set_stage(self, stage):
        """Change the stage and keep the completion date in step with it."""
        self.stage = stage
        if stage == "Complete":
            self.completed_on = self.completed_on or date.today()
        else:
            self.completed_on = None


class Zone(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    notes = db.Column(db.Text)
    # Position on the floor map, in map units (the map is 1000 wide by 600 high). None = not on the map.
    map_x = db.Column(db.Float)
    map_y = db.Column(db.Float)
    map_w = db.Column(db.Float)
    map_h = db.Column(db.Float)

    pallets = db.relationship("Pallet", back_populates="zone")


class Pallet(db.Model):
    """kind == "single": all parts belong to one job (job_id set, numbered within the job).
    kind == "mixed": parts from different jobs; labeled by assigned date (week) + project.
    Every pallet's unique internal number is its id. zone_id may be empty (no spot yet)."""

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False, default="single")
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"))
    number_in_job = db.Column(db.Integer)
    custom_name = db.Column(db.String(120))  # mixed pallets: a name the user types
    assigned_date = db.Column(db.Date)  # mixed pallets (optional, used only if no custom name)
    project = db.Column(db.String(120))  # mixed pallets (optional, used only if no custom name)
    status = db.Column(db.String(10), nullable=False, default="open")  # open / wrapped / shipped
    wrapped_on = db.Column(db.Date)
    shipped_on = db.Column(db.Date)
    zone_id = db.Column(db.Integer, db.ForeignKey("zone.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    job = db.relationship("Job", back_populates="pallets")
    zone = db.relationship("Zone", back_populates="pallets")

    def set_status(self, status):
        """Change the status and stamp/clear the wrapped and shipped dates."""
        self.status = status
        today = date.today()
        if status == "open":
            self.wrapped_on = None
            self.shipped_on = None
        elif status == "wrapped":
            self.wrapped_on = self.wrapped_on or today
            self.shipped_on = None
        else:  # shipped
            self.shipped_on = self.shipped_on or today

    @property
    def name(self):
        """The base name plus its number: pallets with the same base name are #1, #2, #3..."""
        base = self.base_name.upper()
        return f"{base} #{self.number_in_job}" if self.number_in_job else base

    @property
    def base_name(self):
        if self.kind == "single" and self.job:
            return self.job.display_name
        if self.custom_name:
            return self.custom_name
        bits = ["Mixed"]
        if self.assigned_date:
            bits.append("assigned " + self.assigned_date.strftime("%b %d"))
        if self.project:
            bits.append(self.project)
        return " - ".join(bits)
    items = db.relationship("PalletItem", back_populates="pallet", cascade="all, delete-orphan")


class PalletItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pallet_id = db.Column(db.Integer, db.ForeignKey("pallet.id"), nullable=False)
    part_id = db.Column(db.Integer, db.ForeignKey("part.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)

    pallet = db.relationship("Pallet", back_populates="items")
    part = db.relationship("Part", back_populates="pallet_items")


class AuditEvent(db.Model):
    """Who did what and when. Stores plain text (not links) so entries survive deletions."""

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=datetime.now, index=True)  # local time of this computer
    user_id = db.Column(db.Integer)
    username = db.Column(db.String(80), nullable=False)
    summary = db.Column(db.String(500), nullable=False)
    job_id = db.Column(db.Integer, index=True)  # for "history of this job"
    pallet_id = db.Column(db.Integer, index=True)  # for "history of this pallet"
    part_id = db.Column(db.Integer, index=True)  # for "history of this part"
    target_user_id = db.Column(db.Integer, index=True)  # account changes: which user was changed


class MapObject(db.Model):
    """Something drawn on the floor map that is not a zone: a wall, a rack, or a machine.
    Positions are in map units (the map is 1000 wide by 600 high)."""

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False)  # "wall", "rack" or "machine"
    label = db.Column(db.String(80))
    x = db.Column(db.Float, nullable=False)
    y = db.Column(db.Float, nullable=False)
    w = db.Column(db.Float, nullable=False)
    h = db.Column(db.Float, nullable=False)
