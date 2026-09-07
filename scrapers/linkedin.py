"""
LinkedIn recruiter finder — real Safari via AppleScript.

Finds actual hiring people (not just names): people-search per company
with query variants, title filtering (recruiters / talent / campus only),
saved into data/recruiters.json ready for drafts + invites.

Sources: applied jobs by default; --company NAME targets anyone;
--wider includes seen/new jobs. Posture: one Safari window, slow
pacing, hard cap of 5 companies per run.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from datetime import datetime

from browser import safari_bridge as safari
import config
from scrapers.base import BaseScraper
from storage.jobs import load_jobs
from storage.recruiters import upsert

MAX_COMPANIES_PER_RUN = 5      # hard cap regardless of config
RESULTS_PER_SEARCH = 5

# Only these titles are worth messaging. Everyone else is a skip.
TITLE_KEEP = re.compile(
    r"recruit|talent acquisition|sourc|talent|campus|"
    r"university relations|early careers|early talent|\bhiring\b|"
    r"\bpeople\b",
    re.IGNORECASE,
)

QUERIES = ["{c} recruiter"]
QUERIES_DEEP = ["{c} recruiter", "{c} campus recruiter",
                "{c} talent acquisition"]


def _pause(msg: str) -> None:
    """Enter-prompt that degrades gracefully without a tty."""
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        print("(no terminal — continuing)")

SEARCH_JS = """(() => {
  const seen = {};
  const out = [];
  document.querySelectorAll("a[href*='/in/']").forEach(a => {
    let href = (a.getAttribute("href") || "").split("?")[0];
    if (!href) return;
    if (href.indexOf("/in/") === 0)
      href = "https://www.linkedin.com" + href;
    if (!/^https:\/\/www\.linkedin\.com\/in\/[\w-]+\/?$/.test(href)) return;
    if (seen[href]) return;
    seen[href] = 1;
    // Skip off-screen/virtualized duplicates (stale content).
    if (a.offsetParent === null) return;
    let block = a;
    for (let i = 0; i < 6 && block; i++) {
      const t = (block.innerText || "").replace(/\\s+/g, " ").trim();
      // "Megan Costa • 2nd Junior Recruiter ... | ..." — split on the
      // ASCII degree marker (bullet glyphs vary), title runs to "|" or end.
      const m = t.match(/^(.*?)\s+(1st|2nd|3rd\+?)\s+(.*)$/);
      if (t.length > 30 && m) {
        const name = m[1].replace(/[^A-Za-z0-9)\]]+$/g, "").trim();
        let title = m[3].split("|")[0];
        title = title.split(/ Follow | Past:| Current:/)[0].trim()
          .slice(0, 120);
        if (name && title) {
          out.push({url: href, name: name.slice(0, 80), title});
          break;
        }
      }
      block = block.parentElement;
    }
  });
  return JSON.stringify(out.slice(0, __N__));
})()"""


def _companies_with_applied_jobs() -> list[str]:
    jobs = load_jobs()
    companies = {
        j.company for j in jobs.values()
        if j.platform == "sheridan" and j.status == "applied" and j.company
    }
    return sorted(companies)


def _companies_wide() -> list[str]:
    """Applied first, then seen/new — for connecting ahead of applying."""
    jobs = load_jobs()
    rank = {"applied": 0, "seen": 1, "new": 2}
    best: dict[str, int] = {}
    for j in jobs.values():
        if not j.company or j.status not in rank:
            continue
        best[j.company] = min(best.get(j.company, 9), rank[j.status])
    return sorted(best, key=lambda c: (best[c], c))


class LinkedInCollector(BaseScraper):

    async def scrape(self) -> None:  # keep BaseScraper interface
        await collect()

    async def _url(self, wid: int) -> str:
        try:
            return await asyncio.to_thread(safari.current_url, wid)
        except RuntimeError:
            return ""


async def collect(companies: list[str] | None = None,
                  deep: bool = False) -> None:
    if not config.enabled("linkedin_collect"):
        print("[li] linkedin_collect is OFF — enable it in config.yaml")
        return

    queries = QUERIES_DEEP if deep else QUERIES

    if companies:
        targets = companies[:MAX_COMPANIES_PER_RUN]
    elif config.dry_run():
        targets = _companies_with_applied_jobs()
        print(f"[li] DRY RUN — would search recruiters at "
              f"{len(targets)} compan(ies): {', '.join(targets) or '(none)'}")
        return
    else:
        targets = _companies_with_applied_jobs()[:MAX_COMPANIES_PER_RUN]
        if not targets:
            print("[li] No applied Sheridan jobs yet — apply first, or pass "
                  "--company NAME / --wider.")
            return

    print(f"[li] Searching recruiters at {len(targets)} compan(ies): "
          f"{', '.join(targets)}"
          + (" [deep: 3 queries each]" if deep else ""))

    wid = await asyncio.to_thread(
        safari.open_window, "https://www.linkedin.com/feed/")
    await asyncio.sleep(4)
    try:
        try:
            url = await asyncio.to_thread(safari.current_url, wid)
        except RuntimeError:
            url = ""
        if "/login" in url or "checkpoint" in url:
            _pause("[li] LinkedIn wants login. Sign in in the Safari "
                   "window, then press Enter... ")

        for company in targets:
            for qi, q in enumerate(queries):
                url = (
                    "https://www.linkedin.com/search/results/people/"
                    f"?keywords={q.format(c=company).replace(' ', '%20')}"
                    "&origin=GLOBAL_SEARCH_HEADER"
                )
                print(f"\n[li] ▶ {company} [{qi + 1}/{len(queries)}]: "
                      f"{q.format(c=company)}")
                await asyncio.to_thread(safari.goto, wid, url)
                await asyncio.sleep(random.uniform(4, 7))

                try:
                    cur = await asyncio.to_thread(safari.current_url, wid)
                except RuntimeError:
                    cur = ""
                if "/login" in cur or "checkpoint" in cur:
                    _pause("[li] Login/checkpoint detected. Resolve it in "
                           "the Safari window, then press Enter... ")
                    await asyncio.to_thread(safari.goto, wid, url)
                    await asyncio.sleep(random.uniform(4, 7))

                found = await _scrape_results(wid, company)
                print(f"[li] {company}: saved {found} recruiter(s)")
                await asyncio.sleep(random.uniform(8, 20))  # slow pacing
    finally:
        _pause("\n[li] Done collecting. Press Enter to close Safari "
               "window... ")
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
            if not TITLE_KEEP.search(title):
                continue  # not a hiring role — skip
            r_id = hashlib.sha256(profile_url.encode()).hexdigest()[:12]
            upsert(r_id, {
                "name": name,
                "title": title,
                "company": company,
                "profile_url": profile_url,
                "date_found": datetime.now().isoformat(timespec="seconds"),
                "draft": None,
                "draft_date": None,
                "connect_note": None,
                "connect_note_date": None,
                "invite_date": None,
                "sent_date": None,
            })
            saved += 1
        except Exception:
            continue
    return saved


if __name__ == "__main__":
    asyncio.run(collect())
