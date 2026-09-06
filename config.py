"""
Config loader — feature toggles + limits + LLM settings.

All risky behavior is gated behind config.yaml toggles (default OFF).
`enabled()` is the single gate every module must consult.
"""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_FILE = Path(__file__).parent / "config.yaml"

DEFAULTS: dict = {
    "toggles": {
        "dry_run": True,
        "resume_tailoring": False,
        "auto_apply": False,
        "auto_submit": False,
        "linkedin_collect": False,
        "linkedin_drafts": False,
        "linkedin_autosend": False,
        "slate_collect": False,
        "slate_style": False,
    },
    "limits": {
        "autosend_daily_cap": 10,
        "autosend_min_delay_min": 5,
        "autosend_max_delay_min": 15,
        "autosend_max_per_recruiter": 1,
    },
    "llm": {
        "base_url": "http://localhost:1234/v1",
        "model": "local-model",
        "temperature": 0.4,
    },
    "resume": {
        "master": "resume/master_resume.md",
        "out_dir": "data/resumes",
    },
}

# Dependency ladder: a toggle can't be on unless these are on too.
_REQUIRES = {
    "auto_apply": ["resume_tailoring"],
    "auto_submit": ["auto_apply"],
    "linkedin_drafts": ["linkedin_collect"],
    "linkedin_autosend": ["linkedin_drafts"],
    "slate_style": ["slate_collect"],
}


@lru_cache(maxsize=1)
def _load() -> dict:
    data = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open() as f:
            data = yaml.safe_load(f) or {}

    merged = {}
    for section, defaults in DEFAULTS.items():
        merged[section] = {**defaults, **(data.get(section) or {})}
    return merged


def section(name: str) -> dict:
    return dict(_load().get(name, {}))


def enabled(toggle: str) -> bool:
    """
    Returns True only if `toggle` is on AND its prerequisites are on.
    dry_run short-circuits nothing here — modules check it themselves
    because dry_run means "log, don't act", not "off".
    """
    if not _load()["toggles"].get(toggle, False):
        return False
    for prereq in _REQUIRES.get(toggle, []):
        if not _load()["toggles"].get(prereq, False):
            return False
    return True


def dry_run() -> bool:
    return bool(_load()["toggles"].get("dry_run", True))


def limit(key: str):
    return _load()["limits"].get(key)
