from __future__ import annotations
import asyncio
import random
from abc import ABC, abstractmethod
from playwright.async_api import Page


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

    async def human_type(self, page: Page, selector: str, text: str) -> None:
        """Types text character-by-character with realistic inter-key delays."""
        await page.click(selector)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        for char in text:
            await page.keyboard.type(char)
            await asyncio.sleep(random.uniform(0.05, 0.18))

    def is_summer_2026(self, text: str) -> bool:
        """Heuristic: does this text suggest a Summer 2026 / intern position?"""
        text_lower = text.lower()
        return any(kw in text_lower for kw in self.SUMMER_2026_KEYWORDS)

    @abstractmethod
    async def scrape(self) -> list:
        """Subclasses implement this and return list[Job]."""
        ...
