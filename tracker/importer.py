"""Reading the job spreadsheet. Pure Python (no Flask or database), so it is easy to test.

Flow: parse_file -> guess_mapping -> normalize_rows -> build_proposals.
A row WITH a line is a big job (job + line + total quantity; parts are entered later at
pallet time). A row WITHOUT a line is a single-part job (job + part number + quantity).
"""
import csv
import io
import json
import os
import re
import secrets
import time
from datetime import date, datetime

# (field name, label shown on the mapping screen)
FIELDS = [
    ("job", "Job"),
    ("line", "Line"),
    ("quantity", "Quantity"),
    ("part_number", "Part number"),
    ("due_date", "Due date"),
    ("project", "Project"),
    ("assigned_date", "Assigned date"),
]

SYNONYMS = {
    "job": ["job", "job number", "job no", "job #", "job name", "job id", "order", "order number", "work order", "wo"],
    "line": ["line", "line number", "line no", "line #", "lines", "ln"],
    "quantity": ["quantity", "qty", "total quantity", "total qty", "amount", "count", "pieces", "pcs"],
    "part_number": ["part number", "part no", "part #", "part", "part num", "item", "item number", "drawing", "drawing number"],
    "due_date": ["due date", "due", "date due", "ship date", "delivery date", "required date"],
    "project": ["project", "project name"],
    "assigned_date": ["assigned date", "assigned", "date assigned", "assign date", "start date"],
}

DATE_FORMATS = {
    "ymd": ["%Y-%m-%d", "%Y/%m/%d"],
    "mdy": ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y"],
    "dmy": ["%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d %b %Y", "%d-%b-%y"],
}
DATE_FORMAT_LABELS = [
    ("ymd", "Year-Month-Day (2026-03-27)"),
    ("mdy", "Month/Day/Year (03/27/2026)"),
    ("dmy", "Day/Month/Year (27/03/2026)"),
]


# ---------------------------------------------------------------- reading the file

