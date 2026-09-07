"""
Recruiter tracker storage — data/recruiters.json
"""
from __future__ import annotations
import json
from datetime import datetime, date
from pathlib import Path

DATA_FILE = Path(__file__).parent.parent / "data" / "recruiters.json"


def load_recruiters() -> dict[str, dict]:
    if not DATA_FILE.exists():
        return {}
    with DATA_FILE.open() as f:
        return json.load(f)


def save_recruiters(recruiters: dict[str, dict]) -> None:
    DATA_FILE.parent.mkdir(exist_ok=True)
    with DATA_FILE.open("w") as f:
        json.dump(recruiters, f, indent=2)


def upsert(r_id: str, fields: dict) -> bool:
    """Merge fields into one recruiter record. Returns True if new."""
    recruiters = load_recruiters()
    is_new = r_id not in recruiters
    rec = recruiters.setdefault(r_id, {})
    rec.update(fields)
    save_recruiters(recruiters)
    return is_new


def sent_today() -> int:
    """Count of messages sent today (for the daily cap)."""
    today = date.today().isoformat()
    return sum(
        1 for r in load_recruiters().values()
        if (r.get("sent_date") or "") == today
    )


def mark_sent(r_id: str) -> None:
    recruiters = load_recruiters()
    if r_id in recruiters:
        recruiters[r_id]["sent_date"] = date.today().isoformat()
        recruiters[r_id]["sent_at"] = datetime.now().isoformat(timespec="seconds")
        save_recruiters(recruiters)


def invites_today() -> int:
    """Count of connection invites sent today (separate cap)."""
    today = date.today().isoformat()
    return sum(
        1 for r in load_recruiters().values()
        if (r.get("invite_date") or "") == today
    )


def mark_invited(r_id: str) -> None:
    recruiters = load_recruiters()
    if r_id in recruiters:
        recruiters[r_id]["invite_date"] = date.today().isoformat()
        save_recruiters(recruiters)
