"""
Import Safari cookies → Playwright sessions.

Parses macOS Safari's Cookies.binarycookies (requires Full Disk Access
for your terminal), filters the domains this tool uses, and writes:
  - data/sheridan_cookies.json   (picked up by scrapers/sheridan.py and
                                  browser/apply.py automatically)
  - data/imported_cookies.json   (injected by indeed/linkedin scrapers)

Caveat: Cloudflare/LinkedIn sometimes bind sessions to browser
fingerprint. If a site bounces the stolen cookies, fall back to signing
in once inside the tool's own browser window — it persists anyway.

Usage: python main.py import-cookies
"""
from __future__ import annotations
import struct
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"

SAFARI_COOKIE_PATHS = [
    Path.home() / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
    Path.home() / "Library/Cookies/Cookies.binarycookies",
]

DOMAIN_FILTERS = (
    "sheridancollege.ca",       # Sheridan Works
    "login.microsoftonline.com",# SSO session behind Sheridan
    "indeed.com",               # incl. ca.indeed.com
    "linkedin.com",             # incl. www.linkedin.com
)


def _parse_binarycookies(data: bytes) -> list[dict]:
    """Parse macOS Safari Cookies.binarycookies.

    Each page is a 4096-byte page containing a cookie table:
      page[0:4]   = 0x00000100 marker
      page[4:8]   = numCookies (LE)
      page[8:8+4n] = cookie offsets (LE, each points to a cookie record)

    Each cookie record:
      rec[0:4]    = size of this record (LE, self-referential)
      rec[4:16]   = reserved/unknown (4 uint32s)
      rec[16:20]  = offset to URL domain string (relative to rec[0])
      rec[20:24]  = offset to name
      rec[24:28]  = offset to path
      rec[28:32]  = offset to value
      rec[32:56]  = 2 x 8-byte dates (creation/expiry) — unused here
      rec[56:]    = NUL-terminated strings: url, name, path, value
    """
    if data[:4] != b"cook":
        raise ValueError("Not a binarycookies file")
    (num_pages,) = struct.unpack(">I", data[4:8])
    sizes = struct.unpack(f">{num_pages}I", data[8:8 + 4 * num_pages])

    cookies: list[dict] = []
    pos = 8 + 4 * num_pages
    for size in sizes:
        page = data[pos:pos + size]
        pos += size
        try:
            _, num_cookies = struct.unpack("<II", page[:8])
            offsets = struct.unpack(
                f"<{num_cookies}I", page[8:8 + 4 * num_cookies]
            )
        except struct.error:
            continue

        for off in offsets:
            try:
                (csize,) = struct.unpack("<I", page[off:off + 4])
                rec = page[off:off + csize]
                # String offsets are relative to rec[0] (start of
                # the cookie record, including its 4-byte size).
                url_off, = struct.unpack("<I", rec[16:20])
                name_off, = struct.unpack("<I", rec[20:24])
                path_off, = struct.unpack("<I", rec[24:28])
                val_off, = struct.unpack("<I", rec[28:32])

                def _str(base: int) -> str:
                    end = rec.index(b"\x00", base)
                    return rec[base:end].decode("utf-8", "replace")

                domain_full = _str(url_off)
                name = _str(name_off)
                path = _str(path_off)
                value = _str(val_off)

                # Domain: strip scheme if present, take hostname
                domain = domain_full.split("//")[-1].split("/")[0]

                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path or "/",
                    "expires": -1,
                    "httpOnly": False,
                    "secure": False,
                })
            except Exception:
                continue
    return cookies


def _match(cookie_domain: str) -> bool:
    return any(cookie_domain.endswith(d) for d in DOMAIN_FILTERS)


def import_from_safari() -> tuple[int, dict[str, int]]:
    """Returns (total_imported, per-site counts)."""
    src = next((p for p in SAFARI_COOKIE_PATHS if p.exists()), None)
    if src is None:
        raise FileNotFoundError(
            "Cookies.binarycookies not found. Grant Full Disk Access to "
            "your terminal app (System Settings → Privacy & Security → "
            "Full Disk Access), then retry."
        )

    all_cookies = _parse_binarycookies(src.read_bytes())
    matched = [c for c in all_cookies if _match(c["domain"])]
    if not matched:
        print("[cookies] File parsed but no matching domains found — "
              "are you logged in on Safari?")
        return 0, {}

    counts: dict[str, int] = {}
    for site in DOMAIN_FILTERS:
        counts[site] = sum(1 for c in matched if c["domain"].endswith(site))

    DATA.mkdir(exist_ok=True)

    sheridan = [c for c in matched
                if c["domain"].endswith(("sheridancollege.ca",
                                         "login.microsoftonline.com"))]
    (DATA / "sheridan_cookies.json").write_text(
        __import__("json").dumps(sheridan, indent=2))

    (DATA / "imported_cookies.json").write_text(
        __import__("json").dumps(matched, indent=2))

    return len(matched), counts


def load_imported() -> list[dict]:
    """Imported cookies for indeed/linkedin persistent contexts."""
    f = DATA / "imported_cookies.json"
    if not f.exists():
        return []
    return __import__("json").loads(f.read_text())


def matching(platform: str) -> list[dict]:
    """Imported cookies for one platform: sheridan | indeed | linkedin."""
    suffixes = {
        "sheridan": ("sheridancollege.ca", "login.microsoftonline.com"),
        "indeed":   ("indeed.com",),
        "linkedin": ("linkedin.com",),
    }[platform]
    return [c for c in load_imported()
            if any(c["domain"].endswith(s) for s in suffixes)]
