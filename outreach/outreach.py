"""
Recruiter outreach — drafts + gated autosend + gated connect invites.

drafts():   LLM-personalized message + connect note per recruiter.
            Runs even in dry_run (purely local files, made for review).
autosend(): sends drafted messages via LinkedIn messaging. Gated behind
            toggles.linkedin_autosend AND not dry_run, daily-capped,
            5–15 min randomized gaps, one message max per recruiter
            ever. Asks for a typed confirmation before the first send
            of each session.
connect():  sends connection invites with the drafted note. Gated behind
            toggles.linkedin_connect, its own daily cap (LinkedIn
            restricts invites — default 5/day), one invite per recruiter
            ever, same typed confirmation.
"""
from __future__ import annotations
import json
import random
import urllib.request
from datetime import datetime, date
from pathlib import Path
from typing import TYPE_CHECKING

import config
from storage.recruiters import load_recruiters, save_recruiters, sent_today, mark_sent

VOICE_PATH = Path(__file__).parent.parent / "data" / "writing" / "style.md"
CONNECT_NOTE_MAX = 280

if TYPE_CHECKING:
    from models.job import Job

SYSTEM_PROMPT = """\
You write short LinkedIn outreach from a college student seeking a
co-op placement. Two formats (follow the requested one exactly):

MESSAGE (exactly ONE message: 2 sentences, then sign-off block):
- Sentence 1: mention applying to the specific role at their company
  and one genuine, specific point of fit.
- Sentence 2: a low-pressure ask (15-min chat or being kept in mind).
- Then a blank line, "Kind regards,", "Sekun" on their own lines.

CONNECT NOTE (STRICTLY under 240 characters, no greeting issues):
- "Hi {FirstName}," + who you are + the role you applied for + one
  fit point. No question, no ask, NO sign-off.

STYLE (match the candidate's voice):
- Warm peer-review tone: specific compliment first, direct second.
- Short sentences, plain words, contractions OK ("I'm", "I'd").
- Sign-off for MESSAGE: "Kind regards," on its own line, then "Sekun".
  No sign-off for CONNECT NOTE.
HARD RULES:
- Zero fluff, no emojis, no "I hope this finds you well".
- If no applied role is given in context, say only "a co-op placement"
  at the company. NEVER invent a role title, courses, or skills.
- NEVER use em-dashes (—) or en-dashes (–).
- NEVER use: leverage, delve, cutting-edge, tapestry, landscape,
  realm, pivotal, seamless, robust, crucial, vibrant, foster,
  holistic, synergy, testament, utilize, meticulous, detail-oriented,
  fast-paced, ever-evolving, game-changer.
- Output ONLY the message text."""


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
            f"at {recruiter['company']}.\n"
            f"NO APPLIED ROLE ON FILE — refer only to 'a co-op placement'.")


def _voice() -> str:
    try:
        voice = VOICE_PATH.read_text().strip()
    except OSError:
        voice = ""
    return f"\n\nVOICE (match this writing style):\n{voice}" if voice else ""


def _fit_note(note: str) -> str:
    """Strip sign-offs, hard-enforce the character limit at a boundary."""
    import re
    note = re.sub(r"\n*\s*Kind regards,?\s*\n\s*\S+.*$", "", note.strip(),
                  flags=re.IGNORECASE | re.DOTALL).strip()
    # keep first message only if the model emitted variants
    note = re.split(r"\n\s*\n", note)[0].strip()
    if len(note) <= CONNECT_NOTE_MAX:
        return note
    cut = note[:CONNECT_NOTE_MAX].rsplit(" ", 1)[0]
    return cut + "…"


def drafts(jobs: dict[str, "Job"], force: bool = False) -> int:
    """Generate message + connect-note drafts for recruiters missing one."""
    if not config.enabled("linkedin_drafts"):
        print("[outreach] linkedin_drafts is OFF — enable it in config.yaml")
        return 0

    pending = {
        r_id: r for r_id, r in load_recruiters().items()
        if force or not r.get("draft") or not r.get("connect_note")
    }
    if not pending:
        print("[outreach] No recruiters need drafts.")
        return 0

    print(f"[outreach] Drafting {len(pending)} recruiter(s)"
          + (" [DRY RUN]" if config.dry_run() else ""))
    count = 0
    voice = _voice()
    all_recruiters = load_recruiters()
    for r_id, r in pending.items():
        ctx = _context_for(r, jobs) + voice
        if force or not r.get("draft"):
            msg = _llm(ctx + "\n\nFORMAT: MESSAGE")
            if msg:
                print(f"\n--- {r['name']} ({r.get('company')}) ---\n{msg}")
                all_recruiters[r_id]["draft"] = msg
                all_recruiters[r_id]["draft_date"] = datetime.now().isoformat(timespec="seconds")
        if force or not r.get("connect_note"):
            note = _llm(ctx + "\n\nFORMAT: CONNECT NOTE")
            if note:
                note = _fit_note(note)
                print(f"\n--- {r['name']} (connect, {len(note)}ch) ---\n{note}")
                all_recruiters[r_id]["connect_note"] = note
                all_recruiters[r_id]["connect_note_date"] = datetime.now().isoformat(timespec="seconds")
        count += 1
    save_recruiters(all_recruiters)
    print(f"\n[outreach] Saved draft(s) for {count} recruiter(s). Review: python main.py outreach-list")
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


