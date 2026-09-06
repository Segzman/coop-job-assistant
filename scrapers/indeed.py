"""
Indeed Canada co-op / internship scraper — real Safari via AppleScript.

No Playwright, no separate profile, no automation flags: Safari brings
the user's genuine logged-in session and fingerprint, so Cloudflare and
Indeed bot walls stay down.

Bot-detection posture:
  - One status poll per 2 s (single JS round-trip, no traffic burst)
  - Random human delays between pages
  - If a verification page appears, pauses and lets YOU solve it in the
    visible window, then continues automatically
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

from browser import safari_bridge as safari
from models.job import Job, make_job_id
from scrapers.base import BaseScraper

INDEED_HOME = "https://ca.indeed.com"

# Greater Toronto Area, sorted by date, past 30 days
INDEED_SEARCH_URL = (
    "https://ca.indeed.com/jobs"
    "?q=software+engineer+OR+developer+intern+OR+co-op+summer+2026"
    "&l=Greater+Toronto+Area%2C+ON"
    "&radius=35"
    "&fromage=30"
    "&sort=date"
)

# One poll returns everything: card count, login state, block state.
STATUS_JS = """(() => {
  const t = (document.title || "").toLowerCase();
  const b = ((document.body && document.body.innerText) || "")
    .slice(0, 2000).toLowerCase();
  const blockedKw = ["captcha", "unusual traffic", "are you a human",
    "additional verification", "access denied"];
  return JSON.stringify({
    cards: document.querySelectorAll(
      "[data-testid='slider_item'], .job_seen_beacon").length,
    loggedOut: !!document.querySelector("a[href*='/account/login']"),
    blocked: t.indexOf("just a moment") !== -1 ||
      blockedKw.some(k => b.indexOf(k) !== -1),
  });
})()"""

EXTRACT_JS = """(() => {
  const cards = [...document.querySelectorAll(
    "[data-testid='slider_item'], .job_seen_beacon")];
  return JSON.stringify(cards.map(card => {
    const t = card.querySelector(
      "[data-testid='jobTitle'] span, h2.jobTitle span, .jcs-JobTitle span");
    const a = card.querySelector(
      "a[data-testid='job-title-link'], a[id^='job_'], " +
      "a.jcs-JobTitle, h2.jobTitle a");
    const c = card.querySelector(
      "[data-testid='company-name'], .companyName");
    const l = card.querySelector(
      "[data-testid='text-location'], .companyLocation");
    return {
      title: t ? t.innerText.trim() : "",
      href: a ? (a.getAttribute("href") || "") : "",
      company: c ? c.innerText.trim() : "Unknown",
      location: l ? l.innerText.trim() : "Greater Toronto Area, ON",
    };
  }));
})()"""

NEXT_JS = """(() => {
  const n = document.querySelector(
    "a[data-testid='pagination-page-next'], a[aria-label='Next Page']");
  if (!n) return "none";
  n.scrollIntoView({block: "center"});
  n.click();
  return "clicked";
})()"""

LOGIN_SIGNALS = ("indeed.com/account/login", "indeed.com/signin",
                 "secure.indeed.com/account")


class IndeedScraper(BaseScraper):

    def __init__(self) -> None:
        self.max_pages = int(os.environ.get("INDEED_MAX_PAGES", "2"))

    async def _status(self, wid: int) -> dict:
        out = await asyncio.to_thread(safari.js, wid, STATUS_JS)
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"cards": 0, "loggedOut": False, "blocked": False}

    async def _wait_state(self, wid: int, want: str,
                          timeout_s: int, prompt: str = "") -> bool:
        """
        Poll STATUS_JS until want ("cards" | "unblocked" | "loggedin").
        Prints prompt once if given. Returns True on success.
        """
        if prompt:
            print(prompt)
        for _ in range(timeout_s // 2):
            try:
                st = await self._status(wid)
            except RuntimeError:
                print("[indeed] Safari window was closed — stopping.")
                return False
            if want == "cards" and st["cards"] > 0:
                return True
            if want == "unblocked" and not st["blocked"]:
                return True
            if want == "loggedin" and not st["loggedOut"]:
                return True
            await asyncio.sleep(2)
        return False

    async def scrape(self) -> list[Job]:
        wid = await asyncio.to_thread(safari.open_window, INDEED_HOME)
        print("[indeed] Safari window opened.")
        try:
            await self.human_delay(3000, 5000)

            # Step 1: session check — Safari holds the real login already
            st = await self._status(wid)
            cur_url = await asyncio.to_thread(safari.current_url, wid)
            if st["loggedOut"] or any(s in cur_url for s in LOGIN_SIGNALS):
                print("[indeed] Not signed in — sign in in the Safari window "
                      "(Google / Apple OK). Waiting up to 120 s...")
                ok = await self._wait_state(wid, "loggedin", 120)
                if not ok:
                    print("[indeed] Still logged out — stopping.")
                    return []
            else:
                print("[indeed] Already signed in — skipping login.")

            # Step 2: search results
            print("[indeed] Navigating to GTA search results...")
            await asyncio.to_thread(safari.goto, wid, INDEED_SEARCH_URL)
            ok = await self._wait_state(wid, "cards", 60)
            if not ok:
                st = await self._status(wid)
                if st["blocked"]:
                    print("[indeed] Verification page — solve it in the "
                          "Safari window. Waiting up to 120 s...")
                    ok = await self._wait_state(wid, "unblocked", 120)
                    if ok:
                        ok = await self._wait_state(wid, "cards", 30)
                if not ok:
                    print("[indeed] No results — stopping.")
                    return []

            jobs: list[Job] = []
            for page_num in range(1, self.max_pages + 1):
                page_jobs = await self._scrape_page(wid)
                jobs.extend(page_jobs)
                print(f"[indeed] Page {page_num}: {len(page_jobs)} jobs found.")
                if page_num == self.max_pages:
                    break
                await self.human_delay(2500, 5000)
                nxt = await asyncio.to_thread(safari.js, wid, NEXT_JS)
                if nxt.strip().strip('"') != "clicked":
                    print("[indeed] No more pages.")
                    break
                await self.human_delay(1500, 3000)
                if not await self._wait_state(wid, "cards", 30):
                    print("[indeed] Next page never loaded — stopping.")
                    break
        finally:
            await asyncio.to_thread(safari.close_window, wid)

        print(f"[indeed] Collected {len(jobs)} jobs.")
        return jobs

    async def _scrape_page(self, wid: int) -> list[Job]:
        jobs: list[Job] = []
        try:
            out = await asyncio.to_thread(safari.js, wid, EXTRACT_JS)
            cards = json.loads(out)
        except (RuntimeError, json.JSONDecodeError) as e:
            print(f"[indeed] Extract error: {e}")
            return jobs

        for card in cards:
            try:
                job = self._parse_card(card)
                if job:
                    jobs.append(job)
            except Exception as e:
                print(f"[indeed] Card parse error: {e}")
        return jobs

    def _parse_card(self, card: dict) -> Job | None:
        title = (card.get("title") or "").strip()
        href = (card.get("href") or "").strip()
        if not title or not href:
            return None
        company = (card.get("company") or "").strip() or "Unknown"
        location = (card.get("location") or "").strip() or "Greater Toronto Area, ON"
        url = f"{INDEED_HOME}{href}" if href.startswith("/") else href
        clean_url = url.split("?")[0] if "?" in url else url

        intern_kws = ["intern", "co-op", "coop", "placement", "student",
                      "junior", "entry level", "new grad"]
        if not any(kw in title.lower() for kw in intern_kws):
            return None

        return Job(
            id=make_job_id("indeed", clean_url),
            title=title,
            company=company,
            location=location,
            platform="indeed",
            url=url,
            date_found=datetime.now().isoformat(timespec="seconds"),
        )
