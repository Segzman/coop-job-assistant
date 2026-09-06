"""
Recruiter outreach — drafts + gated autosend.

drafts():   LLM-personalized short message per recruiter, saved locally.
            Runs even in dry_run (purely local files, made for review).
autosend(): actually sends via LinkedIn messaging. Gated behind
            toggles.linkedin_autosend AND not dry_run, daily-capped,
            5–15 min randomized gaps, one message max per recruiter
            ever. Asks for a typed confirmation before the first send
            of each session.
"""
from __future__ import annotations
import json
import random
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING

import config
from storage.recruiters import load_recruiters, save_recruiters, sent_today, mark_sent

if TYPE_CHECKING:
    from models.job import Job

SYSTEM_PROMPT = """\
You write short LinkedIn outreach messages from a college student \
seeking a co-op placement. Write a 2-sentence message:
- Sentence 1: mention applying to the specific role at their company \
and one genuine, specific point of fit.
- Sentence 2: a low-pressure ask (15-min chat or being kept in mind).
Tone: warm, direct, zero fluff, no emojis, no "I hope this finds you \
well". Sign-off is NOT needed. Output ONLY the message text."""


def _llm(prompt: str) -> str | None:
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[outreach] LLM unavailable ({e})")
        return None


def _context_for(recruiter: dict, jobs: dict[str, "Job"]) -> str:
    """Applied job at this recruiter's company, for personalization."""
    matches = [
        j for j in jobs.values()
        if j.company == recruiter.get("company") and j.status == "applied"
    ]
    if matches:
        j = matches[0]
        return (f"Recruiter: {recruiter['name']}, {recruiter.get('title', '')} "
                f"at {recruiter['company']}.\nApplied role: {j.title} "
                f"(job id {j.id}). Student: software development co-op "
                f"student at Sheridan College.")
    return (f"Recruiter: {recruiter['name']}, {recruiter.get('title', '')} "
            f"at {recruiter['company']}.")


def drafts(jobs: dict[str, "Job"], force: bool = False) -> int:
    """Generate drafts for recruiters missing one. Returns count."""
    if not config.enabled("linkedin_drafts"):
        print("[outreach] linkedin_drafts is OFF — enable it in config.yaml")
        return 0

    pending = {
        r_id: r for r_id, r in load_recruiters().items()
        if force or not r.get("draft")
    }
    if not pending:
        print("[outreach] No recruiters need drafts.")
        return 0

    print(f"[outreach] Drafting {len(pending)} message(s)"
          + (" [DRY RUN]" if config.dry_run() else ""))
    count = 0
    all_recruiters = load_recruiters()
    for r_id, r in pending.items():
        msg = _llm(_context_for(r, jobs))
        if not msg:
            continue
        print(f"\n--- {r['name']} ({r.get('company')}) ---\n{msg}")
        # Drafts are local review artifacts — saved even in dry_run.
        all_recruiters[r_id]["draft"] = msg
        all_recruiters[r_id]["draft_date"] = datetime.now().isoformat(timespec="seconds")
        count += 1
    save_recruiters(all_recruiters)
    print(f"\n[outreach] Saved {count} draft(s). Review: python main.py outreach-list")
    return count


def autosend() -> None:
    """Send queued drafts through real Safari (native stack, no Playwright)."""
    if not config.enabled("linkedin_autosend"):
        print("[outreach] linkedin_autosend is OFF — enable it in config.yaml")
        return
    if config.dry_run():
        print("[outreach] DRY RUN — nothing will be sent. "
              "Set toggles.dry_run=false to go live.")
        return

    import time
    from browser import safari_bridge as safari

    cap = int(config.limit("autosend_daily_cap"))
    remaining = cap - sent_today()
    if remaining <= 0:
        print(f"[outreach] Daily cap reached ({cap}). Come back tomorrow.")
        return

    queue = [
        (r_id, r) for r_id, r in load_recruiters().items()
        if r.get("draft") and not r.get("sent_date")
    ]
    if not queue:
        print("[outreach] Nothing queued (no unsent drafts).")
        return

    print(f"[outreach] {len(queue)} message(s) queued, "
          f"{remaining} left under today's cap of {cap}.")
    answer = input("Type SEND to start this session's autosend: ").strip()
    if answer != "SEND":
        print("[outreach] Cancelled.")
        return

    wid = safari.open_window("https://www.linkedin.com/feed/")
    time.sleep(4)
    try:
        try:
            url = safari.current_url(wid)
        except RuntimeError:
            url = ""
        if "/login" in url or "checkpoint" in url:
            input("[outreach] LinkedIn wants login. Sign in in the Safari "
                  "window, then press Enter... ")

        sent = 0
        for r_id, r in queue[:remaining]:
            lo = int(config.limit("autosend_min_delay_min"))
            hi = int(config.limit("autosend_max_delay_min"))
            if sent > 0:
                wait_min = random.uniform(lo, hi)
                print(f"[outreach] Waiting {wait_min:.1f} min "
                      f"(human pacing)...")
                time.sleep(wait_min * 60)

            if _send_one(wid, r):
                mark_sent(r_id)
                sent += 1
                print(f"[outreach] SENT #{sent}: {r['name']}")
    finally:
        input("\n[outreach] Session done. Press Enter to close Safari... ")
        safari.close_window(wid)


def _send_one(wid: int, r: dict) -> bool:
    """
    Opens profile → Message → fills → sends, all via Safari JS.
    Any failure aborts THAT message only (never retries blindly).
    """
    import time
    from browser import safari_bridge as safari

    draft_js = json.dumps(r["draft"])
    try:
        safari.goto(wid, r["profile_url"])
        time.sleep(random.uniform(4, 7))

        msg_clicked = safari.js(wid, """(() => {
          const b = [...document.querySelectorAll("main button, button")]
            .find(e => /message/i.test(e.innerText || "") ||
              /message/i.test(e.getAttribute("aria-label") || ""));
          if (!b || b.offsetParent === null) return "miss";
          b.click();
          return "clicked";
        })()""").strip().strip('"')
        if msg_clicked != "clicked":
            print(f"[outreach] No Message button for {r['name']}.")
            return False
        time.sleep(random.uniform(2, 4))

        filled = safari.js(wid, f"""(() => {{
          const box = document.querySelector(
            "div.msg-form__contenteditable[role='textbox']");
          if (!box) return "miss";
          box.focus();
          document.execCommand("selectAll", false, null);
          document.execCommand("insertText", false, {draft_js});
          box.dispatchEvent(new Event("input", {{bubbles: true}}));
          return "done";
        }})()""").strip().strip('"')
        if filled != "done":
            print(f"[outreach] Message box never opened for {r['name']}.")
            return False
        time.sleep(random.uniform(2, 4))

        sent = safari.js(wid, """(() => {
          const b = document.querySelector(
            "button.msg-form__send-button:not([disabled])");
          if (!b) return "miss";
          b.click();
          return "sent";
        })()""").strip().strip('"')
        if sent != "sent":
            print(f"[outreach] Send button not ready for {r['name']}.")
            return False
        time.sleep(random.uniform(3, 5))
        return True
    except RuntimeError:
        print(f"[outreach] Safari window closed during {r['name']}.")
        return False
    except Exception as e:
        print(f"[outreach] FAILED for {r['name']}: {e} "
              f"(not retried — handle manually)")
        return False
