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
import asyncio
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
    if not config.enabled("linkedin_autosend"):
        print("[outreach] linkedin_autosend is OFF — enable it in config.yaml")
        return
    if config.dry_run():
        print("[outreach] DRY RUN — nothing will be sent. "
              "Set toggles.dry_run=false to go live.")
        return

    from scrapers.linkedin import PROFILE_DIR
    from playwright.async_api import async_playwright

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

    async def _run():
        async with async_playwright() as p:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport=None, locale="en-CA",
                timezone_id="America/Toronto",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            from storage.safari_cookies import matching
            imported = matching("linkedin")
            if imported:
                await context.add_cookies(imported)
                print(f"[outreach] Injected {len(imported)} Safari cookie(s).")

            page = context.pages[0] if context.pages else await context.new_page()

            sent = 0
            for r_id, r in queue[:remaining]:
                lo = int(config.limit("autosend_min_delay_min"))
                hi = int(config.limit("autosend_max_delay_min"))
                if sent > 0:
                    wait_min = random.uniform(lo, hi)
                    print(f"[outreach] Waiting {wait_min:.1f} min "
                          f"(human pacing)...")
                    await asyncio.sleep(wait_min * 60)

                ok = await _send_one(page, r)
                if ok:
                    mark_sent(r_id)
                    sent += 1
                    print(f"[outreach] SENT #{sent}: {r['name']}")

            input("\n[outreach] Session done. Press Enter to close browser... ")
            await context.close()

    asyncio.run(_run())


async def _send_one(page, r: dict) -> bool:
    """
    Opens profile → Message → fills → sends.
    Any failure aborts THAT message only (never retries blindly).
    """
    try:
        await page.goto(r["profile_url"], wait_until="domcontentloaded",
                        timeout=45000)
        await asyncio.sleep(random.uniform(3, 6))

        msg_btn = page.locator(
            "main button:has-text('Message'), "
            "button[aria-label*='Message']"
        ).first
        await msg_btn.click(timeout=10000)
        await asyncio.sleep(random.uniform(2, 4))

        box = page.locator(
            "div.msg-form__contenteditable[role='textbox'], "
            "[contenteditable='true'].msg-form__contenteditable"
        ).first
        await box.click(timeout=10000)
        await box.type(r["draft"], delay=random.randint(40, 90))
        await asyncio.sleep(random.uniform(2, 4))

        send_btn = page.locator(
            "button.msg-form__send-button:not([disabled]), "
            "button[aria-label*='Send now']"
        ).first
        await send_btn.click(timeout=10000)
        await asyncio.sleep(random.uniform(3, 5))
        return True
    except Exception as e:
        print(f"[outreach] FAILED for {r['name']}: {e} "
              f"(not retried — handle manually)")
        return False