def connect() -> None:
    """Send connection invites with the drafted note. Separately capped."""
    if not config.enabled("linkedin_connect"):
        print("[outreach] linkedin_connect is OFF — enable it in config.yaml")
        return
    if config.dry_run():
        print("[outreach] DRY RUN — nothing will be sent. "
              "Set toggles.dry_run=false to go live.")
        return

    import time
    from browser import safari_bridge as safari
    from storage.recruiters import invites_today, mark_invited

    cap = int(config.limit("connect_daily_cap"))
    remaining = cap - invites_today()
    if remaining <= 0:
        print(f"[outreach] Connect cap reached ({cap}/day). Tomorrow.")
        return

    queue = [
        (r_id, r) for r_id, r in load_recruiters().items()
        if r.get("connect_note") and not r.get("invite_date")
        and not r.get("sent_date")
    ]
    if not queue:
        print("[outreach] Nothing queued (no unsent connect notes).")
        return

    print(f"[outreach] {len(queue)} invite(s) queued, "
          f"{remaining} left under today's cap of {cap}.")
    answer = input("Type CONNECT to start inviting: ").strip()
    if answer != "CONNECT":
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
            if sent > 0:
                wait_min = random.uniform(3, 8)
                print(f"[outreach] Waiting {wait_min:.1f} min...")
                time.sleep(wait_min * 60)
            if _connect_one(wid, r):
                mark_invited(r_id)
                sent += 1
                print(f"[outreach] INVITED #{sent}: {r['name']}")
    finally:
        input("\n[outreach] Session done. Press Enter to close Safari... ")
        safari.close_window(wid)


def _connect_one(wid: int, r: dict) -> bool:
    """
    Opens profile → Connect → Add a note → pastes note → Send.
    Already-connected profiles (Message button, no Connect) are skipped
    as already-won, not failures.
    """
    import time
    from browser import safari_bridge as safari

    note_js = json.dumps(_fit_note(r["connect_note"]))
    try:
        safari.goto(wid, r["profile_url"])
        time.sleep(random.uniform(4, 7))

        state = safari.js(wid, """(() => {
          const btns = [...document.querySelectorAll("main button")];
          if (btns.some(b => /message/i.test(b.innerText || "")))
            return "already-connected";
          const more = btns.find(b => /more/i.test(b.innerText || ""));
          if (more) more.click();
          return "more-clicked";
        })()""").strip().strip('"')
        if state == "already-connected":
            print(f"[outreach] Already connected with {r['name']} — "
                  f"message them instead.")
            return False
        time.sleep(random.uniform(2, 3))

        clicked = safari.js(wid, """(() => {
          const b = [...document.querySelectorAll(
            "button, div[role='button']")]
            .find(e => /^connect$/i.test((e.innerText || "").trim()) &&
              e.offsetParent !== null);
          if (!b) return "miss";
          b.click();
          return "clicked";
        })()""").strip().strip('"')
        if clicked != "clicked":
            print(f"[outreach] No Connect button for {r['name']}.")
            return False
        time.sleep(random.uniform(2, 3))

        noted = safari.js(wid, """(() => {
          const b = [...document.querySelectorAll("button")]
            .find(e => /add a note/i.test(e.innerText || ""));
          if (!b) return "miss";
          b.click();
          return "clicked";
        })()""").strip().strip('"')
        if noted != "clicked":
            print(f"[outreach] No Add-a-note for {r['name']} "
                  f"(invite without note? skipped — notes convert).")
            return False
        time.sleep(random.uniform(2, 3))

        filled = safari.js(wid, f"""(() => {{
          const box = document.querySelector(
            "textarea[name='message'], textarea[id*='note']");
          if (!box) return "miss";
          box.focus();
          box.value = {note_js};
          box.dispatchEvent(new Event("input", {{bubbles: true}}));
          return "done";
        }})()""").strip().strip('"')
        if filled != "done":
            print(f"[outreach] Note box missing for {r['name']}.")
            return False

        sent = safari.js(wid, """(() => {
          const b = [...document.querySelectorAll("button")]
            .find(e => /^send$/i.test((e.innerText || "").trim()) &&
              !e.disabled);
          if (!b) return "miss";
          b.click();
          return "sent";
        })()""").strip().strip('"')
        if sent != "sent":
            print(f"[outreach] Send missing for {r['name']}.")
            return False
        time.sleep(random.uniform(3, 5))
        return True
    except RuntimeError:
        print(f"[outreach] Safari window closed during {r['name']}.")
        return False
    except Exception as e:
        print(f"[outreach] FAILED for {r['name']}: {e}")
        return False
