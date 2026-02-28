"""
Sheridan Works co-op job scraper.

Login flow (Shibboleth → Microsoft SSO):
  1. Navigate to coopJobs.htm → redirected to notLoggedIn.htm
  2. Navigate to /login.htm → click "STUDENT / ALUMNI LOGIN"
  3. Microsoft login (email → Next → password → Sign in)
  4. MFA if required (waits up to 90 s for authenticator approval)
  5. "Stay signed in?" → Yes
  6. Redirected back to Sheridan Works

Session cookies are saved after login so subsequent runs skip re-authentication.

Job table structure (verified against live page 2026-02-26):
  Table: #postingsTable
  Rows:  tr.searchResult  (tbody)
  Cells: [0]=Shortlist/View  [2]=Term  [3]=PostingID  [4]=Title
         [5]=Organization    [11]=City  [12]=App Deadline
  Row id attr: "posting56372" → posting ID 56372
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, ElementHandle

from models.job import Job, make_job_id
from scrapers.base import BaseScraper

SHERIDAN_BASE   = "https://sheridanworks.sheridancollege.ca"
COOP_JOBS_URL   = f"{SHERIDAN_BASE}/myAccount/co-op/coopJobs.htm"
LOGIN_PAGE_URL  = f"{SHERIDAN_BASE}/login.htm"
STUDENT_SSO_URL = (
    f"{SHERIDAN_BASE}/Shibboleth.sso/Login"
    "?entityID=https://sts.windows.net/465ac757-2131-4711-b9a3-d8278b5c0b14/"
    f"&target={SHERIDAN_BASE}/secure/ssoStudent.htm"
)
COOKIES_PATH  = Path(__file__).parent.parent / "data" / "sheridan_cookies.json"
MS_LOGIN_HOST = "login.microsoftonline.com"

# How long to wait (ms) for the AJAX job table to render after clicking search
JOBS_LOAD_TIMEOUT = 12000


class SheridanScraper(BaseScraper):

    def __init__(self) -> None:
        self.username = os.environ["SHERIDAN_USERNAME"]
        self.password = os.environ["SHERIDAN_PASSWORD"]

    async def scrape(self) -> list[Job]:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                slow_mo=50,
                args=["--start-maximized"],
            )
            context = await browser.new_context(
                no_viewport=True,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )

            if COOKIES_PATH.exists():
                cookies = json.loads(COOKIES_PATH.read_text())
                await context.add_cookies(cookies)
                print("[sheridan] Loaded saved session cookies.")

            page = await context.new_page()
            await page.goto(COOP_JOBS_URL, wait_until="domcontentloaded")
            await self.human_delay(1500, 2500)

            current_url = page.url
            if self._is_logged_in(current_url):
                print("[sheridan] Session still valid — skipping login.")
            else:
                print(f"[sheridan] Not logged in ({current_url}). Starting SSO login...")
                await self._do_full_sso_login(page)

            # Save updated cookies
            COOKIES_PATH.parent.mkdir(exist_ok=True)
            COOKIES_PATH.write_text(json.dumps(await context.cookies(), indent=2))
            print("[sheridan] Session cookies saved.")

            # Navigate to the jobs page if needed
            if "coopJobs" not in page.url:
                await page.goto(COOP_JOBS_URL, wait_until="domcontentloaded")
                await self.human_delay(1500, 2500)
                if not self._is_logged_in(page.url):
                    print(f"[sheridan] ERROR: Still not logged in. URL: {page.url}")
                    await browser.close()
                    return []

            jobs = await self._scrape_job_listings(page)
            await browser.close()

        print(f"[sheridan] Collected {len(jobs)} jobs.")
        return jobs

    # ------------------------------------------------------------------
    # Login helpers
    # ------------------------------------------------------------------

    def _is_logged_in(self, url: str) -> bool:
        blocked = ["notLoggedIn", "login.htm", "login/", MS_LOGIN_HOST, "Shibboleth"]
        return (SHERIDAN_BASE in url) and not any(b in url for b in blocked)

    async def _do_full_sso_login(self, page: Page) -> None:
        """Shibboleth → Microsoft SSO login. Handles MFA automatically."""
        print("[sheridan] Opening login page...")
        await page.goto(LOGIN_PAGE_URL, wait_until="domcontentloaded")
        await self.human_delay(1000, 1800)

        # Click "STUDENT / ALUMNI LOGIN"
        student_btn = page.locator("a:text-matches('STUDENT', 'i')")
        await student_btn.first.wait_for(timeout=8000)
        await student_btn.first.click()
        print("[sheridan] Clicked student SSO button, waiting for Microsoft login...")
        await page.wait_for_url(f"**{MS_LOGIN_HOST}**", timeout=20000)
        await self.human_delay(1200, 2000)

        # Microsoft email
        print("[sheridan] Step 1/3 — entering email...")
        email_sel = "input[name='loginfmt'], input[type='email']"
        await page.wait_for_selector(email_sel, timeout=15000)
        await page.wait_for_timeout(500)
        await self.human_type(page, email_sel, self.username)
        await self.human_delay(500, 900)
        await page.click("#idSIButton9")
        await self.human_delay(1200, 2200)
        await page.wait_for_load_state("domcontentloaded")

        # Microsoft password
        print("[sheridan] Step 2/3 — entering password...")
        passwd_sel = "input[name='passwd'], input[type='password']"
        await page.wait_for_selector(passwd_sel, timeout=15000)
        await page.wait_for_timeout(500)
        await self.human_type(page, passwd_sel, self.password)
        await self.human_delay(500, 900)
        await page.click("#idSIButton9")
        await self.human_delay(1500, 2500)

        # MFA (waits for user to approve on phone/authenticator)
        await self._handle_mfa_if_needed(page)

        # "Stay signed in?" → Yes
        try:
            stay_btn = page.locator("#idSIButton9")
            if await stay_btn.is_visible(timeout=6000):
                print("[sheridan] Step 3/3 — confirming 'Stay signed in'...")
                await stay_btn.click()
                await self.human_delay(1000, 2000)
        except Exception:
            pass

        print("[sheridan] Waiting for redirect back to Sheridan Works...")
        try:
            await page.wait_for_url(f"**{SHERIDAN_BASE}**", timeout=30000)
        except Exception:
            pass
        await self.human_delay(1500, 2500)
        print("[sheridan] Login complete.")

    async def _handle_mfa_if_needed(self, page: Page) -> None:
        """Waits up to 90 s for the user to approve an MFA prompt."""
        mfa_sels = [
            "input[name='otc']",
            "#idRichContext",
            "#idDiv_SAOTCS_Proofs",
        ]
        for sel in mfa_sels:
            try:
                if await page.locator(sel).is_visible(timeout=2500):
                    print("\n[sheridan] *** MFA required ***")
                    print("[sheridan]     Approve the sign-in on your Authenticator app.")
                    print("[sheridan]     Waiting up to 90 seconds...\n")
                    await page.wait_for_function(
                        f"() => !document.querySelector('{sel}')",
                        timeout=90000,
                    )
                    return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Job scraping
    # ------------------------------------------------------------------

    async def _scrape_job_listings(self, page: Page) -> list[Job]:
        """
        Triggers "For My Program" quick search, waits for the AJAX table,
        then paginates through all results.
        """
        jobs: list[Job] = []
        page_num = 1

        print("[sheridan] Triggering job search ('For My Program')...")
        await page.evaluate("""() => {
            const a = document.querySelector('.stat-table a[onclick*="displayQuickSearch"]');
            if (a) a.click();
        }""")

        while True:
            # Wait for the postingsTable to appear / update
            try:
                await page.wait_for_selector(
                    "#postingsTable tbody tr.searchResult",
                    timeout=JOBS_LOAD_TIMEOUT,
                )
            except Exception:
                print(f"[sheridan] Timed out waiting for job table on page {page_num}.")
                break

            await self.human_delay(800, 1500)

            # Extract all visible rows
            rows = await page.query_selector_all(
                "#postingsTable tbody tr.searchResult"
            )
            print(f"[sheridan] Page {page_num}: {len(rows)} rows found.")

            for row in rows:
                try:
                    job = await self._parse_row(row)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    print(f"[sheridan] Row parse error: {e}")

            # Check for a "Next" pagination button
            next_btn = page.locator(
                ".pagination a:text('Next'), .pagination a:text('>'), "
                ".pagination li:not(.disabled) a[rel='next']"
            )
            if await next_btn.count() > 0:
                await next_btn.first.click()
                await self.human_delay(1500, 3000)
                page_num += 1
            else:
                break

        return jobs

    async def _parse_row(self, row: ElementHandle) -> Job | None:
        """
        Extracts a Job from a single #postingsTable tbody row.

        Cell layout (0-indexed):
          0  = Shortlist / View buttons
          1  = App Status
          2  = Term
          3  = Posting ID
          4  = Job Title (may have "NEW " badge prefix)
          5  = Organization
          6  = Division
          7  = Position Type
          8  = Openings
          9  = Internal Status
          10 = Location
          11 = City
          12 = App Deadline
          13 = (empty)
        """
        cells = await row.query_selector_all("td")
        if len(cells) < 12:
            return None

        # Posting ID from the row's own id attribute ("posting56372")
        row_id_attr = (await row.get_attribute("id")) or ""
        posting_id = row_id_attr.replace("posting", "").strip()
        if not posting_id.isdigit():
            return None

        # Term — must be Summer 2026
        term = (await cells[2].inner_text()).strip()
        if "summer" not in term.lower():
            return None  # Skip non-summer terms

        # Title — strip leading "NEW " badge text
        title_raw = (await cells[4].inner_text()).strip()
        title = re.sub(r"^NEW\s+", "", title_raw).strip()
        if not title:
            return None

        # Organization
        company = (await cells[5].inner_text()).strip() or "Unknown"

        # City
        city = (await cells[11].inner_text()).strip() or "Ontario, Canada"

        # Deadline
        deadline_raw = (await cells[12].inner_text()).strip()
        deadline = _parse_date(deadline_raw) if deadline_raw else None

        # URL: deep-link reference using posting ID
        url = f"{COOP_JOBS_URL}#posting{posting_id}"

        job_id = make_job_id("sheridan", url)
        return Job(
            id=job_id,
            title=title,
            company=company,
            location=city,
            platform="sheridan",
            url=url,
            date_found=datetime.now().isoformat(timespec="seconds"),
            deadline=deadline,
            job_type="Co-op",
        )


def _parse_date(raw: str) -> str | None:
    """Normalises messy date strings to YYYY-MM-DD."""
    raw = raw.strip()
    # Already ISO
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d, %Y %I:%M %p",
                "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            from datetime import datetime as dt
            # Strip time portion for formats like "Mar 4, 2026 11:59 PM"
            cleaned = re.sub(r"\s+\d+:\d+.*$", "", raw.strip())
            return dt.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw
