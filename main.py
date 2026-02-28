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

from scrapers.sheridan import SheridanScraper
from scrapers.indeed import IndeedScraper
from storage.jobs import load_jobs, save_jobs, merge_new_jobs, update_status, update_external_url, filter_jobs
from browser.apply import open_and_prefill
from ui.display import print_job_table, print_job_detail

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

    await open_and_prefill(job)

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

        await open_and_prefill(job)

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
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "set-url":
        cmd_set_url(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
