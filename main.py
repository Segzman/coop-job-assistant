#!/usr/bin/env python3
"""
Job Application Assistant

Commands:
  python main.py scrape              Scrape both Sheridan Works and Indeed
  python main.py scrape --sheridan   Sheridan Works only
  python main.py scrape --indeed     Indeed only

  python main.py list                List new jobs (default)
  python main.py list --status seen  Filter by status (new/seen/applied/skipped/all)
  python main.py list --platform sheridan

  python main.py apply <job_id>      Open one application in browser, prefill fields
  python main.py apply-all           Loop through ALL new/seen Sheridan jobs one by one
  python main.py status <job_id> applied   Mark a job without opening the browser
  python main.py status <job_id> skipped

   python main.py tailor <job_id>     Generate a tailored resume PDF for one job
   python main.py slate-collect    Back up own SLATE writing (gated)
   python main.py slate-style      Distill voice profile from backup (gated)
  python main.py linkedin-collect    Scrape recruiters at applied companies (gated)
  python main.py outreach-drafts     LLM drafts for recruiters (gated)
  python main.py outreach-list       Show all recruiter records + drafts
  python main.py outreach-send       Send queued drafts (heavily gated)
  python main.py config              Show current toggles
"""
from __future__ import annotations
import asyncio
import sys
import argparse
from datetime import date, timedelta
from pathlib import Path

# Load .env BEFORE any other project imports that read os.environ
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.panel import Panel

import config
from scrapers.sheridan import SheridanScraper
from scrapers.indeed import IndeedScraper
from storage.jobs import load_jobs, save_jobs, merge_new_jobs, update_status, update_external_url, filter_jobs
from browser.apply import open_and_prefill
from ui.display import print_job_table, print_job_detail
from tailoring.resume import tailor_resume, latest_pdf_for

console = Console()


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

async def cmd_scrape(args: argparse.Namespace) -> None:
    scrape_sheridan = not args.indeed   # default: both; --indeed → skip sheridan
    scrape_indeed   = not args.sheridan # default: both; --sheridan → skip indeed

    all_fresh: list = []

    if scrape_sheridan:
        console.print("[bold blue]▶ Scraping Sheridan Works...[/]")
        try:
            jobs = await SheridanScraper().scrape()
            all_fresh.extend(jobs)
        except KeyError as e:
            console.print(f"[red]Missing environment variable: {e}[/]")
            console.print("[yellow]Set SHERIDAN_USERNAME and SHERIDAN_PASSWORD in your .env file.[/]")
        except Exception as e:
            console.print(f"[red]Sheridan scrape failed: {e}[/]")

    if scrape_indeed:
        console.print("[bold blue]▶ Scraping Indeed Canada...[/]")
        try:
            jobs = await IndeedScraper().scrape()
            all_fresh.extend(jobs)
        except Exception as e:
            console.print(f"[red]Indeed scrape failed: {e}[/]")

    existing = load_jobs()
    merged, new_count = merge_new_jobs(existing, all_fresh)
    save_jobs(merged)

    console.print(
        f"\n[green]Done.[/] {new_count} new job(s) added. "
        f"Total in store: {len(merged)}."
    )
    if new_count > 0:
        console.print("[dim]Run: python main.py list   to review new jobs.[/]")


def cmd_list(args: argparse.Namespace) -> None:
    jobs = load_jobs()
    status = getattr(args, "status", "new")
    platform = getattr(args, "platform", None)

    filtered = filter_jobs(jobs, status=status, platform=platform)

    if not filtered:
        console.print(
            f"[yellow]No jobs with status='{status}'"
            + (f", platform='{platform}'" if platform else "")
            + ".[/]"
        )
        total = len(jobs)
        if total:
            console.print(f"[dim]{total} total job(s) in store. "
                          "Use --status all to see everything.[/]")
        return

    print_job_table(filtered)


