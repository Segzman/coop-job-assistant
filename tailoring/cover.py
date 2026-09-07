"""
Per-job cover letters.

Mirrors tailoring/resume.py: master template + job posting → local LLM
rewrite → data/cover_letters/<job_id>.md + .pdf. Voice-aware (style.md),
never fabricates.
"""
from __future__ import annotations
import json
import subprocess
import urllib.request
from datetime import date
from pathlib import Path

import config

ROOT = Path(__file__).parent.parent

SYSTEM_PROMPT = """\
You are a cover-letter writer. You will receive a master cover letter
template (with {{PLACEHOLDERS}}), a job posting, and the candidate's
resume. Write a complete cover letter for THIS job:
- Fill every {{PLACEHOLDER}} (today's date, company, role).
- Para 1: genuine motivation tied to the specific company/role.
- Para 2: fittest experience + GitHub projects (keep URLs), skills
  mirrored from the posting where truthful.
- Para 3: enthusiasm, availability, contact, thanks. Sign as the candidate.
- Match the attached VOICE profile if present.
HARD RULES:
- Never invent experience, employers, dates, degrees, or skills.
- Never invent project URLs. Keep header contact lines factual.
- NEVER emit placeholders like {{DATE}} — fill every field. Use the
  TODAY date supplied in the prompt.
- NEVER use em-dashes (—) or en-dashes (–); use commas or colons.
- NEVER use these words: leverage, delve, cutting-edge, tapestry,
  landscape, realm, pivotal, seamless, robust, crucial, vibrant, foster,
  holistic, synergy, testament, delve, utilize, meticulous,
  detail-oriented, fast-paced, ever-evolving, game-changer.
- Prefer short concrete sentences over adjectives. State facts
  (built X with Y, N months) instead of claims (proven expertise in Z).
- Output ONLY the letter in Markdown, no commentary."""


def _llm_chat(prompt: str) -> str | None:
    cfg = config.section("llm")
    body = json.dumps({
        "model": cfg["model"],
        "temperature": float(cfg.get("temperature", 0.4)),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(
        f"{cfg['base_url']}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.load(resp)
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[cover] LLM unavailable ({e}); skipping cover letter.")
        return None


def _render_pdf(md_path: Path) -> Path | None:
    pdf_path = md_path.with_suffix(".pdf")
    try:
        subprocess.run(
            ["pandoc", str(md_path), "-o", str(pdf_path),
             "--pdf-engine=weasyprint"],
            check=True, capture_output=True,
        )
        return pdf_path
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"[cover] PDF render failed: {e}")
        return None


def cover_letter(job, resume_md: str = "") -> Path | None:
    """
    Returns path to the cover PDF for this job, or None.
    dry_run: prints what would happen, returns None.
    """
    if config.dry_run():
        print(f"[cover] DRY RUN — would write cover for {job.id}")
        return None

    master = ROOT / "resume" / "cover_master.md"
    if not master.exists():
        print("[cover] Master cover missing: resume/cover_master.md")
        return None

    out_dir = ROOT / "data" / "cover_letters"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_out = out_dir / f"{job.id}.md"

    if md_out.exists():
        pdf = md_out.with_suffix(".pdf")
        if pdf.exists():
            return pdf

    description = job.description or f"{job.title} at {job.company}"
    prompt = (
        f"# TODAY\n{date.today().isoformat()}\n\n"
        f"# JOB POSTING\n{job.title} — {job.company} "
        f"({job.location or 'location n/a'})\n\n{description}\n\n"
        f"# RESUME\n{resume_md}\n\n"
        f"# MASTER COVER TEMPLATE\n{master.read_text()}"
    )
    voice_path = ROOT / "data" / "writing" / "style.md"
    if voice_path.exists():
        try:
            voice = voice_path.read_text().strip()
        except OSError:
            voice = ""
        if voice:
            prompt += f"\n\n# VOICE (match this writing style)\n{voice}"

    letter = _llm_chat(prompt)
    if not letter:
        return None
    md_out.write_text(letter)
    print(f"[cover] Letter written: {md_out.name}")

    pdf = _render_pdf(md_out)
    if pdf:
        print(f"[cover] PDF ready: {pdf.name}")
    return pdf


def latest_pdf_for(job_id: str) -> Path | None:
    """Existing cover PDF for a job, if any."""
    pdf = ROOT / "data" / "cover_letters" / f"{job_id}.pdf"
    return pdf if pdf.exists() else None
