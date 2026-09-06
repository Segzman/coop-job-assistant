"""
LinkedIn recruiter collector — real Safari via AppleScript.

For companies you've APPLIED to, opens LinkedIn people search for
"<company> recruiter", scrapes the first page of results (name, title,
profile URL) into data/recruiters.json.

Posture: real Safari window, one page per company, slow pacing, hard cap
of 5 companies per run. Login wall / checkpoint → pauses for YOU in the
visible window, then continues.

Toggle: config.yaml -> toggles.linkedin_collect  (default OFF)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
from datetime import datetime

from browser import safari_bridge as safari
import config
from scrapers.base import BaseScraper
from storage.jobs import load_jobs
from storage.recruiters import upsert

MAX_COMPANIES_PER_RUN = 5      # hard cap regardless of config
RESULTS_PER_SEARCH = 5

SEARCH_JS = """(() => JSON.stringify(
  [...document.querySelectorAll(
    "div.entity-result, li.reusable-search__result-container")]
    .slice(0, __N__)
    .map(card => {
      const a = card.querySelector("a.app-aware-link[href*='/in/']");
      const n = card.querySelector(
        ".entity-result__title-text a span[aria-hidden='true'], " +
        ".entity-result__title-line span[aria-hidden='true']");
      const t = card.querySelector(".entity-result__primary-subtitle");
      return {
        url: a ? (a.getAttribute("href") || "").split("?")[0] : "",
        name: n ? n.innerText.trim() : "",
        title: t ? t.innerText.trim() : "",
      };
    })))()"""


def _companies_with_applied_jobs() -> list[str]:
    jobs = load_jobs()
    companies = {
        j.company for j in jobs.values()
        if j.platform == "sheridan" and j.status == "applied" and j.company
    }
    return sorted(companies)


class LinkedInCollector(BaseScraper):

    async def scrape(self) -> None:  # keep BaseScraper interface
        await collect()

    async def _url(self, wid: int) -> str:
        try:
            return await asyncio.to_thread(safari.current_url, wid)
        except RuntimeError:
            return ""


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

    wid = await asyncio.to_thread(
        safari.open_window, "https://www.linkedin.com/feed/")
    await asyncio.sleep(4)
    try:
        try:
            url = await asyncio.to_thread(safari.current_url, wid)
        except RuntimeError:
            url = ""
        if "/login" in url or "checkpoint" in url:
            input("[li] LinkedIn wants login. Sign in in the Safari "
                  "window, then press Enter... ")

        for company in companies:
            url = (
                "https://www.linkedin.com/search/results/people/"
                f"?keywords={company.replace(' ', '%20')}%20recruiter"
                "&origin=GLOBAL_SEARCH_HEADER"
            )
            print(f"\n[li] ▶ {company}: {url}")
            await asyncio.to_thread(safari.goto, wid, url)
            await asyncio.sleep(random.uniform(4, 7))

            try:
                cur = await asyncio.to_thread(safari.current_url, wid)
            except RuntimeError:
                cur = ""
            if "/login" in cur or "checkpoint" in cur:
                input("[li] Login/checkpoint detected. Resolve it in the "
                      "Safari window, then press Enter... ")
                await asyncio.to_thread(safari.goto, wid, url)
                await asyncio.sleep(random.uniform(4, 7))

            found = await _scrape_results(wid, company)
            print(f"[li] {company}: saved {found} recruiter(s)")
            await asyncio.sleep(random.uniform(8, 20))  # slow pacing
    finally:
        input("\n[li] Done collecting. Press Enter to close Safari window... ")
        await asyncio.to_thread(safari.close_window, wid)


async def _scrape_results(wid: int, company: str) -> int:
    """Pull visible people-search result cards and store them."""
    try:
        out = await asyncio.to_thread(
            safari.js, wid, SEARCH_JS.replace("__N__", str(RESULTS_PER_SEARCH)))
        cards = json.loads(out)
    except (RuntimeError, json.JSONDecodeError):
        return 0

    saved = 0
    for card in cards:
        try:
            profile_url = (card.get("url") or "").strip()
            name = (card.get("name") or "").strip()
            title = (card.get("title") or "").strip()
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
