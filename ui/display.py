from __future__ import annotations
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from models.job import Job

console = Console()

STATUS_COLORS: dict[str, str] = {
    "new":     "bright_green",
    "seen":    "yellow",
    "applied": "cyan",
    "skipped": "dim",
}


def print_job_table(jobs: list[Job]) -> None:
    if not jobs:
        console.print("[yellow]No jobs to display.[/]")
        return

    table = Table(
        title=f"{len(jobs)} job(s)",
        show_lines=True,
        expand=True,
        header_style="bold",
    )
    table.add_column("ID",       style="dim",   no_wrap=True, width=14)
    table.add_column("Status",   no_wrap=True,  width=9)
    table.add_column("Title",    min_width=24)
    table.add_column("Company",  min_width=14)
    table.add_column("Location", min_width=12)
    table.add_column("Deadline", no_wrap=True,  width=12)
    table.add_column("Platform", no_wrap=True,  width=10)

    for job in jobs:
        color = STATUS_COLORS.get(job.status, "white")
        table.add_row(
            job.id,
            f"[{color}]{job.status}[/]",
            job.title,
            job.company,
            job.location,
            job.deadline or "—",
            f"[bold cyan]{job.platform}[/]",
        )

    console.print(table)
    console.print(
        "[dim]Commands:  "
        "python main.py apply <ID>   "
        "python main.py status <ID> skipped[/]"
    )


def print_job_detail(job: Job) -> None:
    color = STATUS_COLORS.get(job.status, "white")
    lines = [
        f"[bold]{job.title}[/bold]",
        f"Company:   {job.company}",
        f"Location:  {job.location}",
        f"Platform:  [cyan]{job.platform}[/cyan]",
        f"Deadline:  {job.deadline or 'Not specified'}",
        f"Status:    [{color}]{job.status}[/{color}]",
        f"Found:     {job.date_found}",
    ]
    if job.date_applied:
        lines.append(f"Applied:   {job.date_applied}")
    if job.notes:
        lines.append(f"Notes:     {job.notes}")
    lines.append(f"URL:       [link={job.url}]{job.url}[/link]")
    if job.description:
        lines.append(f"\n[dim]{job.description[:400]}…[/dim]")

    console.print(Panel(
        "\n".join(lines),
        title=f"Job {job.id}",
        border_style="blue",
    ))
