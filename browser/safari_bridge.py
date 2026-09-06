"""
Real Safari automation via AppleScript (osascript).

Used INSTEAD of Playwright/Chromium for every web action: Safari carries
the user's real logged-in sessions and a genuine browser fingerprint —
no automation flags for bot walls to key on, no separate profiles.

Requires one-time setup in Safari:
  Develop menu → Allow JavaScript from Apple Events.
(If the Develop menu is hidden: Safari Settings → Advanced → Show Develop.)
File upload additionally needs Accessibility permission for the terminal
(System Settings → Privacy & Security → Accessibility) — macOS prompts
automatically on first use.

All functions target an explicit Safari window id so we never touch the
user's own windows.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path


def _q(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run(body: str, timeout: int = 60) -> str:
    """Run an AppleScript body via a temp file. Returns stdout stripped."""
    with tempfile.NamedTemporaryFile("w", suffix=".scpt", delete=False) as f:
        f.write(body)
        path = f.name
    try:
        p = subprocess.run(
            ["osascript", path],
            capture_output=True, text=True, timeout=timeout,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    if p.returncode != 0:
        raise RuntimeError(f"osascript failed: {p.stderr.strip()}")
    return p.stdout.strip()


def open_window(url: str) -> int:
    """Open a fresh Safari window at url. Returns the window id."""
    out = _run(
        'tell application "Safari"\n'
        "activate\n"
        f'make new document with properties {{URL:"{_q(url)}"}}\n'
        "delay 1\n"
        "return id of front window\n"
        "end tell"
    )
    return int(out)


def goto(wid: int, url: str) -> None:
    _run(
        'tell application "Safari"\n'
        f'set URL of current tab of window id {wid} to "{_q(url)}"\n'
        "end tell"
    )


def js(wid: int, script: str, timeout: int = 60) -> str:
    """Run JavaScript in the window's current tab. Returns stdout stripped."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
        js_path = f.name
    try:
        return _run(
            'tell application "Safari"\n'
            f'set jsSource to read (POSIX file "{_q(js_path)}")\n'
            f"return do JavaScript jsSource in current tab of window id {wid}\n"
            "end tell",
            timeout=timeout,
        )
    finally:
        Path(js_path).unlink(missing_ok=True)


def current_url(wid: int) -> str:
    return _run(
        'tell application "Safari"\n'
        f"return URL of current tab of window id {wid}\n"
        "end tell"
    )


def close_window(wid: int) -> None:
    try:
        _run(
            'tell application "Safari"\n'
            f"close window id {wid}\n"
            "end tell"
        )
    except RuntimeError:
        pass  # already closed by the user — fine


def new_tab(wid: int, url: str) -> None:
    """Open url in a new tab of our window (does not steal focus)."""
    _run(
        'tell application "Safari"\n'
        f'tell window id {wid}\n'
        f'set t to make new tab at end of tabs with properties {{URL:"{_q(url)}"}}\n'
        "end tell\n"
        "end tell"
    )


def activate_tab(wid: int, index: int) -> None:
    """Bring tab (1-based) of our window to the front."""
    _run(
        'tell application "Safari"\n'
        f"set current tab of window id {wid} to tab {index} of window id {wid}\n"
        "end tell"
    )


def tab_count(wid: int) -> int:
    try:
        return int(_run(
            'tell application "Safari"\n'
            f"return count of tabs of window id {wid}\n"
            "end tell"
        ))
    except RuntimeError:
        return 0


def window_exists(wid: int) -> bool:
    try:
        return _run(
            'tell application "Safari"\n'
            f"return exists window id {wid}\n"
            "end tell"
        ).lower() == "true"
    except RuntimeError:
        return False


def wait_window_closed(wid: int, poll_s: float = 5.0) -> None:
    """Block until the user closes our window. Ctrl-C aborts."""
    while window_exists(wid):
        time.sleep(poll_s)


def focus() -> None:
    _run('tell application "Safari"\nactivate\nend tell')


# ---------------------------------------------------------------------------
# File upload (native open-dialog driven via System Events)
# ---------------------------------------------------------------------------

def upload_file(wid: int, path: str, click_js: str | None = None) -> bool:
    """
    Drives the native Open dialog with keystrokes: Cmd+Shift+G → type
    path → Return → Return.

    If click_js is given, it is run first to open the dialog (e.g. click
    a specific <input type=file>). Pass None when the dialog is already
    open.

    Returns True if the dialog flow completed (caller should verify the
    page shows the file). Needs Accessibility permission for the terminal.
    """
    if click_js:
        js(wid, click_js)
        time.sleep(1.5)
    _run(
        'tell application "System Events"\n'
        'keystroke "g" using {command down, shift down}\n'
        "delay 0.8\n"
        f'keystroke "{_q(path)}"\n'
        "delay 0.5\n"
        "key code 36\n"          # Return — confirm Go to Folder
        "delay 0.8\n"
        "key code 36\n"          # Return — choose file
        "delay 0.8\n"
        "end tell",
        timeout=60,
    )
    return True
