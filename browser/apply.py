"""
Application form assistant.

Opens the job's URL(s) in a real, visible browser window, pre-fills common
text fields from the applicant profile in .env, attempts to attach a resume
PDF, then waits for the user to review everything and submit manually.

Dual-tab support:
  - If a Sheridan job also has an external_url (e.g. a Workday link), both
    the Sheridan posting page and the external form are opened in the SAME
    browser window as separate tabs.  Fields are pre-filled on both.
  - The external tab is brought to the foreground so it is the first thing
    the user sees.

DEFAULT GUARANTEE: This module NEVER clicks Submit, Apply, Send, or any
other form submission button — UNLESS toggles.auto_submit is enabled in
config.yaml (and dry_run is off). Even then it only submits when its
sanity checks pass; any doubt = no click, user stays in control.
"""
from __future__ import annotations
import json
import os
import random
import re
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

import config
from models.job import Job

# ---------------------------------------------------------------------------
# Applicant profile — loaded from environment at import time
# ---------------------------------------------------------------------------
_name_parts = os.environ.get("APPLICANT_NAME", "").split(None, 1)
_first = _name_parts[0] if _name_parts else ""
_last = _name_parts[1] if len(_name_parts) > 1 else ""

PROFILE = {
    "first_name":    _first,
    "last_name":     _last,
    "full_name":     os.environ.get("APPLICANT_NAME", ""),
    "email":         os.environ.get("APPLICANT_EMAIL", ""),
    "phone":         os.environ.get("APPLICANT_PHONE", ""),
    "phone_digits":  os.environ.get("APPLICANT_PHONE_DIGITS", ""),
    "linkedin":      os.environ.get("APPLICANT_LINKEDIN", ""),
    "github":        os.environ.get("APPLICANT_GITHUB", ""),
    "location":      os.environ.get("APPLICANT_LOCATION", ""),
    "availability":  os.environ.get("APPLICANT_AVAILABILITY", ""),
    "resume_path":   os.environ.get("APPLICANT_RESUME_PATH", ""),
}

# ---------------------------------------------------------------------------
# Field matching rules (generic — name / id / placeholder / label text).
# ---------------------------------------------------------------------------
FIELD_PATTERNS: list[tuple[list[str], str]] = [
    (["first[_\\s-]?name", "fname", "given[_\\s-]?name"],          PROFILE["first_name"]),
    (["last[_\\s-]?name",  "lname", "family[_\\s-]?name",
      "surname"],                                                    PROFILE["last_name"]),
    (["full[_\\s-]?name",  "your[_\\s-]?name", "applicant[_\\s-]?name",
      "^name$"],                                                     PROFILE["full_name"]),
    (["e[_-]?mail"],                                                 PROFILE["email"]),
    (["phone", "mobile", "tel", "cell"],                             PROFILE["phone"]),
    (["linkedin"],                                                   PROFILE["linkedin"]),
    (["github", "git[_\\s-]?hub"],                                   PROFILE["github"]),
    (["location", "city", "address", "postal", "zip"],               PROFILE["location"]),
    (["start[_\\s-]?date", "available", "availability",
      "when[_\\s-]?can[_\\s-]?you"],                                 PROFILE["availability"]),
]

# ---------------------------------------------------------------------------
# Workday-specific automation-id selectors
# Workday renders inputs as <input data-automation-id="...">.
# We try these directly with fill() before the generic pattern scan.
# ---------------------------------------------------------------------------
WORKDAY_SELECTORS: list[tuple[str, str]] = [
    # Personal info
    ('[data-automation-id="legalNameSection_firstName"]',   PROFILE["first_name"]),
    ('[data-automation-id="legalNameSection_lastName"]',    PROFILE["last_name"]),
    ('[data-automation-id="email"]',                        PROFILE["email"]),
    ('[data-automation-id="phone"]',                        PROFILE["phone"]),
    ('[data-automation-id="phoneNumber"]',                  PROFILE["phone"]),
    ('[data-automation-id="addressSection_addressLine1"]',  PROFILE["location"]),
    ('[data-automation-id="city"]',                         "Oakville"),
    # Social / portfolio
    ('[data-automation-id="linkedin"]',                     PROFILE["linkedin"]),
    ('[data-automation-id="linkedInUrl"]',                  PROFILE["linkedin"]),
    ('[data-automation-id="portfolioUrl"]',                 PROFILE["github"]),
    ('[data-automation-id="websiteUrl"]',                   PROFILE["github"]),
    # Cover letter / other text areas
    ('[data-automation-id="coverLetter"]',                  ""),   # leave blank
]

