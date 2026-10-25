from flask_login import current_user

from . import db
from .models import AuditEvent


def log(summary, job_id=None, pallet_id=None, part_id=None, target_user_id=None):
    """Record who did what. Added to the current session, so it is saved (or not)
    together with the change it describes."""
    who = current_user if current_user.is_authenticated else None
    db.session.add(
        AuditEvent(
            user_id=who.id if who else None,
            username=who.username if who else "(unknown)",
            summary=summary[:500],
            job_id=job_id,
            pallet_id=pallet_id,
            part_id=part_id,
            target_user_id=target_user_id,
        )
    )
