"""
Per-job resume tailoring.

Reads the master Markdown resume, asks the local LLM (LM Studio,
OpenAI-compatible API) to re-aim summary/skills at the specific job
posting, writes data/resumes/<job_id>.md + .pdf.

Rules baked into the prompt: NEVER fabricate experience, skills, or
dates — only reorder, reword, and emphasize what's already true.
If the LLM is unreachable, falls back to the master resume unchanged
(the pipeline keeps working, just untailored).
"""
from __future__ import annotations
import json
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

import config

ROOT = Path(__file__).parent.parent

SYSTEM_PROMPT = """\
You are a resume editor. You will receive a master resume in Markdown \
and a job posting. Rewrite the resume to target that posting:
- Reorder and reword bullet points so the most relevant experience leads.
- Adjust the professional summary to echo the posting's key requirements.
- Reorder the skills section, most relevant first.
- Mirror important keywords from the posting where they truthfully apply.
- PROJECT MATCHING: the master resume lists GitHub projects with URLs.
  Order projects so the most relevant to the posting comes first
  (e.g. Kotlin/Android work for mobile roles, .NET/C# for backend roles,
  Python/data work for data roles). Keep every project's GitHub URL —
  they are the proof. Never drop a project, only reorder.
HARD RULES:
- Never invent experience, employers, dates, degrees, or skills.
- Never invent project URLs or attach a URL to the wrong project.
- Never remove factual content; only reorder/reword/emphasize.
- Keep the same overall Markdown structure (name header, sections).
- Output ONLY the resume Markdown, no commentary."""


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
        print(f"[tailor] LLM unavailable ({e}); using master resume as-is.")
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
        print(f"[tailor] PDF render failed: {e}")
        return None


def tailor_resume(job) -> Path | None:
    """
    Returns path to the tailored PDF for this job, or None.
    dry_run: computes nothing, prints what would happen, returns None.
    """
    if config.dry_run():
        print(f"[tailor] DRY RUN — would tailor resume for {job.id} "
              f"({job.title} @ {job.company})")
        return None

    cfg = config.section("resume")
    master = ROOT / cfg["master"]
    if not master.exists():
        print(f"[tailor] Master resume missing: {master}")
        return None

    out_dir = ROOT / cfg["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    md_out = out_dir / f"{job.id}.md"

    if md_out.exists():  # already tailored for this job
        pdf = md_out.with_suffix(".pdf")
        if pdf.exists():
            return pdf

    description = job.description or f"{job.title} at {job.company}"
    prompt = (
        f"# JOB POSTING\n{job.title} — {job.company} "
        f"({job.location or 'location n/a'})\n\n{description}\n\n"
        f"# MASTER RESUME\n{master.read_text()}"
    )

    voice_path = ROOT / "data" / "writing" / "style.md"
    if voice_path.exists():
        try:
            voice = voice_path.read_text().strip()
        except OSError:
            voice = ""
        if voice:
            prompt += f"\n\n# VOICE (match this writing style)\n{voice}"

    tailored = _llm_chat(prompt)
    md_out.write_text(tailored if tailored else master.read_text())
    print(f"[tailor] Resume written: {md_out.name}"
          + (" (LLM-tailored)" if tailored else " (master copy)"))

    pdf = _render_pdf(md_out)
    if pdf:
        print(f"[tailor] PDF ready: {pdf.name}")
    return pdf


def latest_pdf_for(job_id: str) -> Path | None:
    """Existing tailored PDF for a job, if any."""
    pdf = ROOT / config.section("resume")["out_dir"] / f"{job_id}.pdf"
    return pdf if pdf.exists() else None
