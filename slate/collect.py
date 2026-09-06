"""
SLATE (Brightspace/D2L at slate.sheridancollege.ca) writing collector.

Backs up the student's own essays, assignment submissions, and discussion
posts into data/writing/ so a later step (slate/style.py) can distill
their voice for resume/outreach personalization.

Posture: real Safari window via browser.safari_bridge (never playwright),
same Entra/Microsoft SSO as scrapers/sheridan.py, slow pacing between
courses, manual-fallback prompts when a login wall appears.

Only SLATE_ROOT is hardcoded. Every course/dropbox/discussion path is
discovered at runtime from page anchors, with multiple selector fallbacks;
anything unrecognized is skipped with a log line.

Layout on disk:
  data/writing/<course-slug>/posts.md        own discussion posts
  data/writing/<course-slug>/<files...>      downloaded submissions
  data/writing/manifest.json                 course/item/type/path/date rows

Toggle: config.yaml -> toggles.slate_collect (default OFF)
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
from datetime import datetime
from pathlib import Path

from browser import safari_bridge as safari
import config
from scrapers.base import BaseScraper

SLATE_ROOT = "https://slate.sheridancollege.ca"
SLATE_HOST = "slate.sheridancollege.ca"
MS_LOGIN_HOST = "login.microsoftonline.com"

ROOT = Path(__file__).parent.parent
WRITING_DIR = ROOT / "data" / "writing"
MANIFEST_PATH = WRITING_DIR / "manifest.json"
DOWNLOADS_DIR = Path.home() / "Downloads"

MAX_TOPICS_PER_COURSE = 5   # discussion threads visited per course
MAX_DOWNLOADS_PER_FOLDER = 3  # download candidates clicked per folder

# Dump page anchors for runtime link discovery (no hardcoded deep URLs).
ANCHORS_JS = """(() => JSON.stringify(
  [...document.querySelectorAll("a[href]")].slice(0, 600)
    .map(a => ({
      text: (a.innerText || "").trim().slice(0, 120),
      href: a.href || "",
    }))))()"""

# Candidate discussion-post containers (several D2L layouts).
POSTS_JS = """(() => JSON.stringify(
  [...document.querySelectorAll(
    "article, .d_post, [class*=discussion-post], [class*=d2l-discussion], div[class*=post]"
  )].slice(0, 100).map(el => {
    const a = el.querySelector(
      "[class*=author], .d_author, a[href*=profile], header");
    return {
      author: a ? (a.innerText || "").trim().slice(0, 120) : "",
      body: (el.innerText || "").trim().slice(0, 4000),
    };
  }).filter(p => p.body)))()"""

# Candidate inline submission-text blocks (text-entry dropbox submissions).
SUBMISSION_TEXT_JS = """(() => JSON.stringify(
  [...document.querySelectorAll(
    ".d2l-dropbox-submission, [class*=dropbox-submission], " +
    "[class*=submission], .d2l-htmlblock"
  )].slice(0, 20)
    .map(el => (el.innerText || "").trim().slice(0, 6000))
    .filter(t => t)))()"""


def _click_href_js(href: str) -> str:
    """JS that clicks the anchor whose href contains the given string."""
    return (
        "((sub) => { const a = [...document.querySelectorAll('a[href]')]"
        " .find(x => (x.href || '').toLowerCase()"
        " .includes(sub.toLowerCase()));"
        " if (!a) return 'miss';"
        " a.scrollIntoView({block: 'center'}); a.click();"
        " return 'clicked'; })"
        f"({json.dumps(href)})"
    )


def _safe_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "course")[:60]


def _is_own_post(author: str, applicant: str) -> bool:
    """Author match on full name, or first+last both present."""
    if not author or not applicant:
        return False
    a, want = author.lower(), applicant.lower()
    if want in a:
        return True
    parts = [p for p in re.split(r"\s+", want) if len(p) > 1]
    return len(parts) >= 2 and parts[0] in a and parts[-1] in a


class SlateCollector(BaseScraper):

    async def scrape(self) -> None:  # keep BaseScraper interface
        await collect()

    async def _url(self, wid: int) -> bool | str:
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


def _is_logged_in(url: str) -> bool:
    blocked = [MS_LOGIN_HOST, "Shibboleth", "adfs", "/login"]
    return SLATE_HOST in url and not any(b in url for b in blocked)


async def collect() -> None:
    if not config.enabled("slate_collect"):
        print("[slate] slate_collect is OFF — enable it in config.yaml")
        return

    if config.dry_run():
        print("[slate] DRY RUN — would open SLATE, enumerate courses, "
              "download own submissions + scrape own discussion posts "
              "into data/writing/")
        return

    applicant = os.environ.get("APPLICANT_NAME", "").strip()
    if not applicant:
        print("[slate] WARNING: APPLICANT_NAME not set in .env — "
              "discussion-post author matching will save nothing.")

    col = SlateCollector()
    manifest: list[dict] = _load_manifest()

    wid = await asyncio.to_thread(safari.open_window, SLATE_ROOT)
    print("[slate] Safari window opened.")
    await asyncio.sleep(4)
    try:
        try:
            url = await asyncio.to_thread(safari.current_url, wid)
        except RuntimeError:
            url = ""
        if _is_logged_in(url):
            print("[slate] Session still valid — skipping login.")
        else:
            print("[slate] Not logged in. Starting SSO login...")
            await _do_sso_login(col, wid)
            try:
                url = await asyncio.to_thread(safari.current_url, wid)
            except RuntimeError:
                url = ""
            if not _is_logged_in(url):
                input("[slate] Still on a login page. Finish signing in "
                      "in the Safari window, then press Enter... ")

        courses = await _list_courses(col, wid)
        if not courses:
            input("[slate] No courses found. In the Safari window open "
                  "your course list (e.g. waffle/Course Selector), "
                  "then press Enter... ")
            courses = await _list_courses(col, wid)
        print(f"[slate] {len(courses)} course(s) found.")
        if not courses:
            print("[slate] Nothing to collect — leaving.")
            return

        for idx, course in enumerate(courses, 1):
            print(f"\n[slate] ▶ [{idx}/{len(courses)}] {course['name']}")
            try:
                await _collect_course(col, wid, course, applicant, manifest)
            except RuntimeError:
                print("[slate] Safari window went away — stopping.")
                break
            except Exception as e:
                print(f"[slate] Course error ({course['name']}): {e}")
            await asyncio.sleep(random.uniform(8, 20))  # slow pacing

        _save_manifest(manifest)
        print(f"\n[slate] Done. Manifest: {MANIFEST_PATH} "
              f"({len(manifest)} item(s))")
    finally:
        input("\n[slate] Done collecting. Press Enter to close Safari "
              "window... ")
        await asyncio.to_thread(safari.close_window, wid)


async def _do_sso_login(col: SlateCollector, wid: int) -> None:
    """Entra/Microsoft SSO, adapted from scrapers/sheridan.py."""
    username = os.environ.get("SHERIDAN_USERNAME", "")
    password = os.environ.get("SHERIDAN_PASSWORD", "")
    await asyncio.to_thread(safari.goto, wid, SLATE_ROOT)
    await col.human_delay(1500, 2500)

    # Slate may show its own login link first — try several shapes.
    await col.safari_click(wid, [
        "a[href*=login i]", ".d2l-login-button", "button[class*=login i]",
    ])
    try:  # fallback: click by visible text
        await asyncio.to_thread(safari.js, wid, """(() => {
          const a = [...document.querySelectorAll("a, button")]
            .find(e => /log ?in|sign ?in/i.test(e.innerText || ""));
          if (a) { a.click(); return "clicked"; }
          return "miss";
        })()""")
    except RuntimeError:
        return
    print("[slate] Waiting for Microsoft login...")
    await col._wait_url(wid, MS_LOGIN_HOST, 20)
    await col.human_delay(1500, 2500)

    print("[slate] Step 1/3 — entering email...")
    if await col._poll_js(wid, _present_js(
            "input[name='loginfmt'], input[type='email']"), "yes", 15):
        await col.safari_fill(wid, ["input[name='loginfmt']",
                                    "input[type='email']"], username)
        await col.human_delay(500, 900)
        await col.safari_click(wid, ["#idSIButton9",
                                     "button[type='submit']"])
        await col.human_delay(1500, 2500)

    print("[slate] Step 2/3 — entering password...")
    if await col._poll_js(wid, _present_js(
            "input[name='passwd'], input[type='password']"), "yes", 15):
        await col.safari_fill(wid, ["input[name='passwd']",
                                    "input[type='password']"], password)
        await col.human_delay(500, 900)
        await col.safari_click(wid, ["#idSIButton9",
                                     "button[type='submit']"])
        await col.human_delay(2000, 3000)

    await _handle_mfa_if_needed(col, wid)

    try:  # "Stay signed in?" → Yes
        if await col.safari_present(wid, ["#idSIButton9"]):
            print("[slate] Step 3/3 — confirming 'Stay signed in'...")
            await col.safari_click(wid, ["#idSIButton9"])
            await col.human_delay(1000, 2000)
    except RuntimeError:
        pass

    print("[slate] Waiting for redirect back to SLATE...")
    await col._wait_url(wid, SLATE_HOST, 30)
    await col.human_delay(1500, 2500)
    print("[slate] Login complete.")


async def _handle_mfa_if_needed(col: SlateCollector, wid: int) -> None:
    for sel in ("input[name='otc']", "#idRichContext",
                "#idDiv_SAOTCS_Proofs"):
        try:
            if await col.safari_present(wid, [sel]):
                print("\n[slate] *** MFA required ***")
                print("[slate]     Approve the sign-in on your "
                      "Authenticator app (up to 90 s)...\n")
                for _ in range(180):
                    out = (await asyncio.to_thread(
                        safari.js, wid, _absent_js(sel))).strip().strip('"')
                    if out == "yes":
                        return
                    await asyncio.sleep(0.5)
                return
        except RuntimeError:
            return


async def _anchors(col: SlateCollector, wid: int) -> list[dict]:
    try:
        out = await asyncio.to_thread(safari.js, wid, ANCHORS_JS)
        links = json.loads(out)
        return [l for l in links if l.get("href")]
    except (RuntimeError, json.JSONDecodeError, AttributeError):
        return []


async def _list_courses(col: SlateCollector, wid: int) -> list[dict]:
    """Enrolled courses, discovered from hub-page anchors."""
    await asyncio.to_thread(safari.goto, wid, SLATE_ROOT)
    await col.human_delay(2500, 4000)
    links = await _anchors(col, wid)
    seen: dict[str, dict] = {}
    for link in links:
        href = link["href"]
        if "/d2l/home/" not in href and "/d2l/le/content/" not in href:
            continue
        m = re.search(r"[?&]ou=(\d+)", href)
        key = m.group(1) if m else href.split("?")[0]
        if key not in seen:
            name = link["text"] or f"course-{key[-8:]}"
            seen[key] = {"name": name, "url": href.split("?")[0] + (
                f"?ou={key}" if m else "")}
    return list(seen.values())


async def _collect_course(col: SlateCollector, wid: int, course: dict,
                          applicant: str, manifest: list[dict]) -> None:
    slug = _safe_slug(course["name"])
    course_dir = WRITING_DIR / slug
    course_dir.mkdir(parents=True, exist_ok=True)

    await asyncio.to_thread(safari.goto, wid, course["url"])
    await col.human_delay(2500, 4000)
    links = await _anchors(col, wid)
    if not links:
        print(f"[slate]   {course['name']}: unreadable layout — skipped.")
        return

    dropbox = [l for l in links
               if "dropbox" in l["href"].lower()
               or re.search(r"drop\s?box|assignments?(\s+folders?)?|"
                            r"submissions?", l["text"], re.I)]
    discuss = [l for l in links
               if "discuss" in l["href"].lower()
               or re.search(r"discussions?", l["text"], re.I)]
    print(f"[slate]   dropbox link(s): {len(dropbox)}, "
          f"discussion link(s): {len(discuss)}")

    for link in _dedupe_links(dropbox):
        await _collect_dropbox(col, wid, course, course_dir, link, manifest)
    await _collect_discussions(col, wid, course, course_dir, applicant,
                               _dedupe_links(discuss), manifest)


async def _collect_dropbox(col: SlateCollector, wid: int, course: dict,
                           course_dir: Path, link: dict,
                           manifest: list[dict]) -> None:
    await asyncio.to_thread(safari.goto, wid, link["href"])
    await col.human_delay(2000, 3500)
    links = await _anchors(col, wid)
    folders = [l for l in links if "dropbox" in l["href"].lower()
               and l["href"] != link["href"]]
    pages = _dedupe_links(folders) or [link]
    for page in pages:
        try:
            await asyncio.to_thread(safari.goto, wid, page["href"])
            await col.human_delay(2000, 3500)
            await _save_submission_text(col, wid, course, course_dir,
                                        page, manifest)
            await _save_downloads(col, wid, course, course_dir,
                                  page, manifest)
        except RuntimeError:
            raise
        except Exception as e:
            print(f"[slate]   folder error "
                  f"({(page.get('text') or page['href'])[:60]}): {e}")


async def _save_submission_text(col: SlateCollector, wid: int, course: dict,
                                course_dir: Path, page: dict,
                                manifest: list[dict]) -> None:
    try:
        out = await asyncio.to_thread(safari.js, wid, SUBMISSION_TEXT_JS)
        blocks = json.loads(out)
    except (RuntimeError, json.JSONDecodeError):
        return
    text = "\n\n---\n\n".join(b for b in blocks if b.strip())
    if not text.strip():
        return
    fname = _safe_slug(page.get("text") or "submission") + ".txt"
    dest = _unique_path(course_dir / fname)
    dest.write_text(f"# {course['name']}\n# {page['href']}\n\n{text}\n")
    _record(manifest, course["name"], dest, "submission-text")
    print(f"[slate]   saved submission text: {dest.name}")


async def _save_downloads(col: SlateCollector, wid: int, course: dict,
                          course_dir: Path, page: dict,
                          manifest: list[dict]) -> None:
    links = await _anchors(col, wid)
    cands = [l for l in links
             if "download" in l["href"].lower()
             or re.search(r"download|my submission|view submission|"
                          r"feedback|attachment", l["text"], re.I)]
    for cand in _dedupe_links(cands)[:MAX_DOWNLOADS_PER_FOLDER]:
        before = _download_snapshot()
        try:
            out = (await asyncio.to_thread(
                safari.js, wid, _click_href_js(cand["href"]))
                ).strip().strip('"')
        except RuntimeError:
            raise
        if out != "clicked":
            continue
        found = await _wait_new_download(before)
        if not found:
            continue
        dest = _unique_path(course_dir / found.name)
        try:
            shutil.move(str(found), str(dest))
        except OSError as e:
            print(f"[slate]   move failed ({found.name}): {e}")
            continue
        _record(manifest, course["name"], dest, "submission")
        print(f"[slate]   saved submission file: {dest.name}")
        await col.human_delay(1500, 3000)


async def _collect_discussions(col: SlateCollector, wid: int, course: dict,
                               course_dir: Path, applicant: str,
                               discuss_links: list[dict],
                               manifest: list[dict]) -> None:
    if not discuss_links:
        return
    index = discuss_links[0]
    await asyncio.to_thread(safari.goto, wid, index["href"])
    await col.human_delay(2000, 3500)
    try:
        cur = await asyncio.to_thread(safari.current_url, wid)
    except RuntimeError:
        cur = index["href"]
    links = await _anchors(col, wid)
    topics = [l for l in links if "discuss" in l["href"].lower()
              and l["href"] != cur]
    pages = [index] + _dedupe_links(topics)[:MAX_TOPICS_PER_COURSE]

    posts_path = course_dir / "posts.md"
    seen_posts: set[tuple[str, str]] = set()
    own = 0
    for page in pages:
        try:
            await asyncio.to_thread(safari.goto, wid, page["href"])
            await col.human_delay(2000, 3500)
            out = await asyncio.to_thread(safari.js, wid, POSTS_JS)
            candidates = json.loads(out)
        except (RuntimeError, json.JSONDecodeError):
            continue
        with posts_path.open("a") as f:
            for post in candidates:
                author = (post.get("author") or "").strip()
                body = (post.get("body") or "").strip()
                if not body or not _is_own_post(author, applicant):
                    continue
                key = (author, body[:100])
                if key in seen_posts:
                    continue
                seen_posts.add(key)
                f.write(f"\n## {course['name']} — {author}\n"
                        f"Source: {page['href']}\n\n{body}\n")
                own += 1
    if own:
        _record(manifest, course["name"], posts_path, "post-export")
    print(f"[slate]   own discussion post(s): {own}")


# ------------------------------------------------------------------
# Downloads + manifest bookkeeping
# ------------------------------------------------------------------

def _download_snapshot() -> set[str]:
    try:
        return {p.name for p in DOWNLOADS_DIR.iterdir()}
    except OSError:
        return set()


async def _wait_new_download(before: set[str],
                             timeout_s: int = 25) -> Path | None:
    """Newest finished file in ~/Downloads that wasn't there before."""
    for _ in range(timeout_s * 2):
        await asyncio.sleep(0.5)
        try:
            cands = [p for p in DOWNLOADS_DIR.iterdir()
                     if p.name not in before and p.is_file()
                     and p.suffix.lower() not in (
                         ".crdownload", ".part", ".download")]
        except OSError:
            continue
        if cands:
            await asyncio.sleep(2)  # let the write finish
            return max(cands, key=lambda p: p.stat().st_mtime)
    return None


