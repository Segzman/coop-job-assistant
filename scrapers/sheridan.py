"""
Sheridan Works co-op job scraper — real Safari via AppleScript.

Login flow (Shibboleth → Microsoft SSO):
  1. Navigate to coopJobs.htm → redirected to notLoggedIn.htm
  2. Navigate to /login.htm → click "STUDENT / ALUMNI LOGIN"
  3. Microsoft login (email → Next → password → Sign in)
  4. MFA if required (waits up to 90 s for authenticator approval)
  5. "Stay signed in?" → Yes
  6. Redirected back to Sheridan Works

No cookie files: Safari keeps the session itself between runs, so a
valid login is simply still there next time.

Job table structure (verified against live page 2026-02-26):
  Table: #postingsTable
  Rows:  tr.searchResult  (tbody)
  Cells: [0]=Shortlist/View  [2]=Term  [3]=PostingID  [4]=Title
          [5]=Organization    [11]=City  [12]=App Deadline
  Row id attr: "posting56372" → posting ID 56372
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime

from browser import safari_bridge as safari
from models.job import Job, make_job_id
from scrapers.base import BaseScraper

SHERIDAN_BASE = "https://sheridanworks.sheridancollege.ca"
COOP_JOBS_URL = f"{SHERIDAN_BASE}/myAccount/co-op/coopJobs.htm"
LOGIN_PAGE_URL = f"{SHERIDAN_BASE}/login.htm"
MS_LOGIN_HOST = "login.microsoftonline.com"

# How long to wait (s) for the AJAX job table to render after search
JOBS_LOAD_TIMEOUT = 15

ROWS_JS = """(() => JSON.stringify(
  [...document.querySelectorAll("#postingsTable tbody tr.searchResult")]
    .map(tr => ({
      rowId: tr.getAttribute("id") || "",
      cells: [...tr.querySelectorAll("td")].map(td => td.innerText.trim()),
    }))))()"""

ROWS_PRESENT_JS = """(() => document.querySelectorAll(
  "#postingsTable tbody tr.searchResult").length > 0
  ? "yes" : "no")()"""

NEXT_JS = """(() => {
  const n = [...document.querySelectorAll(".pagination a")]
    .find(a => /^(next|>)$/i.test(a.innerText.trim())
      && !a.closest("li.disabled"));
  if (!n) return "none";
  n.click();
  return "clicked";
})()"""

TRIGGER_SEARCH_JS = """(() => {
  const a = document.querySelector(
    '.stat-table a[onclick*="displayQuickSearch"]');
  if (!a) return "miss";
  a.click();
  return "clicked";
})()"""


class SheridanScraper(BaseScraper):

    def __init__(self) -> None:
        self.username = os.environ["SHERIDAN_USERNAME"]
        self.password = os.environ["SHERIDAN_PASSWORD"]

    async def _url(self, wid: int) -> str:
        try:
            return await asyncio.to_thread(safari.current_url, wid)
        except RuntimeError:
            return ""

    async def _poll_js(self, wid: int, script: str, expect: str,
                       timeout_s: int) -> bool:
        for _ in range(timeout_s * 2):
            try:
                out = (await asyncio.to_thread(safari.js, wid, script)
                       ).strip().strip('"')
            except RuntimeError:
                return False
            if out == expect:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_url(self, wid: int, needle: str, timeout_s: int) -> bool:
        for _ in range(timeout_s * 2):
            if needle in await self._url(wid):
                return True
            await asyncio.sleep(0.5)
        return False

    async def scrape(self) -> list[Job]:
        wid = await asyncio.to_thread(safari.open_window, COOP_JOBS_URL)
        print("[sheridan] Safari window opened.")
        try:
            await self.human_delay(2500, 4000)

            if self._is_logged_in(await self._url(wid)):
                print("[sheridan] Session still valid — skipping login.")
            else:
                print("[sheridan] Not logged in. Starting SSO login...")
                await self._do_full_sso_login(wid)

            if "coopJobs" not in await self._url(wid):
                await asyncio.to_thread(safari.goto, wid, COOP_JOBS_URL)
                await self.human_delay(1500, 2500)
                if not self._is_logged_in(await self._url(wid)):
                    print("[sheridan] ERROR: Still not logged in.")
                    return []

            jobs = await self._scrape_job_listings(wid)
        finally:
            await asyncio.to_thread(safari.close_window, wid)

        print(f"[sheridan] Collected {len(jobs)} jobs.")
        return jobs

    # ------------------------------------------------------------------
    # Login helpers
    # ------------------------------------------------------------------

    def _is_logged_in(self, url: str) -> bool:
        blocked = ["notLoggedIn", "login.htm", "login/", MS_LOGIN_HOST,
                   "Shibboleth"]
        return (SHERIDAN_BASE in url) and not any(b in url for b in blocked)

    async def _do_full_sso_login(self, wid: int) -> None:
        """Shibboleth → Microsoft SSO login. Handles MFA automatically."""
        print("[sheridan] Opening login page...")
        await asyncio.to_thread(safari.goto, wid, LOGIN_PAGE_URL)
        await self.human_delay(1500, 2500)

        # Click "STUDENT / ALUMNI LOGIN"
        clicked = await self.safari_click(wid, [
            "a[href*='STUDENT' i]", "a[href*='student']",
        ])
        if not clicked:
            # Fallback: click by visible text
            await asyncio.to_thread(safari.js, wid, """(() => {
              const a = [...document.querySelectorAll("a")]
                .find(e => /student/i.test(e.innerText));
              if (a) { a.click(); return "clicked"; }
              return "miss";
            })()""")
        print("[sheridan] Clicked student SSO button, waiting for Microsoft...")
        await self._wait_url(wid, MS_LOGIN_HOST, 20)
        await self.human_delay(1500, 2500)

        # Microsoft email
        print("[sheridan] Step 1/3 — entering email...")
        if await self._poll_js(wid,
                               _present_js("input[name='loginfmt'], "
                                           "input[type='email']"), "yes", 15):
            await self.safari_fill(wid, ["input[name='loginfmt']",
                                         "input[type='email']"],
                                   self.username)
            await self.human_delay(500, 900)
            await self.safari_click(wid, ["#idSIButton9",
                                          "button[type='submit']"])
            await self.human_delay(1500, 2500)

        # Microsoft password
        print("[sheridan] Step 2/3 — entering password...")
        if await self._poll_js(wid,
                               _present_js("input[name='passwd'], "
                                           "input[type='password']"),
                               "yes", 15):
            await self.safari_fill(wid, ["input[name='passwd']",
                                         "input[type='password']"],
                                   self.password)
            await self.human_delay(500, 900)
            await self.safari_click(wid, ["#idSIButton9",
                                          "button[type='submit']"])
            await self.human_delay(2000, 3000)

        # MFA (waits for user to approve on phone/authenticator)
        await self._handle_mfa_if_needed(wid)

        # "Stay signed in?" → Yes
        try:
            if await self.safari_present(wid, ["#idSIButton9"]):
                print("[sheridan] Step 3/3 — confirming 'Stay signed in'...")
                await self.safari_click(wid, ["#idSIButton9"])
                await self.human_delay(1000, 2000)
        except RuntimeError:
            pass

        print("[sheridan] Waiting for redirect back to Sheridan Works...")
        await self._wait_url(wid, SHERIDAN_BASE, 30)
        await self.human_delay(1500, 2500)
        print("[sheridan] Login complete.")

    async def _handle_mfa_if_needed(self, wid: int) -> None:
        """Waits up to 90 s for the user to approve an MFA prompt."""
        mfa_sels = ["input[name='otc']", "#idRichContext",
                    "#idDiv_SAOTCS_Proofs"]
        for sel in mfa_sels:
            try:
                if await self.safari_present(wid, [sel]):
                    print("\n[sheridan] *** MFA required ***")
                    print("[sheridan]     Approve the sign-in on your "
                          "Authenticator app.")
                    print("[sheridan]     Waiting up to 90 seconds...\n")
                    gone = _absent_js(sel)
                    for _ in range(180):
                        out = (await asyncio.to_thread(safari.js, wid, gone)
                               ).strip().strip('"')
                        if out == "yes":
                            return
                        await asyncio.sleep(0.5)
                    return
            except RuntimeError:
                return

    # ------------------------------------------------------------------
    # Job scraping
    # ------------------------------------------------------------------

    async def _scrape_job_listings(self, wid: int) -> list[Job]:
        """
        Triggers "For My Program" quick search, waits for the AJAX table,
        then paginates through all results.
        """
        jobs: list[Job] = []
        page_num = 1

        print("[sheridan] Triggering job search ('For My Program')...")
        await asyncio.to_thread(safari.js, wid, TRIGGER_SEARCH_JS)

        while True:
            if not await self._poll_js(wid, ROWS_PRESENT_JS, "yes",
                                       JOBS_LOAD_TIMEOUT):
                print(f"[sheridan] Timed out waiting for job table "
                      f"on page {page_num}.")
                break

            await self.human_delay(800, 1500)

            try:
                out = await asyncio.to_thread(safari.js, wid, ROWS_JS)
                rows = json.loads(out)
            except (RuntimeError, json.JSONDecodeError) as e:
                print(f"[sheridan] Extract error: {e}")
                break
            print(f"[sheridan] Page {page_num}: {len(rows)} rows found.")

            for row in rows:
                try:
                    job = self._parse_row(row)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    print(f"[sheridan] Row parse error: {e}")

            try:
                nxt = (await asyncio.to_thread(safari.js, wid, NEXT_JS)
                       ).strip().strip('"')
            except RuntimeError:
                break
            if nxt == "clicked":
                await self.human_delay(1500, 3000)
                page_num += 1
            else:
                break

        return jobs

    def _parse_row(self, row: dict) -> Job | None:
        """
        Extracts a Job from one table row ({rowId, cells}).
        Cell layout (0-indexed): 0=Shortlist/View 1=App Status 2=Term
        3=Posting ID 4=Title 5=Organization 11=City 12=App Deadline.
        """
        cells = row.get("cells", [])
        if len(cells) < 12:
            return None

        posting_id = (row.get("rowId", "") or "").replace("posting", "").strip()
        if not posting_id.isdigit():
            return None

        term = (cells[2] or "").strip()
        if not _term_ok(term):
            return None  # Skip stale terms (e.g. past co-op cycles)

        title = re.sub(r"^NEW\s+", "", (cells[4] or "").strip()).strip()
        if not title:
            return None

        company = (cells[5] or "").strip() or "Unknown"
        city = (cells[11] or "").strip() or "Ontario, Canada"
        deadline_raw = (cells[12] or "").strip()
        deadline = _parse_date(deadline_raw) if deadline_raw else None
        if deadline and deadline < datetime.now().strftime("%Y-%m-%d"):
            return None  # Already closed

        url = f"{COOP_JOBS_URL}#posting{posting_id}"
        return Job(
            id=make_job_id("sheridan", url),
            title=title,
            company=company,
            location=city,
            platform="sheridan",
            url=url,
            date_found=datetime.now().isoformat(timespec="seconds"),
            deadline=deadline,
            job_type="Co-op",
        )


def _present_js(selectors: str) -> str:
    import json as _json
    return ("(() => [...document.querySelectorAll("
            f"{_json.dumps(selectors)})].some(e => e && "
            'e.offsetParent !== null) ? "yes" : "no")()')


def _absent_js(selector: str) -> str:
    import json as _json
    return ("(() => !document.querySelector("
            f"{_json.dumps(selector)}) ? \"yes\" : \"no\")()")


def _term_ok(term: str) -> bool:
    """
    Accepts current/future co-op cycles ("Winter 2027", "Summer 2027",
    "Fall 2026"...), rejects stale ones ("Summer 2025"). The board rolls
    terms each cycle, so match on year instead of a hardcoded season.
    """
    m = re.search(r"(19|20)\d{2}", term)
    if not m:
        return "summer" in term.lower()  # unparseable → legacy behaviour
    return int(m.group(0)) >= datetime.now().year


def _parse_date(raw: str) -> str | None:
    """Normalises messy date strings to YYYY-MM-DD."""
    raw = raw.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d, %Y %I:%M %p",
                "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            from datetime import datetime as dt
            cleaned = re.sub(r"\s+\d+:\d+.*$", "", raw.strip())
            return dt.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw
