from __future__ import annotations
import asyncio
import random
from abc import ABC, abstractmethod

from browser import safari_bridge as safari


class BaseScraper(ABC):

    SUMMER_2026_KEYWORDS = [
        "summer 2026", "summer2026", "s26",
        "may 2026", "june 2026", "july 2026",
        "summer term", "4-month", "16-week",
        "co-op", "coop", "intern", "internship",
    ]

    async def human_delay(self, min_ms: int = 800, max_ms: int = 2500) -> None:
        """Randomized pause that mimics human reading/thinking time."""
        await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)

    # ------------------------------------------------------------------
    # Safari form helpers (all JS, selector-fallback lists)
    # ------------------------------------------------------------------

    async def safari_fill(self, wid: int, selectors: list[str], value: str) -> bool:
        """
        Fill the first visible, empty input/textarea matching any selector.
        Fires input/change events so React/Angular pick it up.
        """
        sels = ", ".join(selectors)
        sels_js = json_dumps_value(sels)
        out = await asyncio.to_thread(
            safari.js, wid,
            f"""(() => {{
              const el = [...document.querySelectorAll({sels_js})]
                .find(e => e && e.offsetParent !== null
                  && (e.value === undefined || e.value === ""));
              if (!el) return "miss";
              el.focus();
              el.value = {json_dumps_value(value)};
              el.dispatchEvent(new Event("input", {{bubbles: true}}));
              el.dispatchEvent(new Event("change", {{bubbles: true}}));
              return "filled";
            }})()""",
        )
        return out.strip().strip('"') == "filled"

    async def safari_click(self, wid: int, selectors: list[str]) -> bool:
        """Click the first visible element matching any selector."""
        sels = ", ".join(selectors)
        sels_js = json_dumps_value(sels)
        out = await asyncio.to_thread(
            safari.js, wid,
            f"""(() => {{
              const el = [...document.querySelectorAll({sels_js})]
                .find(e => e && e.offsetParent !== null);
              if (!el) return "miss";
              el.scrollIntoView({{block: "center"}});
              el.click();
              return "clicked";
            }})()""",
        )
        return out.strip().strip('"') == "clicked"

    async def safari_present(self, wid: int, selectors: list[str]) -> bool:
        """True if any selector matches a visible element right now."""
        sels = ", ".join(selectors)
        sels_js = json_dumps_value(sels)
        out = await asyncio.to_thread(
            safari.js, wid,
            f"""(() => [...document.querySelectorAll({sels_js})]
              .some(e => e && e.offsetParent !== null) ? "yes" : "no")()""",
        )
        return out.strip().strip('"') == "yes"

    def is_summer_2026(self, text: str) -> bool:
        """Heuristic: does this text suggest a Summer 2026 / intern position?"""
        text_lower = text.lower()
        return any(kw in text_lower for kw in self.SUMMER_2026_KEYWORDS)

    @abstractmethod
    async def scrape(self) -> list:
        """Subclasses implement this and return list[Job]."""
        ...


def json_dumps_value(value: str) -> str:
    """Encode a Python string as a JS string literal (stdlib json)."""
    import json
    return json.dumps(value)
