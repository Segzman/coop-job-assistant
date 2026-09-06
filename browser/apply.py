"""
Application form assistant — real Safari via AppleScript.

Opens the job's URL(s) in a visible Safari window, pre-fills common
text fields from the applicant profile in .env via JavaScript, attaches
the resume PDF through the native Open dialog, then waits for the user
to review everything and submit manually.

Dual-tab support:
  - If a Sheridan job also has an external_url (e.g. a Workday link), both
    the Sheridan posting page and the external form are opened in the SAME
    Safari window as separate tabs.  Fields are pre-filled on both.
  - The external tab is brought to the foreground so it is the first thing
    the user sees.

DEFAULT GUARANTEE: This module NEVER clicks Submit, Apply, Send, or any
other form submission button — UNLESS toggles.auto_submit is enabled in
config.yaml (and dry_run is off). Even then it only submits when its
sanity checks pass; any doubt = no click, user stays in control.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path

from browser import safari_bridge as safari
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
    (["first[\\s-]?name", "fname", "given[\\s-]?name"],          PROFILE["first_name"]),
    (["last[\\s-]?name",  "lname", "family[\\s-]?name",
      "surname"],                                                    PROFILE["last_name"]),
    (["full[\\s-]?name",  "your[\\s-]?name", "applicant[\\s-]?name",
      "^name$"],                                                     PROFILE["full_name"]),
    (["e[_-]?mail"],                                                 PROFILE["email"]),
    (["phone", "mobile", "tel", "cell"],                             PROFILE["phone"]),
    (["linkedin"],                                                   PROFILE["linkedin"]),
    (["github", "git[\\s-]?hub"],                                    PROFILE["github"]),
    (["location", "city", "address", "postal", "zip"],               PROFILE["location"]),
    (["start[\\s-]?date", "available", "availability",
      "when[\\s-]?can[\\s-]?you"],                                 PROFILE["availability"]),
]

# ---------------------------------------------------------------------------
# Workday-specific automation-id selectors.
# ---------------------------------------------------------------------------
_CITY = (PROFILE["location"].split(",")[0].strip() or "Oakville")

WORKDAY_SELECTORS: list[tuple[str, str]] = [
    ('[data-automation-id="legalNameSection_firstName"]',   PROFILE["first_name"]),
    ('[data-automation-id="legalNameSection_lastName"]',    PROFILE["last_name"]),
    ('[data-automation-id="email"]',                        PROFILE["email"]),
    ('[data-automation-id="phone"]',                        PROFILE["phone"]),
    ('[data-automation-id="phoneNumber"]',                  PROFILE["phone"]),
    ('[data-automation-id="addressSection_addressLine1"]',  PROFILE["location"]),
    ('[data-automation-id="city"]',                         _CITY),
    ('[data-automation-id="linkedin"]',                     PROFILE["linkedin"]),
    ('[data-automation-id="linkedInUrl"]',                  PROFILE["linkedin"]),
    ('[data-automation-id="portfolioUrl"]',                 PROFILE["github"]),
    ('[data-automation-id="websiteUrl"]',                   PROFILE["github"]),
    ('[data-automation-id="coverLetter"]',                  ""),   # leave blank
]

# External-apply link patterns — used to auto-detect 3rd-party URLs from
# the Sheridan posting page (Workday, Greenhouse, Lever, etc.)
_EXTERNAL_DOMAINS = re.compile(
    r"(myworkdayjobs\.com|greenhouse\.io|lever\.co|jobvite\.com"
    r"|smartrecruiters\.com|taleo\.net|icims\.com|ashbyhq\.com"
    r"|bamboohr\.com|breezy\.hr|recruitee\.com)",
    re.IGNORECASE,
)

_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button[data-automation-id='submitButton']",          # Workday
    "button[aria-label*='Submit' i]",
]

# Buttons that LOOK like submit but must never be auto-clicked.
_DENYLIST = "save|draft|back|cancel|previous|next|sign|upload|continue"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def open_and_prefill(job: Job, resume_path: Path | None = None) -> None:
    """
    Opens the job application URL(s) in Safari.
    - If job.external_url is set (or auto-detected), opens BOTH pages
      (two tabs in one window).
    - Pre-fills known fields on every tab opened.
    - Attaches `resume_path` (per-job tailored PDF) when given, else the
      .env master resume.
    - With toggles.auto_submit ON (and dry_run off), attempts a
      sanity-checked auto-submit; otherwise waits for the user.
    """
    start_url = job.url.split("#")[0] if "#posting" in job.url else job.url
    wid = await asyncio.to_thread(safari.open_window, start_url)
    await asyncio.sleep(3)

    detected_external: str | None = None

    # ── Tab 1: Sheridan posting page ────────────────────────────────────
    print(f"\n[apply] Opening Sheridan posting: {job.url}")
    if job.platform == "sheridan" and "#posting" in job.url:
        posting_id = job.url.split("#posting")[-1]
        try:
            await asyncio.to_thread(safari.js, wid, """(() => {
              const a = document.querySelector(
                '.stat-table a[onclick*="displayQuickSearch"]');
              if (a) { a.click(); return "clicked"; }
              return "miss";
            })()""")
            if await _poll_present(
                    wid, [f".np-apply-btn-{posting_id}"], 12):
                await asyncio.to_thread(safari.js, wid, f"""(() => {{
                  document.querySelector(".np-apply-btn-{posting_id}").click();
                  return "clicked";
                }})()""")
                await asyncio.sleep(3)
                print(f"[apply] Opened posting {posting_id} in Sheridan Works.")
            detected_external = await _find_external_link(wid)
            if detected_external:
                print(f"[apply] Auto-detected external link: {detected_external}")
        except RuntimeError:
            print("[apply] Safari window closed.")
            return

    await asyncio.sleep(2.5)
    await asyncio.to_thread(safari.activate_tab, wid, 1)
    s_filled = await _prefill_all(wid, workday=True)
    await _attach_resume(wid, resume_path)
    await _maybe_autosubmit(wid, s_filled, "Sheridan")

    # ── Tab 2: external application form ───────────────────────────────
    ext_url = job.external_url or detected_external
    if ext_url:
        print(f"[apply] Opening external application: {ext_url}")
        await asyncio.to_thread(safari.new_tab, wid, ext_url)
        await asyncio.sleep(4)
        await asyncio.to_thread(safari.activate_tab, wid, 2)
        total_ext = await _prefill_all(wid, workday=True)
        await _attach_resume(wid, resume_path)
        print(f"[apply] External form: pre-filled {total_ext} field(s).")
        await _maybe_autosubmit(wid, total_ext, "External")
    else:
        print("[apply] No external URL set. Only Sheridan posting is open.")
        print("[apply]   → Use: python main.py set-url <job_id> <url>  to add one.")

    print(f"[apply] Sheridan tab: pre-filled {s_filled} field(s).")
    print("[apply] Review everything carefully before submitting.")
    print("[apply] Close the Safari window when you are done.\n")

    await asyncio.to_thread(safari.wait_window_closed, wid)
    print("[apply] Window closed.")


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

async def _poll_present(wid: int, selectors: list[str],
                        timeout_s: int) -> bool:
    sels_js = json.dumps(", ".join(selectors))
    script = (f"(() => [...document.querySelectorAll({sels_js})]"
              '.some(e => e && e.offsetParent !== null) ? "yes" : "no")()')
    for _ in range(timeout_s * 2):
        try:
            out = (await asyncio.to_thread(safari.js, wid, script)
                   ).strip().strip('"')
        except RuntimeError:
            return False
        if out == "yes":
            return True
        await asyncio.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Auto-detect external apply link
# ---------------------------------------------------------------------------

async def _find_external_link(wid: int) -> str | None:
    try:
        out = await asyncio.to_thread(safari.js, wid, """(() => {
          const hits = [];
          document.querySelectorAll("a[href]").forEach(a =>
            hits.push(a.getAttribute("href") || ""));
          document.querySelectorAll("[onclick]").forEach(el =>
            hits.push(el.getAttribute("onclick") || ""));
          return JSON.stringify(hits.slice(0, 400));
        })()""")
        for href in json.loads(out):
            if _EXTERNAL_DOMAINS.search(href):
                return href.strip()
            m = re.search(r"['\"]?(https?://[^'\")\s]+)['\"]?", href)
            if m and _EXTERNAL_DOMAINS.search(m.group(1)):
                return m.group(1)
    except (RuntimeError, json.JSONDecodeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Prefill (Workday-specific first, then generic — one JS round-trip each)
# ---------------------------------------------------------------------------

def _workday_js() -> str:
    rules = [{"sel": sel, "val": val}
             for sel, val in WORKDAY_SELECTORS if val]
    return f"""(() => {{
      const rules = {json.dumps(rules)};
      let filled = 0;
      for (const r of rules) {{
        const el = document.querySelector(r.sel);
        if (!el || el.offsetParent === null) continue;
        if (!el.value) {{
          el.focus();
          el.value = r.val;
          el.dispatchEvent(new Event("input", {{bubbles: true}}));
          el.dispatchEvent(new Event("change", {{bubbles: true}}));
          filled++;
        }}
      }}
      return String(filled);
    }})()"""


def _generic_js() -> str:
    rules = [{"pats": pats, "val": val}
             for pats, val in FIELD_PATTERNS if val]
    return f"""(() => {{
      const rules = {json.dumps(rules)};
      let filled = 0;
      const els = [...document.querySelectorAll(
        "input:not([type='hidden']):not([type='submit'])" +
        ":not([type='button']):not([type='checkbox'])" +
        ":not([type='radio']):not([type='file']), textarea")];
      for (const el of els) {{
        if (el.offsetParent === null) continue;
        let lab = "";
        if (el.id) {{
          const l = document.querySelector(
            'label[for="' + el.id + '"]');
          if (l) lab = l.innerText;
        }}
        const sig = ((el.name || "") + " " + (el.id || "") + " " +
          (el.placeholder || "") + " " + lab).toLowerCase();
        for (const r of rules) {{
          let hit = false;
          for (const p of r.pats) {{
            try {{ if (new RegExp(p, "i").test(sig)) {{ hit = true; break; }} }}
            catch (e) {{ continue; }}
          }}
          if (hit) {{
            if (!el.value) {{
              el.focus();
              el.value = r.val;
              el.dispatchEvent(new Event("input", {{bubbles: true}}));
              el.dispatchEvent(new Event("change", {{bubbles: true}}));
              filled++;
            }}
            break;
          }}
        }}
      }}
      return String(filled);
    }})()"""


async def _prefill_all(wid: int, workday: bool = False) -> int:
    total = 0
    try:
        if workday:
            total += int((await asyncio.to_thread(
                safari.js, wid, _workday_js())).strip().strip('"') or 0)
        total += int((await asyncio.to_thread(
            safari.js, wid, _generic_js())).strip().strip('"') or 0)
    except (RuntimeError, ValueError):
        pass
    return total


# ---------------------------------------------------------------------------
# Resume attachment (native Open dialog via System Events)
# ---------------------------------------------------------------------------

_ATTACH_CLICK_JS = """(() => {
  const inp = [...document.querySelectorAll("input[type='file']")]
    .find(e => {
      if (e.offsetParent === null) return false;
      const acc = (e.getAttribute("accept") || "").toLowerCase();
      return !acc || acc.indexOf("pdf") !== -1 ||
        acc.indexOf("doc") !== -1 || acc.indexOf("*") !== -1;
    });
  if (!inp) return "miss";
  inp.scrollIntoView({block: "center"});
  inp.click();
  return "clicked";
})()"""

_ATTACH_CHECK_JS = """(() => {
  const inp = document.querySelector("input[type='file']");
  return (inp && inp.files && inp.files.length > 0) ? "yes" : "no";
})()"""


async def _attach_resume(wid: int,
                         resume_path: str | Path | None = None) -> bool:
    path = Path(resume_path or PROFILE["resume_path"] or "")
    if not str(path):
        return False
    if not path.exists():
        print(f"[apply] Resume file not found: {path}")
        return False
    try:
        clicked = (await asyncio.to_thread(safari.js, wid, _ATTACH_CLICK_JS)
                   ).strip().strip('"')
        if clicked != "clicked":
            return False
        await asyncio.to_thread(safari.upload_file, wid, str(path))
        # verify the file landed:
        for _ in range(20):
            await asyncio.sleep(0.5)
            check = (await asyncio.to_thread(safari.js, wid, _ATTACH_CHECK_JS)
                     ).strip().strip('"')
            if check == "yes":
                print(f"[apply] Resume attached: {path.name}")
                return True
        print("[apply] Resume dialog completed but file not detected — "
              "attach manually.")
        return False
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Sanity-gated auto-submit (only when toggles.auto_submit is ON)
# ---------------------------------------------------------------------------

async def _maybe_autosubmit(wid: int, fields_filled: int, label: str) -> bool:
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

    try:
        out = await asyncio.to_thread(safari.js, wid, f"""(() => {{
          const sels = {json.dumps(", ".join(_SUBMIT_SELECTORS))};
          const deny = new RegExp({json.dumps(_DENYLIST)}, "i");
          const found = [];
          document.querySelectorAll(sels).forEach(el => {{
            if (el.offsetParent === null || el.disabled) return;
            const text = ((el.innerText || "") + " " +
              (el.getAttribute("value") || "") + " " +
              (el.getAttribute("aria-label") || "")).trim();
            if (deny.test(text)) return;
            if (!el.id) el.id = "auto-submit-candidate";
            found.push(text);
          }});
          return JSON.stringify(found);
        }})()""")
        candidates = json.loads(out)
    except (RuntimeError, json.JSONDecodeError):
        return False

    if len(candidates) != 1:
        print(f"[auto] {label}: {len(candidates)} submit candidate(s) — "
              f"NOT submitting (ambiguous).")
        return False

    print(f"[auto] {label}: sanity checks passed — "
          f"clicking '{candidates[0]}'...")
    try:
        await asyncio.sleep(random.uniform(1.5, 3.0))
        await asyncio.to_thread(safari.js, wid, """(() => {
          const el = document.getElementById("auto-submit-candidate");
          if (el) { el.click(); return "clicked"; }
          return "miss";
        })()""")
        await asyncio.sleep(4)
        print(f"[auto] {label}: clicked submit. VERIFY the confirmation "
              f"page before closing.")
        return True
    except RuntimeError as e:
        print(f"[auto] {label}: submit click failed ({e}).")
        return False