SHERIDAN_COOKIES = Path(__file__).parent.parent / "data" / "sheridan_cookies.json"

# External-apply link patterns — used to auto-detect 3rd-party URLs from
# the Sheridan posting page (Workday, Greenhouse, Lever, etc.)
_EXTERNAL_DOMAINS = re.compile(
    r"(myworkdayjobs\.com|greenhouse\.io|lever\.co|jobvite\.com"
    r"|smartrecruiters\.com|taleo\.net|icims\.com|ashbyhq\.com"
    r"|bamboohr\.com|breezy\.hr|recruitee\.com)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def open_and_prefill(job: Job, resume_path: Path | None = None) -> None:
    """
    Opens the job application URL(s) in a maximised browser.
    - If job.external_url is set, opens BOTH pages (two tabs).
    - Pre-fills known fields on every tab opened.
    - Attaches `resume_path` (per-job tailored PDF) when given, else the
      .env master resume.
    - With toggles.auto_submit ON (and dry_run off), attempts a
      sanity-checked auto-submit; otherwise waits for the user.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = await browser.new_context(no_viewport=True)

        # Restore Sheridan Works session
        if job.platform == "sheridan" and SHERIDAN_COOKIES.exists():
            cookies = json.loads(SHERIDAN_COOKIES.read_text())
            await context.add_cookies(cookies)

        # ── Tab 1: Sheridan posting page ────────────────────────────────────
        sheridan_page = await context.new_page()
        print(f"\n[apply] Opening Sheridan posting: {job.url}")

        detected_external: str | None = None  # auto-detected link

        if job.platform == "sheridan" and "#posting" in job.url:
            base_url   = job.url.split("#")[0]
            posting_id = job.url.split("#posting")[-1]
            try:
                await sheridan_page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                await sheridan_page.wait_for_timeout(3000)
                await sheridan_page.evaluate("""() => {
                    const a = document.querySelector('.stat-table a[onclick*="displayQuickSearch"]');
                    if (a) a.click();
                }""")
                await sheridan_page.wait_for_selector(
                    f".np-apply-btn-{posting_id}", timeout=12000
                )
                await sheridan_page.click(f".np-apply-btn-{posting_id}")
                await sheridan_page.wait_for_timeout(3000)
                print(f"[apply] Opened posting {posting_id} in Sheridan Works.")

                # Try to auto-detect an external application link
                detected_external = await _find_external_link(sheridan_page)
                if detected_external:
                    print(f"[apply] Auto-detected external link: {detected_external}")

            except Exception as e:
                print(f"[apply] Sheridan SPA navigation error: {e}")
                await sheridan_page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        else:
            try:
                await sheridan_page.goto(job.url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[apply] Page load warning: {e}")

        await sheridan_page.wait_for_timeout(2500)
        s_filled = await _prefill_text_fields(sheridan_page)
        await _attach_resume(sheridan_page, resume_path)
        await _maybe_autosubmit(sheridan_page, s_filled, "Sheridan")

        # ── Tab 2: external application form ───────────────────────────────
        ext_url = job.external_url or detected_external
        if ext_url:
            print(f"[apply] Opening external application: {ext_url}")
            ext_page = await context.new_page()
            try:
                await ext_page.goto(ext_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"[apply] External page load warning: {e}")

            await ext_page.wait_for_timeout(3000)

            # Workday-specific fill first, then generic
            wd_filled = await _prefill_workday(ext_page)
            gen_filled = await _prefill_text_fields(ext_page)
            await _attach_resume(ext_page, resume_path)

            total_ext = wd_filled + gen_filled
            print(f"[apply] External form: pre-filled {total_ext} field(s).")
            await _maybe_autosubmit(ext_page, total_ext, "External")

            # Bring the external tab to front so user sees it immediately
            await ext_page.bring_to_front()
        else:
            print("[apply] No external URL set. Only Sheridan posting is open.")
            print("[apply]   → Use: python main.py set-url <job_id> <url>  to add one.")

        print(f"[apply] Sheridan tab: pre-filled {s_filled} field(s).")
        print("[apply] Review everything carefully before submitting.")
        print("[apply] Close the browser when you are done.\n")

        # ── Wait for ALL pages to be closed ────────────────────────────────
        # We watch for the context to have no pages left.
        while True:
            try:
                await context.pages[0].wait_for_event("close", timeout=5000)
            except Exception:
                pass
            if not context.pages:
                break

        print("[apply] Browser closed.")
        await browser.close()


# ---------------------------------------------------------------------------
# Auto-detect external apply link
# ---------------------------------------------------------------------------

async def _find_external_link(page: Page) -> str | None:
    """
    Scans the page for links or buttons whose href / onclick leads to a
    known external ATS (Workday, Greenhouse, Lever, etc.).
    Returns the first match, or None.
    """
    try:
        links = await page.query_selector_all("a[href]")
        for link in links:
            href = (await link.get_attribute("href") or "").strip()
            if _EXTERNAL_DOMAINS.search(href):
                return href

        # Also check onclick attributes for JS window.open(...) patterns
        all_els = await page.query_selector_all("[onclick]")
        for el in all_els:
            onclick = (await el.get_attribute("onclick") or "")
            match = re.search(r"['\"]?(https?://[^'\")\s]+)['\"]?", onclick)
            if match and _EXTERNAL_DOMAINS.search(match.group(1)):
                return match.group(1)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Workday prefill
# ---------------------------------------------------------------------------

async def _prefill_workday(page: Page) -> int:
    """
    Fills Workday-specific data-automation-id fields.
    Returns count of fields filled.
    """
    filled = 0
    for selector, value in WORKDAY_SELECTORS:
        if not value:
            continue
        try:
            el = page.locator(selector).first
            if await el.count() == 0:
                continue
            if not await el.is_visible(timeout=1000):
                continue
            current = await el.input_value()
            if not current:
                await el.fill(value)
                filled += 1
        except Exception:
            continue
    return filled


# ---------------------------------------------------------------------------
# Generic text-field prefill
# ---------------------------------------------------------------------------

async def _prefill_text_fields(page: Page) -> int:
    """
    Iterates over visible text/email/tel/url inputs and textareas.
    Fills empty fields whose attributes match a known profile pattern.
    Returns the count of fields filled.
    """
    filled = 0
    inputs = await page.query_selector_all(
        "input:not([type='hidden'])"
        ":not([type='submit']):not([type='button'])"
        ":not([type='checkbox']):not([type='radio'])"
        ":not([type='file']), "
        "textarea"
    )

    for el in inputs:
        try:
            if not await el.is_visible():
                continue

            name        = (await el.get_attribute("name") or "").lower()
            id_attr     = (await el.get_attribute("id") or "").lower()
            placeholder = (await el.get_attribute("placeholder") or "").lower()
            label_text  = await _get_label_text(page, el)
            field_sig   = f"{name} {id_attr} {placeholder} {label_text}"

            for patterns, value in FIELD_PATTERNS:
                if not value:
                    continue
                if any(re.search(pat, field_sig, re.IGNORECASE) for pat in patterns):
                    current = await el.input_value()
                    if not current:
                        await el.fill(value)
                        filled += 1
                    break
        except Exception:
            continue

    return filled


# ---------------------------------------------------------------------------
# Resume attachment
# ---------------------------------------------------------------------------

async def _attach_resume(page: Page, resume_path: str | Path | None = None) -> bool:
    """
    Attempts to set the resume file on a standard <input type="file"> element.
    Uses the passed-in path (per-job tailored PDF) or falls back to .env.
    Returns True if attached, False otherwise.
    """
    resume_path = resume_path or PROFILE["resume_path"]
    if not resume_path:
        return False

    path = Path(resume_path)
    if not path.exists():
        print(f"[apply] Resume file not found: {resume_path}")
        return False

    file_inputs = await page.query_selector_all("input[type='file']")
    for el in file_inputs:
        try:
            if not await el.is_visible():
                continue
            accept = (await el.get_attribute("accept") or "").lower()
            if accept and "pdf" not in accept and "doc" not in accept and "*" not in accept:
                continue
            await el.set_input_files(str(path))
            print(f"[apply] Resume attached: {path.name}")
            return True
        except Exception as e:
            print(f"[apply] Resume attach error: {e}")

    return False


# ---------------------------------------------------------------------------
# Sanity-gated auto-submit (only when toggles.auto_submit is ON)
# ---------------------------------------------------------------------------

_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Submit')",
    "button:has-text('Submit Application')",
    "button[data-automation-id='submitButton']",          # Workday
    "button[aria-label*='Submit']",
]

# Buttons that LOOK like submit but must never be auto-clicked.
_DENYLIST_TEXT = re.compile(
    r"save|draft|back|cancel|previous|next|sign|upload|continue",
    re.IGNORECASE,
)


async def _maybe_autosubmit(page: Page, fields_filled: int, label: str) -> bool:
    """
    Auto-submit only if ALL of these hold:
      - toggles.auto_submit enabled AND dry_run off
      - at least 3 profile fields were pre-filled (form actually engaged)
      - exactly one plausible submit button is visible and not denylisted
    Any ambiguity → no click. Returns True if submitted.
    """
    if not config.enabled("auto_submit") or config.dry_run():
        return False

    if fields_filled < 3:
        print(f"[auto] {label}: only {fields_filled} field(s) filled — "
              f"NOT submitting (sanity check failed).")
        return False

    candidates = []
    for sel in _SUBMIT_SELECTORS:
        try:
            for el in await page.query_selector_all(sel):
                if not await el.is_visible():
                    continue
                if await el.is_disabled():
                    continue
                text = ((await el.inner_text()) or
                        (await el.get_attribute("value") or "") or "").strip()
                if _DENYLIST_TEXT.search(text):
                    continue
                candidates.append((el, text))
        except Exception:
            continue

    if len(candidates) != 1:
        print(f"[auto] {label}: {len(candidates)} submit candidate(s) — "
              f"NOT submitting (ambiguous).")
        return False

    el, text = candidates[0]
    print(f"[auto] {label}: sanity checks passed — clicking '{text}'...")
    try:
        await page.wait_for_timeout(random.uniform(1500, 3000))
        await el.click()
        await page.wait_for_timeout(4000)
        print(f"[auto] {label}: clicked submit. VERIFY the confirmation "
              f"page before closing.")
        return True
    except Exception as e:
        print(f"[auto] {label}: submit click failed ({e}).")
        return False


# ---------------------------------------------------------------------------
# Label lookup
# ---------------------------------------------------------------------------

async def _get_label_text(page: Page, el) -> str:
    """Returns the lowercased text of the <label> element associated with `el`."""
    try:
        el_id = await el.get_attribute("id")
        if el_id:
            label = await page.query_selector(f"label[for='{el_id}']")
            if label:
                return (await label.inner_text()).lower()
    except Exception:
        pass
    return ""