async def cmd_apply(args: argparse.Namespace) -> None:
    jobs = load_jobs()
    job_id = args.job_id

    if job_id not in jobs:
        console.print(f"[red]Job ID '{job_id}' not found.[/]")
        console.print("[dim]Use: python main.py list   to see available IDs.[/]")
        return

    job = jobs[job_id]
    print_job_detail(job)

    if not Confirm.ask("\nOpen application in browser?", default=True):
        return

    # Mark as "seen" before opening
    if job.status == "new":
        update_status(job_id, "seen")

    resume_path = tailor_resume(job) if config.enabled("resume_tailoring") else None
    await open_and_prefill(job, resume_path)

    # After the browser closes, ask for a status update
    console.print()
    new_status = Prompt.ask(
        "Update status",
        choices=["applied", "skipped", "seen", "new"],
        default="applied",
    )
    notes_input = Prompt.ask(
        "Notes (press Enter to skip)",
        default="",
    )
    update_status(job_id, new_status, notes=notes_input or None)  # type: ignore[arg-type]
    console.print(f"[green]Status updated → '{new_status}'[/]")


async def cmd_apply_all(args: argparse.Namespace) -> None:
    """
    Loops through every new/seen Sheridan job sorted by deadline (soonest first).
    For each job: shows detail, asks apply / skip / quit, opens browser,
    waits for you to close it, then asks for a status update before moving on.
    """
    jobs = load_jobs()
    platform = getattr(args, "platform", "sheridan")

    # Collect new + seen jobs for the chosen platform
    candidates = [
        j for j in jobs.values()
        if j.platform == platform and j.status in ("new", "seen")
    ]
    # Sort by deadline ascending (None last), then date_found
    candidates.sort(key=lambda j: (j.deadline or "9999-12-31", j.date_found))

    if not candidates:
        console.print(
            f"[yellow]No new/seen {platform} jobs to apply to.[/]\n"
            "[dim]They may all be applied/skipped already. "
            "Run: python main.py list --status all[/]"
        )
        return

    today     = date.today().isoformat()
    tomorrow  = (date.today() + timedelta(days=1)).isoformat()
    urgent    = [j for j in candidates if j.deadline in (today, tomorrow)]
    total     = len(candidates)

    # ── Summary banner ──────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        f"[bold]{total} {platform.title()} job(s) queued — sorted by deadline (soonest first)[/]\n"
        + (f"[bold red]⚠  {len(urgent)} job(s) deadline TODAY or TOMORROW![/]" if urgent else ""),
        title="[bold cyan]apply-all[/]",
        expand=False,
    ))
    print_job_table(candidates)

    if not Confirm.ask(
        f"\nStart batch-apply ({total} jobs, one browser window at a time)?",
        default=True,
    ):
        return

    applied_this_run  = 0
    skipped_this_run  = 0

    for idx, job in enumerate(candidates, 1):
        # ── Per-job header ─────────────────────────────────────────────────
        deadline_str = f"  [bold red]⚠  DEADLINE {job.deadline}[/]" \
                       if job.deadline in (today, tomorrow) else \
                       (f"  [yellow]deadline {job.deadline}[/]" if job.deadline else "")
        console.print(
            f"\n[bold cyan]{'─' * 60}[/]"
            f"\n[bold]Job {idx}/{total}[/]  {job.title}  ·  {job.company}{deadline_str}"
            f"\n[bold cyan]{'─' * 60}[/]"
        )
        print_job_detail(job)

        # ── Action prompt ───────────────────────────────────────────────────
        choice = Prompt.ask(
            "\n[bold]Action[/]",
            choices=["apply", "skip", "quit"],
            default="apply",
        )

        if choice == "quit":
            console.print("[yellow]Stopping batch apply.[/]")
            break

        if choice == "skip":
            update_status(job.id, "skipped")
            skipped_this_run += 1
            console.print(f"[dim]↷ Skipped.[/]")
            continue

        # ── Open browser ────────────────────────────────────────────────────
        if job.status == "new":
            update_status(job.id, "seen")

        resume_path = tailor_resume(job) if config.enabled("resume_tailoring") else None
        await open_and_prefill(job, resume_path)

        # Auto-apply mode: no per-job prompts between jobs
        if config.enabled("auto_apply") and not config.dry_run():
            applied_this_run += 1
            update_status(job.id, "applied")
            console.print(f"[green]✓ Auto-applied → {job.company}[/]")
            continue

        # ── Post-apply status update ─────────────────────────────────────────
        console.print()
        new_status = Prompt.ask(
            "Update status",
            choices=["applied", "skipped", "seen", "new"],
            default="applied",
        )
        notes_input = Prompt.ask("Notes (Enter to skip)", default="")
        update_status(job.id, new_status, notes=notes_input or None)  # type: ignore[arg-type]

        if new_status == "applied":
            applied_this_run += 1
            console.print(f"[green]✓ Applied → {job.company}[/]")
        else:
            if new_status == "skipped":
                skipped_this_run += 1
            console.print(f"[dim]Marked '{new_status}' → {job.company}[/]")

    # ── Final summary ────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        f"[green]Applied:  {applied_this_run}[/]\n"
        f"[dim]Skipped:  {skipped_this_run}[/]\n"
        f"[dim]Remaining: "
        f"{total - applied_this_run - skipped_this_run} (still new/seen)[/]",
        title="[bold]Batch apply complete[/]",
        expand=False,
    ))