def _cell(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def parse_file(filename, data):
    """Return (headers, rows) of strings from an .xlsx or .csv file."""
    name = (filename or "").lower()
    if name.endswith((".csv", ".txt")):
        text = data.decode("utf-8-sig", errors="replace")
        raw = list(csv.reader(io.StringIO(text)))
    elif name.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            raise ValueError(
                "Reading Excel files needs the openpyxl package (pip install openpyxl). "
                "Or save the sheet as a .csv file and upload that."
            )
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        raw = [list(row) for row in workbook.worksheets[0].iter_rows(values_only=True)]
        workbook.close()
    else:
        raise ValueError("Upload an .xlsx or .csv file.")

    rows = [[_cell(c) for c in r] for r in raw]
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        raise ValueError("The file needs a header row and at least one row of jobs under it.")
    headers = rows[0]
    width = len(headers)
    return headers, [(r + [""] * width)[:width] for r in rows[1:]]


def _norm(text):
    return re.sub(r"[^a-z0-9# ]+", " ", text.lower()).strip()


def guess_mapping(headers):
    """Best guess of which column holds each field: {field: column index or None}."""
    normalized = [_norm(h) for h in headers]
    mapping = {field: None for field, _ in FIELDS}
    used = set()
    for exact in (True, False):  # exact matches first, then "contains"
        for field, _ in FIELDS:
            if mapping[field] is not None:
                continue
            for i, h in enumerate(normalized):
                if i in used or not h:
                    continue
                if any((h == s) if exact else (s in h.split() or h == s) for s in SYNONYMS[field]):
                    mapping[field] = i
                    used.add(i)
                    break
    return mapping


# ---------------------------------------------------------------- cleaning rows

def parse_int(text):
    s = text.replace(",", "").replace(" ", "")
    try:
        f = float(s)
    except ValueError:
        return None
    if f != int(f) or f <= 0:
        return None
    return int(f)


def parse_date(text, date_format):
    text = text.strip()
    if not text:
        return None
    candidates = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S"] + DATE_FORMATS.get(date_format, [])
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_rows(rows, mapping, date_format):
    """Turn raw rows into records. Returns (records, skipped) where skipped is [(row, reason)]."""
    records, skipped = [], []

    def get(row, field):
        i = mapping.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    for n, row in enumerate(rows, start=2):  # row 1 is the header
        job = get(row, "job")
        if not job:
            skipped.append((n, "No job"))
            continue
        line = get(row, "line")
        warnings = []

        quantity = None
        quantity_text = get(row, "quantity")
        if quantity_text:
            quantity = parse_int(quantity_text)
            if quantity is None:
                if not line:
                    skipped.append((n, f"Quantity not understood ({quantity_text})"))
                    continue
                warnings.append(f"quantity not understood ({quantity_text})")
        elif not line:
            skipped.append((n, "No quantity"))
            continue

        dates = {}
        for field in ("due_date", "assigned_date"):
            text = get(row, field)
            dates[field] = parse_date(text, date_format)
            if text and dates[field] is None:
                warnings.append(f"{field.replace('_', ' ')} not understood ({text})")

        records.append(
            {
                "row": n,
                "job": job,
                "line": line,
                "quantity": quantity,
                "part_number": get(row, "part_number").upper(),
                "due_date": dates["due_date"],
                "assigned_date": dates["assigned_date"],
                "project": get(row, "project"),
                "warnings": warnings,
            }
        )
    return records, skipped


# ---------------------------------------------------------------- proposed jobs

def default_label(lines):
    return lines[0] if len(lines) == 1 else f"{lines[0]}-{lines[-1]}"


def build_proposals(records, groups, labels):
    """groups: {row number: group id} for lines the user combined.
    labels: {(job lowercased, group id): label the user typed}.
    Returns a list of proposed jobs."""
    proposals = []
    combined = {}
    for rec in records:
        base = {
            "job": rec["job"],
            "due_date": rec["due_date"],
            "assigned_date": rec["assigned_date"],
            "project": rec["project"] or None,
        }
        if not rec["line"]:
            proposals.append(
                dict(base, kind="single", label=None, quantity=rec["quantity"],
                     part_number=rec["part_number"] or None, rows=[rec["row"]], lines=[])
            )
            continue
        group = groups.get(rec["row"], "")
        if not group:
            proposals.append(
                dict(base, kind="big", label=rec["line"], quantity=rec["quantity"],
                     part_number=None, rows=[rec["row"]], lines=[rec["line"]])
            )
            continue
        key = (rec["job"].lower(), group)
        p = combined.get(key)
        if p is None:
            p = dict(base, kind="big", label=None, quantity=None, part_number=None,
                     rows=[], lines=[], group=group)
            combined[key] = p
            proposals.append(p)
        p["rows"].append(rec["row"])
        p["lines"].append(rec["line"])
        if rec["quantity"] is not None:
            p["quantity"] = (p["quantity"] or 0) + rec["quantity"]
        if rec["due_date"] and (p["due_date"] is None or rec["due_date"] < p["due_date"]):
            p["due_date"] = rec["due_date"]
        p["assigned_date"] = p["assigned_date"] or rec["assigned_date"]
        p["project"] = p["project"] or (rec["project"] or None)
    for (job_key, group), p in combined.items():
        typed = labels.get((job_key, group), "").strip()
        p["label"] = typed or default_label(p["lines"])
    return proposals


def proposal_key(p):
    return "||".join([p["job"].lower(), (p["label"] or "").lower(), (p["part_number"] or "").lower()])


# ---------------------------------------------------------------- keeping an upload between steps

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{6,40}$")


def new_token():
    return secrets.token_urlsafe(9)


def _path(directory, token):
    if not _TOKEN.match(token or ""):
        return None
    return os.path.join(directory, f"{token}.json")


def save_state(directory, token, state):
    os.makedirs(directory, exist_ok=True)
    with open(_path(directory, token), "w", encoding="utf-8") as f:
        json.dump(state, f)


def load_state(directory, token):
    path = _path(directory, token)
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def delete_state(directory, token):
    path = _path(directory, token)
    if path and os.path.exists(path):
        os.remove(path)


def cleanup_old(directory, max_age_seconds=2 * 24 * 3600):
    if not os.path.isdir(directory):
        return
    cutoff = time.time() - max_age_seconds
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if name.endswith(".json") and os.path.getmtime(path) < cutoff:
            os.remove(path)
