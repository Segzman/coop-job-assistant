"""
Voice distillation for SLATE writing backups.

Reads data/writing/ (posts.md files + saved submission text), sends a
truncated bundle (cap ~12k chars) to the local LLM (same urllib pattern
as tailoring/resume.py, model from config.section("llm")), and writes
data/writing/style.md: diction, sentence rhythm/length, tone,
opener/closer habits, things to avoid.

Pure local files — the only network call is localhost LLM. No sends.

Toggle: config.yaml -> toggles.slate_style (requires slate_collect).
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import config

ROOT = Path(__file__).parent.parent
WRITING_DIR = ROOT / "data" / "writing"
STYLE_PATH = WRITING_DIR / "style.md"
MAX_CHARS = 12000

SYSTEM_PROMPT = """\
You are a writing-style analyst. You will receive samples of one student's
own coursework (essays, assignment submissions, discussion posts).
Distill HOW they write — never summarize WHAT they wrote about.
Output Markdown with exactly these sections:
## Diction
## Sentence rhythm / length
## Tone
## Opener habits
## Closer habits
## Things to avoid (what would sound unlike them)
Be concrete: quote short illustrative phrases where useful. No commentary
outside these sections."""


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
        print(f"[slate-style] LLM unavailable ({e}); style.md not written.")
        return None


def _gather_writing() -> str:
    """All collected text, oldest first, truncated to MAX_CHARS."""
    if not WRITING_DIR.exists():
        return ""
    chunks: list[str] = []
    paths = sorted(
        [p for p in WRITING_DIR.rglob("*")
         if p.is_file() and p.suffix.lower() in (".md", ".txt")
         and p.name != STYLE_PATH.name],
    )
    for path in paths:
        try:
            text = path.read_text().strip()
        except OSError:
            continue
        if text:
            chunks.append(f"\n\n===== {path.name} =====\n{text}")
    return "".join(chunks)[:MAX_CHARS]


def distill_style() -> Path | None:
    """Writes data/writing/style.md. Returns its path, or None."""
    if not config.enabled("slate_style"):
        print("[slate-style] slate_style is OFF (needs slate_collect too) "
              "— enable in config.yaml")
        return None

    if config.dry_run():
        print("[slate-style] DRY RUN — would distill data/writing/ "
              "into data/writing/style.md via local LLM")
        return None

    corpus = _gather_writing()
    if not corpus.strip():
        print("[slate-style] No writing found in data/writing/ — "
              "run: python main.py slate-collect")
        return None

    print(f"[slate-style] Distilling {len(corpus)} chars of writing...")
    style = _llm_chat(
        "Here are the student's writing samples:\n" + corpus)
    if not style:
        return None

    WRITING_DIR.mkdir(parents=True, exist_ok=True)
    STYLE_PATH.write_text(style.strip() + "\n")
    print(f"[slate-style] Voice profile written: {STYLE_PATH}")
    return STYLE_PATH


if __name__ == "__main__":
    distill_style()