async def cmd_apply_batch(args: argparse.Namespace) -> None:
    """
    One-yes batch apply:
      Phase 1 — bespoke resume for EVERY queued job (cached; skips done).
      Phase 2 — overview table + ONE confirm for the whole batch.
      Phase 3 — walk all jobs with zero prompts: Safari opens (Sheridan
                tab + external ATS tab), fields prefill, resume attaches,
                you review + submit, close window, next job opens.
                Each job auto-marks applied on window close; fix any you
                bailed on afterwards with: status <id> skipped
    """
    jobs = load_jobs()
    platform = getattr(args, "platform", "all")

    candidates = [
        j for j in jobs.values()
        if j.status in ("new", "seen")
        and (platform == "all" or j.platform == platform)
    ]
    candidates.sort(key=lambda j: (j.deadline or "9999-12-31", j.date_found))

    max_n = getattr(args, "max", 0) or len(candidates)
    candidates = candidates[:max_n]

    if not candidates:
        console.print("[yellow]No new/seen jobs queued.[/]\n"
                      "[dim]Run: python main.py scrape / list --status all[/]")
        return

    total = len(candidates)

    # ── Phase 1: bespoke resumes for all ──────────────────────────────
    console.print(Panel(
        f"[bold]Phase 1/{3} — tailoring {total} bespoke resume(s)[/]\n"
        "[dim]Local LLM, ~2 min each. Already-tailored jobs are skipped.[/]",
        title="[bold cyan]apply-batch[/]", expand=False))
    ready = 0
    for idx, job in enumerate(candidates, 1):
        console.print(f"[dim][{idx}/{total}][/] {job.title[:45]} @ "
                      f"{job.company[:25]} ...")
        try:
            pdf = tailor_resume(job) if config.enabled("resume_tailoring") \
                else None
        except Exception as e:
            console.print(f"[red]Tailor failed for {job.id}: {e}[/]")
            pdf = None
        if pdf:
            ready += 1
    console.print(f"[green]Resumes ready: {ready}/{total}[/] "
                  f"(missing ones fall back to master resume)\n")

    # ── Phase 2: overview + ONE yes ───────────────────────────────────
    console.print(Panel(
        f"[bold]Phase 2/{3} — batch overview[/]\n"
        f"{total} job(s), each opens in Safari (Sheridan + external tabs), "
        f"prefilled, resume attached. You review + submit each; "
        f"close window → next opens. Auto-marks applied.",
        title="[bold cyan]apply-batch[/]", expand=False))
    print_job_table(candidates)
    if not Confirm.ask(
        f"\nApply to all {total} jobs ({ready} bespoke resumes)?",
        default=True,
    ):
        console.print("[yellow]Batch cancelled — resumes kept for later.[/]")
        return

    # ── Phase 3: promptless walk ──────────────────────────────────────
    console.print(Panel(f"[bold]Phase 3/{3} — applying (no more prompts)[/]",
                        title="[bold cyan]apply-batch[/]", expand=False))
    done = 0
    for idx, job in enumerate(candidates, 1):
        console.print(
            f"\n[bold cyan]{'─' * 60}[/]"
            f"\n[bold]Job {idx}/{total}[/]  {job.title}  ·  {job.company}"
            f"\n[bold cyan]{'─' * 60}[/]"
        )
        if job.status == "new":
            update_status(job.id, "seen")
        try:
            resume_path = latest_pdf_for(job.id)
            await open_and_prefill(job, resume_path)
        except Exception as e:
            console.print(f"[red]Browser error on {job.id}: {e} "
                          f"— left as seen.[/]")
            continue
        update_status(job.id, "applied")
        done += 1
        console.print(f"[green]✓ {done}/{total} applied → "
                      f"{job.company}[/]")

    console.print()
    console.print(Panel(
        f"[green]Batch done: {done}/{total} marked applied.[/]\n"
        "[dim]Bailed on any without submitting? Fix with:\n"
        "  python main.py status <job_id> skipped[/]",
        title="[bold]Batch apply complete[/]",
        expand=False,
    ))