def _unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for i in range(2, 100):
        alt = dest.with_name(f"{dest.stem}-{i}{dest.suffix}")
        if not alt.exists():
            return alt
    return dest


def _dedupe_links(links: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for link in links:
        href = (link.get("href") or "").strip()
        if href and href not in seen:
            seen[href] = link
    return list(seen.values())


def _record(manifest: list[dict], course: str, path: Path,
            kind: str) -> None:
    try:
        rel = str(path.relative_to(ROOT))
    except ValueError:
        rel = str(path)
    row = {"course": course,
           "item": path.name,
           "type": kind,
           "path": rel,
           "date": datetime.now().isoformat(timespec="seconds")}
    for i, existing in enumerate(manifest):
        if existing.get("path") == rel:
            manifest[i] = row
            return
    manifest.append(row)


def _load_manifest() -> list[dict]:
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save_manifest(manifest: list[dict]) -> None:
    WRITING_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


def _present_js(selectors: str) -> str:
    return ("(() => [...document.querySelectorAll("
            f"{json.dumps(selectors)})].some(e => e && "
            'e.offsetParent !== null) ? "yes" : "no")()')


def _absent_js(selector: str) -> str:
    return ("(() => !document.querySelector("
            f"{json.dumps(selector)}) ? \"yes\" : \"no\")()")


if __name__ == "__main__":
    asyncio.run(collect())
