from __future__ import annotations
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Literal

JobStatus = Literal["new", "seen", "applied", "skipped"]


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    platform: Literal["sheridan", "indeed"]
    url: str
    date_found: str          # ISO 8601 e.g. "2026-02-26T19:04:00"
    status: JobStatus = "new"

    deadline: str | None = None      # "2026-03-15" or None
    description: str | None = None   # First 500 chars only
    salary: str | None = None
    job_type: str | None = None
    date_applied: str | None = None
    notes: str | None = None
    external_url: str | None = None  # External apply link (e.g. Workday, Greenhouse)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        known = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def make_job_id(platform: str, url: str) -> str:
    """Deterministic 12-char ID from platform + URL."""
    return hashlib.sha256(f"{platform}:{url}".encode()).hexdigest()[:12]