def cmd_status(args: argparse.Namespace) -> None:
    try:
        update_status(args.job_id, args.new_status)  # type: ignore[arg-type]
        console.print(f"[green]Job {args.job_id} marked as '{args.new_status}'.[/]")
    except KeyError as e:
        console.print(f"[red]{e}[/]")


def cmd_set_url(args: argparse.Namespace) -> None:
    """Attach an external application URL to a Sheridan job."""
    try:
        jobs = load_jobs()
        if args.job_id not in jobs:
            console.print(f"[red]Job ID '{args.job_id}' not found.[/]")
            return
        job = jobs[args.job_id]
        update_external_url(args.job_id, args.url)
        console.print(
            f"[green]External URL set for[/] [bold]{job.title}[/] [dim]({job.company})[/]\n"
            f"[dim]{args.url}[/]"
        )
        console.print("[dim]Next time you run apply or apply-all, both tabs will open.[/]")
    except KeyError as e:
        console.print(f"[red]{e}[/]")


def cmd_tailor(args: argparse.Namespace) -> None:
    """Generate (or regenerate with --force) a tailored resume for one job."""
    jobs = load_jobs()
    if args.job_id not in jobs:
        console.print(f"[red]Job ID '{args.job_id}' not found.[/]")
        return
    job = jobs[args.job_id]
    print_job_detail(job)
    path = tailor_resume(job)
    if path:
        console.print(f"\n[green]Tailored PDF:[/] {path}")
        console.print("[dim]Open it and review BEFORE using it in an application.[/]")
    elif not config.enabled("resume_tailoring"):
        console.print("[yellow]resume_tailoring toggle is OFF — enable in config.yaml.[/]")


def cmd_linkedin_collect(args: argparse.Namespace) -> None:
    from scrapers.linkedin import collect
    asyncio.run(collect())


def cmd_slate_collect(args: argparse.Namespace) -> None:
    from slate.collect import collect
    asyncio.run(collect())


def cmd_slate_style(args: argparse.Namespace) -> None:
    from slate.style import distill_style
    distill_style()


def cmd_outreach_drafts(args: argparse.Namespace) -> None:
    from outreach.outreach import drafts
    drafts(load_jobs(), force=args.force)


def cmd_outreach_list(args: argparse.Namespace) -> None:
    from storage.recruiters import load_recruiters, sent_today
    recruiters = load_recruiters()
    if not recruiters:
        console.print("[yellow]No recruiters collected yet. "
                      "Run: python main.py linkedin-collect[/]")
        return
    for r_id, r in recruiters.items():
        sent = f" [green]SENT {r['sent_date']}[/]" if r.get("sent_date") else ""
        draft_flag = "draft ✓" if r.get("draft") else "NO DRAFT"
        console.print(
            f"\n[bold]{r['name']}[/] — {r.get('title', '?')} @ {r.get('company', '?')}\n"
            f"  {r['profile_url']}  [{draft_flag}]{sent}"
        )
        if r.get("draft"):
            console.print(f"  [dim]{r['draft']}[/]")
    cap = int(config.limit("autosend_daily_cap"))
    console.print(f"\n[dim]{sent_today()}/{cap} sent today.[/]")


def cmd_outreach_send(args: argparse.Namespace) -> None:
    from outreach.outreach import autosend
    autosend()


def cmd_config(args: argparse.Namespace) -> None:
    import yaml
    with open(config.CONFIG_FILE) as f:
        data = yaml.safe_load(f)
    toggles = config.section("toggles")
    lines = []
    for name, val in toggles.items():
        mark = "[green]ON [/]" if val else "[dim]off[/]"
        lines.append(f"  {mark} {name}")
    console.print(Panel("\n".join(lines), title="config.yaml toggles", expand=False))
    console.print("[dim]Edit config.yaml to flip toggles. Ladder: dry_run → "
                  "resume_tailoring → auto_apply → auto_submit → linkedin_collect "
                  "→ linkedin_drafts → linkedin_autosend[/]\n"
                  "[dim]Voice: slate_collect → slate_style[/]")


