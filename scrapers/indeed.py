"""
Indeed Canada co-op / internship scraper.

Bot detection notes:
  - Always runs headful (headless=False).
  - navigator.webdriver is overridden before page loads.
  - Homepage warm-up before search.
  - Uses the search bar (more human) instead of a direct search URL.
  - Waits for Cloudflare "Just a moment..." to auto-resolve.
  - Random delays between pages.

Login / session persistence:
  - Uses a persistent Chrome profile (data/indeed_profile/) so cookies —
    including the Indeed login session — survive across runs.
  - On first run (or if the session expired), the scraper detects the
    login wall, fills in INDEED_EMAIL / INDEED_PASSWORD from .env,
    and submits.  After that, the session is saved in the profile and
    subsequent runs skip login entirely.
  - If INDEED_EMAIL / INDEED_PASSWORD are not set the scraper pauses
    and lets you sign in manually, then continues automatically.

Summer 2026 filtering:
  Indeed has no co-op term filter, so the search uses keywords.
  All intern/co-op results are kept; use `status <id> skipped` to dismiss
  irrelevant ones.
"""
from __future__ import annotations
import os
import random
from datetime import datetime

from playwright.async_api import async_playwright, Page

from pathlib import Path

from models.job import Job, make_job_id
from scrapers.base import BaseScraper

INDEED_HOME         = "https://ca.indeed.com"
INDEED_PROFILE_DIR  = Path(__file__).parent.parent / "data" / "indeed_profile"

# Greater Toronto Area, sorted by date, past 30 days
INDEED_SEARCH_URL = (
    "https://ca.indeed.com/jobs"
    "?q=software+engineer+OR+developer+intern+OR+co-op+summer+2026"
    "&l=Greater+Toronto+Area%2C+ON"
    "&radius=35"
    "&fromage=30"
    "&sort=date"
)

INDEED_LOGIN_URL = "https://ca.indeed.com/account/login"


