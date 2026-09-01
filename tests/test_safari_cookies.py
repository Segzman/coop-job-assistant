"""Round-trip test for the Safari binarycookies parser.
Run: .venv/bin/python -m pytest tests/ -q   (or just: python tests/test_safari_cookies.py)
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.safari_cookies import _parse_binarycookies, _match


def _mk_cookie(domain, name="sess", value="abc123") -> bytes:
    """Build one binarycookies record the way Safari writes it.
    Layout (offsets relative to rec[0], which includes the 4-byte
    self-referential size):
      rec[0:4]  = size (self-referential)
      rec[4:16] = 3 unknown/reserved uint32s
      rec[16:32]= 4 offsets (url, name, path, value)
      rec[32:56]= 24 bytes reserved
      rec[56:]  = NUL-terminated strings
    """
    url = domain.encode() + b"\x00"
    nm = name.encode() + b"\x00"
    path = b"/\x00"
    val = value.encode() + b"\x00"
    base = 56  # strings start here (relative to rec[0])
    header = struct.pack(
        "<IIIIIII",
        0, 0x4230, 0,     # 3 unknown/reserved
        base, base + len(url), base + len(url) + len(nm),
        base + len(url) + len(nm) + len(path),
    )
    body = header + b"\x00" * 24 + url + nm + path + val
    return struct.pack("<I", 4 + len(body)) + body


def _fake_file(domains) -> bytes:
    n = len(domains)
    base = 8 + 4 * n  # cookies start right after page header + offsets array
    page_body, off_list = b"", []
    for dom in domains:
        # offset points at the cookie record start (including its 4-byte size)
        off_list.append(base + len(page_body))
        page_body += _mk_cookie(dom)
    page = (struct.pack("<I", 0x00000100)
            + struct.pack("<I", n)
            + struct.pack(f"<{n}I", *off_list) + page_body)
    return b"cook" + struct.pack(">I", 1) + struct.pack(">I", len(page)) + page


def test_roundtrip_and_filter():
    cookies = _parse_binarycookies(
        _fake_file([".sheridancollege.ca", ".indeed.com",
                    "www.linkedin.com", ".google.com"]))
    assert len(cookies) == 4
    assert [(c["domain"], c["value"]) for c in cookies] == [
        (".sheridancollege.ca", "abc123"), (".indeed.com", "abc123"),
        ("www.linkedin.com", "abc123"), (".google.com", "abc123")]

    matched = [c for c in cookies if _match(c["domain"])]
    assert len(matched) == 3                      # google excluded
    domains = {c["domain"] for c in matched}
    assert ".sheridancollege.ca" in domains
    assert ".indeed.com" in domains
    assert any(d.endswith("linkedin.com") for d in domains)
    assert ".google.com" not in domains


def test_session_cookie_still_parsed():
    """Session cookies (no expiry) still appear; domain is the key field."""
    cookies = _parse_binarycookies(_fake_file([".indeed.com"]))
    assert len(cookies) == 1
    assert cookies[0]["domain"] == ".indeed.com"


def test_real_file_yields_domains():
    """Sanity check against the actual Safari cookie file (if accessible)."""
    src = Path.home() / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"
    if not src.exists():
        print("(skipping real-file test — Safari cookie file not accessible)")
        return
    cookies = _parse_binarycookies(src.read_bytes())
    print(f"parsed {len(cookies)} cookies from Safari")
    domains = {c["domain"] for c in cookies if c["domain"]}
    print("unique domains:", len(domains))
    assert len(cookies) > 0
    assert len(domains) > 10


if __name__ == "__main__":
    test_roundtrip_and_filter()
    test_session_cookie_still_parsed()
    print("PARSER OK")