def cmd_doctor(args: argparse.Namespace) -> None:
    """Check every piece of the pipeline. Non-invasive — opens nothing."""
    import os
    import shutil
    import subprocess as sp
    import urllib.request

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, hint: str = "") -> None:
        results.append((name, ok, hint))

    # 1. Python deps (stdlib + pip only — no browser driver packages)
    for mod in ("dotenv", "rich", "yaml"):
        try:
            __import__(mod)
            check(f"dep: {mod}", True)
        except ImportError:
            check(f"dep: {mod}", False, ".venv/bin/pip install -r requirements.txt")

    # 2. Apple native stack: Safari + osascript + JS-from-Apple-Events
    check("Safari.app", Path("/Applications/Safari.app").exists(),
          "macOS only — this pipeline drives real Safari")
    check("osascript", bool(shutil.which("osascript")),
          "should ship with macOS — check your PATH")
    try:
        out = sp.run(
            ["defaults", "read", "com.apple.Safari",
             "AllowJavaScriptFromAppleEvents"],
            capture_output=True, text=True, timeout=10,
        )
        js_ok = out.returncode == 0 and out.stdout.strip() == "1"
    except Exception:
        js_ok = False
    check("Safari: Allow JavaScript from Apple Events", js_ok,
          "Safari → Settings → Advanced → Show Develop menu, then "
          "Develop → Allow JavaScript from Apple Events")

    # 3. PDF toolchain
    for tool in ("pandoc", "weasyprint"):
        check(f"pdf: {tool}", bool(shutil.which(tool)),
              f"brew install {tool}")

    # 4. .env present + key fields
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        check(".env file", False, "cp .env.example .env")
    else:
        check(".env file", True)
        vals = dict(
            line.split("=", 1)
            for line in env_path.read_text().splitlines()
            if "=" in line and not line.strip().startswith("#")
        )
        for key in ("SHERIDAN_USERNAME", "APPLICANT_NAME", "APPLICANT_EMAIL",
                    "APPLICANT_RESUME_PATH"):
            v = vals.get(key, "").strip()
            placeholder = not v or "your" in v.lower()
            check(f".env: {key}", not placeholder,
                  f"set {key} in .env")
        rp = Path(vals.get("APPLICANT_RESUME_PATH", "").strip())
        if vals.get("APPLICANT_RESUME_PATH", "").strip() and "your" not in \
                vals.get("APPLICANT_RESUME_PATH", "").lower():
            check(".env: resume file exists", rp.exists(), str(rp))

    # 5. Master resume (markdown)
    master = Path(__file__).parent / config.section("resume")["master"]
    check("resume/master_resume.md", master.exists(),
          "create it — copy your resume content into Markdown")

    # 6. LLM server reachable
    cfg = config.section("llm")
    try:
        req = urllib.request.Request(f"{cfg['base_url']}/models")
        with urllib.request.urlopen(req, timeout=4):
            check(f"LLM at {cfg['base_url']}", True)
    except Exception:
        check(f"LLM at {cfg['base_url']}", False,
              "open LM Studio → Developer tab → Start Server "
              "(needed for tailoring + drafts)")

    # 7. Login sessions — native Safari holds them; no profiles to manage.
    # Sign in once in Safari itself (Indeed / LinkedIn / Sheridan SSO)
    # and every run reuses it. Nothing to check here.
    check("login: native Safari sessions", True)

    # 8. Job store
    jobs = load_jobs()
    by_status = {}
    for j in jobs.values():
        by_status[j.status] = by_status.get(j.status, 0) + 1
    check("job store", bool(jobs),
          "run: python main.py scrape"
          + (f"  [{', '.join(f'{k}:{v}' for k, v in sorted(by_status.items()))}]"
             if jobs else ""))

    # Report
    ok_all = all(ok for _, ok, _ in results)
    lines = []
    for name, ok, hint in results:
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        lines.append(f" {mark} {name}" + (f"\n     [dim]→ {hint}[/]" if hint and not ok else ""))
    console.print(Panel("\n".join(lines),
                        title="doctor — pipeline check", expand=False))
    console.print("\n[green]All good.[/]" if ok_all else
                  "\n[yellow]Fix the ✗ items above.[/]")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Job Application Assistant — scrape, track, and apply.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scrape
    p_scrape = sub.add_parser("scrape", help="Scrape job listings from platforms")
    p_scrape.add_argument("--sheridan", action="store_true", help="Sheridan Works only")
    p_scrape.add_argument("--indeed",   action="store_true", help="Indeed only")

    # list
    p_list = sub.add_parser("list", help="Display jobs from the local store")
    p_list.add_argument(
        "--status",
        default="new",
        choices=["new", "seen", "applied", "skipped", "all"],
        help="Filter by status (default: new)",
    )
    p_list.add_argument(
        "--platform",
        choices=["sheridan", "indeed"],
        help="Filter by platform",
    )

    # apply (single job)
    p_apply = sub.add_parser("apply", help="Open a single job's application page")
    p_apply.add_argument("job_id", help="Job ID from the list command")

    # apply-all (batch)
    p_apply_all = sub.add_parser(
        "apply-all",
        help="Loop through all new/seen Sheridan jobs one by one",
    )
    p_apply_all.add_argument(
        "--platform",
        choices=["sheridan", "indeed"],
        default="sheridan",
        help="Platform to batch-apply to (default: sheridan)",
    )

    # apply-batch (one-yes batch mode)
    p_batch = sub.add_parser(
        "apply-batch",
        help="Tailor all resumes, one confirm, then walk every job",
    )
    p_batch.add_argument(
        "--platform",
        choices=["sheridan", "indeed", "all"],
        default="all",
        help="Platform to batch-apply to (default: all)",
    )
    p_batch.add_argument(
        "--max",
        type=int,
        default=0,
        help="Cap jobs this run (0 = all). Deadlines-soonest first.",
    )

    # status
    p_status = sub.add_parser("status", help="Manually update a job's status")
    p_status.add_argument("job_id", help="Job ID")
    p_status.add_argument(
        "new_status",
        choices=["new", "seen", "applied", "skipped"],
        help="New status to set",
    )

    # set-url
    p_set_url = sub.add_parser(
        "set-url",
        help="Attach an external application URL to a job (e.g. Workday, Greenhouse)",
    )
    p_set_url.add_argument("job_id", help="Job ID from the list command")
    p_set_url.add_argument("url",    help="External application URL")

    # tailor
    p_tailor = sub.add_parser("tailor", help="Generate a tailored resume PDF for one job")
    p_tailor.add_argument("job_id", help="Job ID from the list command")

    # linkedin-collect
    sub.add_parser(
        "linkedin-collect",
        help="Scrape recruiters at companies you applied to (gated by toggles)",
    )

    # slate-collect
    sub.add_parser(
        "slate-collect",
        help="Back up own SLATE submissions + discussion posts (gated)",
    )

    # slate-style
    sub.add_parser(
        "slate-style",
        help="Distill collected writing into a voice profile (gated)",
    )

    # outreach-drafts
    p_drafts = sub.add_parser("outreach-drafts", help="Generate outreach drafts (gated)")
    p_drafts.add_argument("--force", action="store_true",
                          help="Regenerate drafts even if they exist")

    # outreach-list
    sub.add_parser("outreach-list", help="Show recruiter records and drafts")

    # outreach-send
    sub.add_parser("outreach-send", help="Send queued drafts (heavily gated)")

    # config
    sub.add_parser("config", help="Show current feature toggles")

    # doctor
    sub.add_parser("doctor", help="Check every pipeline piece (non-invasive)")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "scrape":
        asyncio.run(cmd_scrape(args))
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "apply":
        asyncio.run(cmd_apply(args))
    elif args.command == "apply-all":
        asyncio.run(cmd_apply_all(args))
    elif args.command == "apply-batch":
        asyncio.run(cmd_apply_batch(args))
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "set-url":
        cmd_set_url(args)
    elif args.command == "tailor":
        cmd_tailor(args)
    elif args.command == "linkedin-collect":
        cmd_linkedin_collect(args)
    elif args.command == "slate-collect":
        cmd_slate_collect(args)
    elif args.command == "slate-style":
        cmd_slate_style(args)
    elif args.command == "outreach-drafts":
        cmd_outreach_drafts(args)
    elif args.command == "outreach-list":
        cmd_outreach_list(args)
    elif args.command == "outreach-send":
        cmd_outreach_send(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