class IndeedScraper(BaseScraper):

    def __init__(self) -> None:
        self.max_pages  = int(os.environ.get("INDEED_MAX_PAGES", "2"))
        self.email      = os.environ.get("INDEED_EMAIL", "")
        self.password   = os.environ.get("INDEED_PASSWORD", "")

    async def scrape(self) -> list[Job]:
        async with async_playwright() as p:
            # Use a persistent browser profile so Cloudflare cookies AND the
            # Indeed login session accumulate across runs.
            profile_dir = str(INDEED_PROFILE_DIR)
            INDEED_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

            launch_kwargs = dict(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1366, "height": 768},
                locale="en-CA",
                timezone_id="America/Toronto",
            )

            try:
                context = await p.chromium.launch_persistent_context(
                    profile_dir,
                    channel="chrome",   # Use real Chrome binary
                    **launch_kwargs,
                )
                print("[indeed] Using real Chrome with persistent profile.")
            except Exception:
                context = await p.chromium.launch_persistent_context(
                    profile_dir,
                    **launch_kwargs,
                )
                print("[indeed] Using Playwright Chromium with persistent profile.")

            # Override automation fingerprint on every page
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-CA', 'en'] });
                window.chrome = { runtime: {} };
            """)

            # Safari-imported cookies (if python main.py import-cookies ran)
            from storage.safari_cookies import matching
            imported = matching("indeed")
            if imported:
                await context.add_cookies(imported)
                print(f"[indeed] Injected {len(imported)} Safari cookie(s).")

            page = context.pages[0] if context.pages else await context.new_page()

            # Step 1: warm up on the homepage
            print("[indeed] Loading homepage...")
            await page.goto(INDEED_HOME, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            await page.mouse.move(random.randint(300, 900), random.randint(200, 500))
            await self.human_delay(1500, 2500)

            # Step 2: log in if the session is not already active
            await self._login_if_needed(page)

            # Step 3: navigate to GTA search results
            print("[indeed] Navigating to GTA search results...")
            await page.goto(INDEED_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
            resolved = await self._wait_for_cloudflare(page)
            if not resolved:
                print("[indeed] Cloudflare challenge did not resolve. Stopping.")
                await context.close()
                return []

            # If redirected to login again after search nav, log in and retry
            if self._is_login_page(page.url):
                print("[indeed] Redirected to login after search nav — logging in again...")
                await self._do_login(page)
                await page.goto(INDEED_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
                await self._wait_for_cloudflare(page)

            await self.human_delay(1500, 2500)

            jobs: list[Job] = []

            for page_num in range(1, self.max_pages + 1):
                if await self._is_hard_blocked(page):
                    print(f"[indeed] Bot detection on page {page_num}. Stopping.")
                    break

                print(f"[indeed] Scraping page {page_num}...")
                page_jobs = await self._scrape_results_page(page)
                jobs.extend(page_jobs)
                print(f"[indeed] Page {page_num}: {len(page_jobs)} jobs found.")

                # Advance to next page
                next_btn = page.locator(
                    "a[data-testid='pagination-page-next'], "
                    "a[aria-label='Next Page']"
                )
                if await next_btn.count() == 0:
                    print("[indeed] No more pages.")
                    break

                await self.human_delay(2500, 5000)
                await next_btn.first.click()
                await page.wait_for_load_state("domcontentloaded")
                await self._wait_for_cloudflare(page)
                await self.human_delay(1500, 3000)

            await context.close()

        print(f"[indeed] Collected {len(jobs)} jobs.")
        return jobs

    # ------------------------------------------------------------------
    # Login helpers
    # ------------------------------------------------------------------

    def _is_login_page(self, url: str) -> bool:
        """True when Indeed has sent us to its login / sign-in page."""
        login_signals = [
            "indeed.com/account/login",
            "indeed.com/signin",
            "secure.indeed.com/account",
        ]
        return any(s in url for s in login_signals)

    async def _login_if_needed(self, page: Page) -> None:
        """
        Checks whether we are already signed in on the homepage.
        Signs in if not.  Uses credentials from .env; if not set, pauses
        for the user to sign in manually.
        """
        # Detect logged-out state: "Sign in" link visible in nav
        try:
            sign_in_link = page.locator(
                "a[href*='/account/login'], "
                "a[data-tn-element='signin'], "
                "a:text-matches('sign in', 'i')"
            )
            is_logged_out = await sign_in_link.first.is_visible(timeout=3000)
        except Exception:
            is_logged_out = False

        if not is_logged_out:
            print("[indeed] Already signed in — skipping login.")
            return

        print("[indeed] Not signed in. Starting Indeed login...")
        await self._do_login(page)

    async def _do_login(self, page: Page) -> None:
        """
        Navigates to the Indeed login page and signs in.
        If credentials are not in .env, waits up to 120 s for the user
        to log in manually in the open browser window.
        """
        await page.goto(INDEED_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await self._wait_for_cloudflare(page, max_seconds=30)
        await self.human_delay(1000, 2000)

        if self.email and self.password:
            print("[indeed] Step 1/2 — entering email...")
            email_sel = (
                "input[name='__email'], input[type='email'], "
                "input[id*='email'], input[autocomplete='email']"
            )
            try:
                await page.wait_for_selector(email_sel, timeout=10000)
                await self.human_type(page, email_sel, self.email)
                await self.human_delay(400, 800)

                # Click Continue / Next
                continue_btn = page.locator(
                    "button[type='submit'], "
                    "button:text-matches('continue|next|sign in', 'i')"
                )
                await continue_btn.first.click()
                await self.human_delay(1200, 2000)
                await page.wait_for_load_state("domcontentloaded")

                # Password step (some flows show it on a second screen)
                print("[indeed] Step 2/2 — entering password...")
                passwd_sel = (
                    "input[name='password'], input[type='password'], "
                    "input[id*='password'], input[autocomplete='current-password']"
                )
                try:
                    await page.wait_for_selector(passwd_sel, timeout=8000)
                    await self.human_type(page, passwd_sel, self.password)
                    await self.human_delay(400, 800)

                    submit_btn = page.locator(
                        "button[type='submit'], "
                        "button:text-matches('sign in|log in|continue', 'i')"
                    )
                    await submit_btn.first.click()
                    await self.human_delay(2000, 3500)
                    await page.wait_for_load_state("domcontentloaded")
                    print("[indeed] Login submitted — waiting for redirect...")
                except Exception as e:
                    print(f"[indeed] Password step error: {e}")
            except Exception as e:
                print(f"[indeed] Email step error: {e}")
        else:
            print("[indeed] INDEED_EMAIL / INDEED_PASSWORD not set in .env.")
            print("[indeed] Please sign in manually in the browser window.")
            print("[indeed] Waiting up to 120 s for you to complete sign-in...")

        # Wait until we are no longer on a login/sign-in page (up to 120 s)
        for _ in range(120):
            await page.wait_for_timeout(1000)
            if not self._is_login_page(page.url):
                print("[indeed] Login complete — session will persist in Chrome profile.")
                return
        print("[indeed] Warning: still on login page after 120 s; continuing anyway.")

    # ------------------------------------------------------------------
    # Cloudflare helpers
    # ------------------------------------------------------------------

    async def _wait_for_cloudflare(self, page: Page, max_seconds: int = 60) -> bool:
        """
        Handles the Cloudflare challenge gate in two steps:
          1. Auto-JS challenge ("Just a moment…") — usually resolves in 8-15 s.
          2. Interactive Turnstile ("Additional Verification") — tries to click
             the checkbox in the challenge iframe; if that fails, waits for you
             to click it manually (up to max_seconds total).
        Returns True if the page is unblocked, False if we timed out.
        """
        for i in range(max_seconds):
            title = (await page.title()).lower()

            # Challenge gone — we're through
            if "just a moment" not in title:
                if i > 0:
                    print(f"[indeed] Cloudflare resolved after {i+1}s.")
                return True

            # Every 3 seconds, do a small random mouse movement to look human
            if i % 3 == 0:
                vw = page.viewport_size or {"width": 1366, "height": 768}
                await page.mouse.move(
                    random.randint(100, vw["width"] - 100),
                    random.randint(100, vw["height"] - 100),
                )

            # After 5 s of still being on "Just a moment", try clicking the
            # Turnstile iframe checkbox if it exists
            if i == 5:
                await self._try_click_turnstile(page)

            await page.wait_for_timeout(1000)

        print(f"[indeed] Cloudflare challenge did not resolve after {max_seconds}s.")
        return False

    async def _try_click_turnstile(self, page: Page) -> None:
        """
        Attempts to click the Cloudflare Turnstile checkbox inside its iframe.
        Silently does nothing if the iframe/checkbox is not present.
        """
        try:
            # Cloudflare Turnstile is rendered inside an iframe
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url or "turnstile" in frame.url:
                    checkbox = await frame.query_selector("input[type='checkbox'], .cf-turnstile-wrapper")
                    if checkbox:
                        await checkbox.click()
                        print("[indeed] Clicked Turnstile checkbox.")
                        return
            # Also try clicking any visible "Verify you are human" button
            btn = page.locator(
                "button:text-matches('human|verify|continue', 'i'), "
                "input[type='checkbox'][id*='turnstile'], "
                "label[for*='turnstile']"
            )
            if await btn.count() > 0:
                await btn.first.click()
                print("[indeed] Clicked Turnstile button on main page.")
        except Exception as e:
            print(f"[indeed] Turnstile click attempt: {e}")

    async def _is_hard_blocked(self, page: Page) -> bool:
        """True when Cloudflare or Indeed explicitly blocks us."""
        title = (await page.title()).lower()
        body_start = (await page.evaluate(
            "() => document.body?.innerText?.slice(0, 300) || ''"
        )).lower()
        blocked = ["captcha", "unusual traffic", "robot", "automated",
                   "are you a human", "additional verification", "access denied"]
        cf_blocked = "just a moment" in title  # Cloudflare challenge still showing
        explicit = any(kw in title + body_start for kw in blocked)
        return cf_blocked or explicit

    # ------------------------------------------------------------------
    # Scraping helpers
    # ------------------------------------------------------------------

    async def _scrape_results_page(self, page: Page) -> list[Job]:
        """Extracts job cards from the current Indeed search results page."""
        jobs: list[Job] = []
        card_sel = (
            "[data-testid='slider_item'], .job_seen_beacon, "
            "li.css-5lfssm, div.job_seen_beacon"
        )
        job_cards = await page.query_selector_all(card_sel)

        for card in job_cards:
            try:
                job = await self._parse_card(card)
                if job:
                    jobs.append(job)
            except Exception as e:
                print(f"[indeed] Card parse error: {e}")

        return jobs

    async def _parse_card(self, card) -> Job | None:
        """Extracts a Job from a single Indeed job card element."""
        title_el = await card.query_selector(
            "[data-testid='jobTitle'] span, h2.jobTitle span, .jcs-JobTitle span"
        )
        link_el = await card.query_selector(
            "a[data-testid='job-title-link'], a[id^='job_'], "
            "a.jcs-JobTitle, h2.jobTitle a"
        )
        company_el = await card.query_selector(
            "[data-testid='company-name'], .companyName, "
            "[class*='companyName'] span"
        )
        location_el = await card.query_selector(
            "[data-testid='text-location'], .companyLocation, "
            "[class*='location']"
        )

        if not title_el or not link_el:
            return None

        title   = (await title_el.inner_text()).strip()
        company = (await company_el.inner_text()).strip() if company_el else "Unknown"
        location= (await location_el.inner_text()).strip() if location_el else "Greater Toronto Area, ON"
        href    = (await link_el.get_attribute("href")) or ""
        url     = f"{INDEED_HOME}{href}" if href.startswith("/") else href
        # Strip tracking params for the dedup ID
        clean_url = url.split("?")[0] if "?" in url else url

        # Keep only internship / co-op / student roles
        intern_kws = ["intern", "co-op", "coop", "placement", "student",
                      "junior", "entry level", "new grad"]
        if not any(kw in title.lower() for kw in intern_kws):
            return None

        job_id = make_job_id("indeed", clean_url)
        return Job(
            id=job_id,
            title=title,
            company=company,
            location=location,
            platform="indeed",
            url=url,
            date_found=datetime.now().isoformat(timespec="seconds"),
        )
