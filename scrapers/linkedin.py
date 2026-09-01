"""
LinkedIn recruiter collector.

For companies you've APPLIED to, opens LinkedIn people search for
"<company> recruiter", scrapes the first page of results (name, title,
profile URL) into data/recruiters.json.

Bot-detection posture (same philosophy as the Indeed scraper):
  - Headful Chrome with a persistent profile (data/linkedin_profile/)
  - navigator.webdriver overridden before any page load
  - Long random delays; never more than a handful of company searches
    per run
  - If LinkedIn throws a checkpoint / login wall, it pauses and waits
    for YOU to resolve it manually in the window, then continues

Toggle: config.yaml -> toggles.linkedin_collect  (default OFF)
"""
from __future__ import annotations
import asyncio
import hashlib
import random
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

import config
from models.job import Job
from storage.jobs import load_jobs
from storage.recruiters import upsert

PROFILE_DIR = Path(__file__).parent.parent / "data" / "linkedin_profile"
MAX_COMPANIES_PER_RUN = 5      # hard cap regardless of config
RESULTS_PER_SEARCH = 5


def _companies_with_applied_jobs() -> list[str]:
    jobs = load_jobs()
    companies = {
        j.company for j in jobs.values()
        if j.platform == "sheridan" and j.status == "applied" and j.company
    }
    return sorted(companies)


async def collect() -> None:
    if not config.enabled("linkedin_collect"):
        print("[li] linkedin_collect is OFF — enable it in config.yaml")
        return

    if config.dry_run():
        companies = _companies_with_applied_jobs()
        print(f"[li] DRY RUN — would search recruiters at "
              f"{len(companies)} compan(ies): {', '.join(companies) or '(none)'}")
        return

    companies = _companies_with_applied_jobs()[:MAX_COMPANIES_PER_RUN]
    if not companies:
        print("[li] No applied Sheridan jobs yet — apply first.")
        return

    print(f"[li] Searching recruiters at {len(companies)} compan(ies): "
          f"{', '.join(companies)}")

    async with async_playwright() as p:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
            viewport=None,
            locale="en-CA",
            timezone_id="America/Toronto",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        from storage.safari_cookies import matching
        imported = matching("linkedin")
        if imported:
            await context.add_cookies(imported)
            print(f"[li] Injected {len(imported)} Safari cookie(s).")

        page = context.pages[0] if context.pages else await context.new_page()

        for company in companies:
            url = (
                "https://www.linkedin.com/search/results/people/"
                f"?keywords={company.replace(' ', '%20')}%20recruiter"
                "&origin=GLOBAL_SEARCH_HEADER"
            )
            print(f"\n[li] ▶ {company}: {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"[li] Load failed ({e}); pausing for manual check.")
                input("    Resolve anything in the browser, press Enter... ")
                continue

            # Login wall / checkpoint → wait for human
            if "/login" in page.url or "checkpoint" in page.url:
                input("[li] Login/checkpoint detected. "
                      "Sign in manually, then press Enter... ")
                await page.goto(url, wait_until="domcontentloaded")

            await asyncio.sleep(random.uniform(3, 6))
            found = await _scrape_results(page, company)
            print(f"[li] {company}: saved {found} recruiter(s)")

            await asyncio.sleep(random.uniform(8, 20))  # slow pacing

        input("\n[li] Done collecting. Press Enter to close the browser... ")
        await context.close()


async def _scrape_results(page, company: str) -> int:
    """Pull visible people-search result cards and store them."""
    cards = await page.query_selector_all(
        "div.entity-result, li.reusable-search__result-container"
    )
    saved = 0
    for card in cards[:RESULTS_PER_SEARCH]:
        try:
            link_el = await card.query_selector(
                "a.app-aware-link[href*='/in/']"
            )
            name_el = await card.query_selector(
                ".entity-result__title-text a span[aria-hidden='true'], "
                ".entity-result__title-line span[aria-hidden='true']"
            )
            title_el = await card.query_selector(
                ".entity-result__primary-subtitle"
            )
            if not (link_el and name_el):
                continue

            profile_url = (await link_el.get_attribute("href") or "").split("?")[0]
            name = ((await name_el.inner_text()) or "").strip()
            title = ((await title_el.inner_text()) if title_el else "") or ""
            title = title.strip()

            if not profile_url or not name:
                continue

            r_id = hashlib.sha256(profile_url.encode()).hexdigest()[:12]
            upsert(r_id, {
                "name": name,
                "title": title,
                "company": company,
                "profile_url": profile_url,
                "date_found": datetime.now().isoformat(timespec="seconds"),
                "draft": None,
                "draft_date": None,
                "sent_date": None,
            })
            saved += 1
        except Exception:
            continue
    return saved


if __name__ == "__main__":
    asyncio.run(collect())
