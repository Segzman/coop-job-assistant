from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from models.job import Job, JobStatus

DATA_FILE = Path(__file__).parent.parent / "data" / "jobs.json"


def load_jobs() -> dict[str, Job]:
    """Returns {job_id: Job}. Empty dict if the file doesn't exist yet."""
    if not DATA_FILE.exists():
        return {}
    with DATA_FILE.open() as f:
        raw = json.load(f)
    return {jid: Job.from_dict(jdata) for jid, jdata in raw.items()}


def save_jobs(jobs: dict[str, Job]) -> None:
    DATA_FILE.parent.mkdir(exist_ok=True)
    with DATA_FILE.open("w") as f:
        json.dump({jid: j.to_dict() for jid, j in jobs.items()}, f, indent=2)


def merge_new_jobs(
    existing: dict[str, Job], fresh: list[Job]
) -> tuple[dict[str, Job], int]:
    """
    Merges freshly scraped jobs into the existing store.
    - Never overwrites status, notes, or date_applied on existing jobs.
    - Only adds genuinely new jobs.
    Returns (merged_dict, count_of_new_jobs).
    """
    new_count = 0
    for job in fresh:
        if job.id not in existing:
            existing[job.id] = job
            new_count += 1
    return existing, new_count


def update_status(
    job_id: str,
    status: JobStatus,
    notes: str | None = None,
) -> None:
    jobs = load_jobs()
    if job_id not in jobs:
        raise KeyError(f"Job '{job_id}' not found in store.")
    jobs[job_id].status = status
    if status == "applied":
        jobs[job_id].date_applied = datetime.now().isoformat(timespec="seconds")
    if notes is not None:
        jobs[job_id].notes = notes
    save_jobs(jobs)


def update_external_url(job_id: str, url: str) -> None:
    """Attach (or clear) an external application URL for a job."""
    jobs = load_jobs()
    if job_id not in jobs:
        raise KeyError(f"Job '{job_id}' not found in store.")
    jobs[job_id].external_url = url or None
    save_jobs(jobs)


def filter_jobs(
    jobs: dict[str, Job],
    status: str | None = None,
    platform: str | None = None,
) -> list[Job]:
    result = list(jobs.values())
    if status and status != "all":
        result = [j for j in result if j.status == status]
    if platform:
        result = [j for j in result if j.platform == platform]
    # Sort: deadline ascending (None goes last), then date_found descending
    result.sort(key=lambda j: (j.deadline or "9999-12-31", j.date_found))
    return result
