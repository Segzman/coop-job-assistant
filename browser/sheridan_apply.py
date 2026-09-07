"""
Sheridan Works bot-does-all application.

Per job, against the live Orbis portal (mapped 2026-09-06):
  1. Upload bespoke resume + cover PDFs to the Documents vault
     (dashboard → Upload a Document: Name + Type select + file input).
  2. Board → quick search → walk pages → posting → APPLY →
     custom package radio.
  3. Select the two fresh docs in the package dropdowns
     (transcript stays on its default).
  4. Caller shows the native final-yes popup; on yes, click
     SUBMIT APPLICATION and verify the confirmation.

Returns "submitted" | "skipped" | "error". Never clicks submit —
that stays behind the caller's popup gate.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from browser import safari_bridge as safari

SHERIDAN_BASE = "https://sheridanworks.sheridancollege.ca"
BOARD_URL = f"{SHERIDAN_BASE}/myAccount/co-op/coopJobs.htm"
DASH_URL = f"{SHERIDAN_BASE}/myAccount/dashboard.htm"

TRIGGER_JS = """(() => {
  const a = [...document.querySelectorAll('.stat-table a')]
    .find(x => /for my program/i.test(x.innerText || ''));
  if (!a) return "miss";
  a.click();
  return "clicked";
})()"""

ROWS_JS = ("document.querySelectorAll("
           "'#postingsTable tbody tr.searchResult').length")

NEXT_JS = """(() => {
  const n = [...document.querySelectorAll(".pagination a")]
    .find(a => /^(next|>)$/i.test(a.innerText.trim()) &&
      !a.closest("li.disabled"));
  if (!n) return "none";
  n.click();
  return "clicked";
})()"""

UPLOAD_JS = """(() => {
  const a = [...document.querySelectorAll('a')].find(x =>
    /upload a document/i.test((x.innerText || '').trim()));
  if (!a) return "miss";
  a.click();
  return "clicked";
})()"""


async def _js(wid: int, script: str, timeout: int = 60) -> str:
    return (await asyncio.to_thread(safari.js, wid, script, timeout)
            ).strip()


async def _num(wid: int, script: str) -> int:
    try:
        return int(float(await _js(wid, script)))
    except (RuntimeError, ValueError):
        return -1


async def _wait_board(wid: int, timeout_s: int = 25) -> bool:
    for _ in range(timeout_s * 2):
        try:
            if (await _js(wid, "document.querySelector('.stat-table') "
                               "? 'yes' : 'no'")).strip('"') == "yes":
                return True
        except RuntimeError:
            return False
        await asyncio.sleep(0.5)
    return False


async def _wait_rows(wid: int, timeout_s: int = 20) -> bool:
    for _ in range(timeout_s * 2):
        if await _num(wid, ROWS_JS) > 0:
            return True
        await asyncio.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# 1. Vault upload
# ---------------------------------------------------------------------------

async def upload_document(wid: int, pdf: Path, name: str,
                          doctype: str) -> bool:
    """
    Dashboard → Upload a Document → Name + Type + file → Upload.
    doctype: "Resume - .pdf" | "Coverletter - .pdf".
    """
    try:
        await asyncio.to_thread(safari.goto, wid, DASH_URL)
        await asyncio.sleep(4)
        if (await _js(wid, UPLOAD_JS)).strip('"') != "clicked":
            print("[sheridan-apply] Upload entry not found.")
            return False
        await asyncio.sleep(4)

        fill = await _js(wid, f"""(() => {{
          const t = [...document.querySelectorAll(
            "input:not([type=hidden]):not([type=file])")]
            .find(e => e.offsetParent !== null &&
              (e.type === "text" || e.type === ""));
          const s = [...document.querySelectorAll("select")]
            .find(e => e.offsetParent !== null);
          if (!t || !s) return "miss";
          t.focus();
          t.value = {json.dumps(name)};
          t.dispatchEvent(new Event("input", {{bubbles: true}}));
          t.dispatchEvent(new Event("change", {{bubbles: true}}));
          const opt = [...s.options].find(o =>
            (o.text || "").trim() === {json.dumps(doctype)});
          if (!opt) return "no-type";
          s.value = opt.value;
          s.dispatchEvent(new Event("change", {{bubbles: true}}));
          return "filled";
        }})()""")
        if fill.strip('"') != "filled":
            print(f"[sheridan-apply] Upload form not ready ({fill}).")
            return False

        clicked = await _js(wid, """(() => {
          const f = document.querySelector(
            "#fileUpload_docUpload, input[type='file']");
          if (!f) return "miss";
          f.click();
          return "clicked";
        })()""")
        if clicked.strip('"') != "clicked":
            return False
        await asyncio.to_thread(safari.upload_file, wid, str(pdf))
        await asyncio.sleep(2)

        saved = await _js(wid, """(() => {
          const b = [...document.querySelectorAll("button")]
            .find(e => /upload document/i.test(
              (e.innerText || "").trim()) && e.offsetParent !== null);
          if (!b) return "miss";
          b.click();
          return "clicked";
        })()""")
        if saved.strip('"') != "clicked":
            print("[sheridan-apply] Upload save button not found.")
            return False
        await asyncio.sleep(4)
        return True
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# 2. Posting → custom package → select docs
# ---------------------------------------------------------------------------

async def _find_and_open(wid: int, posting_id: str,
                         max_pages: int = 8) -> bool:
    for _ in range(max_pages):
        try:
            n = await _num(wid, ROWS_JS)
        except RuntimeError:
            return False
        if n <= 0:
            await asyncio.sleep(2)
            continue
        try:
            out = await _js(wid, f"""(() => {{
              const b = document.querySelector(
                ".np-apply-btn-{posting_id}");
              if (!b) return "miss";
              b.click();
              return "clicked";
            }})()""")
        except RuntimeError:
            return False
        if out.strip('"') == "clicked":
            return True
        try:
            nxt = await _js(wid, NEXT_JS)
        except RuntimeError:
            return False
        if nxt.strip('"') != "clicked":
            return False
        await asyncio.sleep(4)
    return False


async def _select_package_docs(wid: int, resume_name: str,
                               cover_name: str) -> tuple[bool, str]:
    """
    Custom-package radio → dropdowns → pick fresh docs.
    Returns (ok, detail). Transcript dropdown left on default.
    """
    try:
        radio = await _js(wid, """(() => {
          const r = [...document.querySelectorAll(
            "input[name='applyOption']")]
            .find(e => e.value === "customPkg");
          if (!r) return "miss";
          r.click();
          return "clicked";
        })()""")
        if radio.strip('"') != "clicked":
            return False, "custom package option missing"
        await asyncio.sleep(3)

        picked = await _js(wid, f"""(() => {{
          const want = {json.dumps({"resume": resume_name,
                                    "cover": cover_name})};
          const sels = [...document.querySelectorAll("select")]
            .filter(e => e.offsetParent !== null);
          const log = [];
          const ctxText = (s) => {{
            const c = s.closest("tr,div");
            return c ? (c.innerText || "") : "";
          }};
          for (const s of sels) {{
            const opts = [...s.options];
            const isTranscript = opts.some(o =>
              /transcript/i.test(o.text || ""));
            if (isTranscript) {{
              log.push("transcript:kept-default");
              continue;
            }}
            const label = (/cover/i.test(s.name || "") ||
              /cover/i.test(ctxText(s))) ? "cover" : "resume";
            const hit = opts.find(o =>
              (o.text || "").indexOf(want[label]) === 0);
            if (hit) {{
              s.value = hit.value;
              s.dispatchEvent(new Event("change", {{bubbles: true}}));
              log.push(label + ":picked");
            }} else {{
              log.push(label + ":missing");
            }}
          }}
          return JSON.stringify(log);
        }})()""")
        log = json.loads(picked)
        if "resume:missing" in log or "cover:missing" in log:
            return False, f"dropdown miss: {log}"
        if "resume:picked" not in log:
            return False, f"resume not picked: {log}"
        return True, "; ".join(log)
    except (RuntimeError, json.JSONDecodeError) as e:
        return False, str(e)[:120]


async def _submit_button_text(wid: int) -> str | None:
    """Exactly-one visible SUBMIT APPLICATION button, else None."""
    try:
        out = await _js(wid, """(() => {
          const found = [...document.querySelectorAll("button")]
            .filter(e => /^submit application$/i.test(
              (e.innerText || "").trim()) && e.offsetParent !== null);
          return JSON.stringify(found.map(e => e.id || "noid"));
        })()""")
        ids = json.loads(out)
    except (RuntimeError, json.JSONDecodeError):
        return None
    if len(ids) != 1:
        return None
    return ids[0]


async def click_submit(wid: int) -> str:
    """Clicks SUBMIT APPLICATION, returns verdict page state."""
    btn_id = await _submit_button_text(wid)
    if not btn_id:
        return "ambiguous"
    try:
        await _js(wid, f"""(() => {{
          const el = document.getElementById({json.dumps(btn_id)});
          if (el) {{ el.click(); return "clicked"; }}
          const b = [...document.querySelectorAll("button")].find(e =>
            /^submit application$/i.test((e.innerText || "").trim()) &&
            e.offsetParent !== null);
          if (b) {{ b.click(); return "clicked"; }}
          return "miss";
        }})()""")
    except RuntimeError:
        return "closed"
    await asyncio.sleep(5)
    try:
        return (await _js(wid, """(() => {
          const t = ((document.body && document.body.innerText) || "")
            .toLowerCase();
          if (["required", "invalid", "missing", "error",
               "please complete", "please correct", "unsuccessful"]
              .some(k => t.indexOf(k) !== -1)) return "errors";
          if (["thank you", "received", "submitted", "confirmation",
               "successfully", "reference number"]
              .some(k => t.indexOf(k) !== -1)) return "confirmed";
          return "unknown";
        })()""")).strip('"')
    except RuntimeError:
        return "closed"


# ---------------------------------------------------------------------------
# Public: full per-job run (caller owns the final-yes popup)
# ---------------------------------------------------------------------------

async def apply_to_posting(wid: int, posting_id: str, resume_pdf: Path,
                           cover_pdf: Path | None, company: str,
                           title: str) -> str:
    """
    Uploads docs to vault, opens posting, builds custom package.
    Returns "ready" (caller shows popup + click_submit),
    "skipped" (not found), or "error".
    """
    resume_name = f"{company[:24]} Resume".strip()
    cover_name = f"{company[:24]} Cover Letter".strip()

    if not await upload_document(wid, resume_pdf, resume_name,
                                 "Resume - .pdf"):
        return "error"
    print(f"[sheridan-apply] Vault: resume '{resume_name}' uploaded.")
    if cover_pdf and cover_pdf.exists():
        if await upload_document(wid, cover_pdf, cover_name,
                                 "Coverletter - .pdf"):
            print(f"[sheridan-apply] Vault: cover '{cover_name}' uploaded.")
        else:
            print("[sheridan-apply] Cover upload failed — resume only.")
            cover_name = ""

    await asyncio.to_thread(safari.goto, wid, BOARD_URL)
    if not await _wait_board(wid):
        return "error"
    if (await _js(wid, TRIGGER_JS)).strip('"') != "clicked":
        return "error"
    if not await _wait_rows(wid):
        return "error"
    if not await _find_and_open(wid, posting_id):
        return "skipped"

    # posting detail → APPLY into the application view
    await asyncio.sleep(2)
    try:
        await _js(wid, """(() => {
          const b = document.querySelector(
            ".nav--interaction__item.js--actions-group-item.applyButton") ||
            [...document.querySelectorAll("button")].find(e =>
              /^apply$/i.test((e.innerText || "").trim()) &&
              e.offsetParent !== null);
          if (b) b.click();
        })()""")
    except RuntimeError:
        return "error"
    await asyncio.sleep(5)

    ok, detail = await _select_package_docs(wid, resume_name, cover_name)
    print(f"[sheridan-apply] Package docs: {detail}")
    if not ok:
        return "error"
    if await _submit_button_text(wid) is None:
        print("[sheridan-apply] Submit button ambiguous/missing.")
        return "error"
    return "ready"
