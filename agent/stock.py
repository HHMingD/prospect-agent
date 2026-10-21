"""What the studio's desk has found that is worth telling anyone: events, platform changes, go-to-market news. Kept in
one JSON file, so the site agent, every simulated run and the desk itself read the same stock."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

STOCK = Path(os.environ.get("DESK_STOCK", "desk_stock.json"))
SEED = Path(os.environ.get("DESK_SEED", ""))  # a simulated run starts from the live stock and keeps its own
BEFORE = datetime(2000, 1, 1, tzinfo=UTC)


class Topic(StrEnum):
    EVENT = "an event a brand's founder or marketer could go to: a conference, meetup, workshop or webinar"
    PLATFORM = "a change on a platform brands buy or build on: Meta, Google, TikTok, Pinterest, Shopify, Klaviyo"
    GTM = "go-to-market news: what consumer brands are doing, a shift in how people buy, a season's numbers"


class Headache(StrEnum):
    """The site's own "biggest headache" chips: what a finding speaks to."""

    ADS_EXPENSIVE = "Ads got expensive"
    CREATIVE_STALE = "Creative feels stale"
    NO_SALES = "Traffic but no sales"
    CANT_TELL = "Can't tell what's working"
    LAUNCH = "Big launch coming"


class Stocked(BaseModel):
    fact: str
    source: str
    published: str = ""
    topic: Topic
    speaks_to: list[Headache] = Field(default_factory=list)
    happens: date | None = None  # an event's date
    found: datetime


class Stock(BaseModel):
    items: list[Stocked] = Field(default_factory=list)
    looked: datetime | None = None
    next_look: datetime | None = None
    why: str = ""

    def current(self, today: date) -> list[Stocked]:
        """What is still worth saying: an event that has not happened yet, and everything else."""
        return [s for s in self.items if s.happens is None or s.happens >= today]

    def shown(self, today: date) -> list[str]:
        return [
            f"- [{s.topic.name.lower()}"
            + (f", {s.happens:%a %d %b %Y}" if s.happens else "")
            + (f"; for: {', '.join(h.value for h in s.speaks_to)}" if s.speaks_to else "")
            + f"] {s.fact} ({s.source}{', ' + s.published if s.published else ''})"
            for s in self.current(today)
        ]


def load() -> Stock:
    if STOCK.exists():
        return Stock.model_validate_json(STOCK.read_bytes())
    if SEED.is_file():  # what was in stock before the run began is old to everyone in it, never news
        seeded = Stock.model_validate_json(SEED.read_bytes())
        for s in seeded.items:
            s.found = BEFORE
        return seeded
    return Stock()


def save(stock: Stock) -> None:
    STOCK.write_text(stock.model_dump_json(indent=1))
