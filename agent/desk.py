"""The studio's desk: research no visitor asked for. It keeps the agent stocked with things worth telling anyone who
walks in (an event coming up, a platform change, what brands are doing this season), checks each against the page it
cites, and decides for itself when to look again.

    uv run python -m agent.desk      one look now: prints what it kept and what it dropped
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx
from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.builtin_tools import WebSearchTool
from pydantic_ai.exceptions import UnexpectedModelBehavior

from agent import fetch
from agent.looks import (
    PLANNER_MODEL,
    QUICK_CALL,
    RESEARCH_MODEL,
    SEARCH_SOURCES,
    SLOW_CALL,
    Finding,
    _key,
    _sources,
    _stated,
)
from agent.stock import Headache, Stocked, Topic, load, save
from agent.studio import STUDIO

log = logging.getLogger("agent")
KEEP_FOR = timedelta(days=60)  # news older than this is no longer news
SOONEST, LATEST = timedelta(hours=6), timedelta(days=7)


class DeskFinding(BaseModel):
    fact: str = Field(
        description="One true, specific thing, in a sentence: for an event, what it is, who it is for, where and when."
    )
    source: str = Field(description="The link it came from (https://...): for an event, the organiser's own page.")
    published: str = Field(default="", description="Its publication date, YYYY-MM-DD (or YYYY-MM), if known.")
    topic: Topic
    speaks_to: list[Headache] = Field(description="Which of the visitors' headaches it speaks to; empty if none.")
    happens: date | None = Field(default=None, description="For an event: the day it starts. None otherwise.")

    @field_validator("source")
    @classmethod
    def _linked(cls, source: str) -> str:
        if not source.startswith(("http://", "https://")):
            raise ValueError("a finding carries the link it came from")
        return source


class DeskLook(BaseModel):
    findings: list[DeskFinding] = Field(description="What you found, best first. Empty if nothing real turned up.")
    next_look: datetime = Field(description="When to look again (UTC).")
    why: str = Field(description="Why then, in a sentence.")


DESK = f"""You keep {STUDIO.name}'s site agent stocked with things worth telling its visitors, before any visitor asks.
The visitors run or market consumer brands doing £1m-£30m a year (beauty, food and drink, home, some B2B software),
mostly in the UK; the studio is in {STUDIO.city}. Their headaches, as the site asks them: {", ".join(h.value for h in Headache)}.

Search for, in this order:
1. Events from three days to eight weeks from now that such a person would actually go to: a conference, meetup, workshop or webinar
   on paid social, ecommerce, retention, creative or growth, in the UK or online. The organiser's own page, the date,
   the place. Never an event that has happened.
2. Changes in the last 60 days on Meta, Google, TikTok, Pinterest, Shopify or Klaviyo that change how brands advertise
   or sell: from the platform's own announcement or established trade press.
3. Go-to-market news from the last 60 days: what consumer brands are doing this season, a shift in how people buy,
   a number from a source a marketer would trust.

Each look is about one of the three, named below: search the web for it at least twice, with the month and year in
the query. Never content farms, listicles, vendors' own blogs or agencies marketing themselves. Every finding carries the link
you got it from; never invent one. Nothing already in stock (below). Aim for about three of each kind; fewer
real ones beat padding, and an empty list is a fine answer.

Then choose when to look again: sooner when the stock is thin or an event in it is close, later when it is full and
fresh. Between {SOONEST.total_seconds() / 3600:.0f} hours and {LATEST.days} days from now."""


@dataclass
class Looking:
    cited: list[str] = field(default_factory=list)  # sources Perplexity searches returned


desk = Agent(
    RESEARCH_MODEL,
    deps_type=Looking,
    output_type=DeskLook,
    instructions=DESK,
    builtin_tools=[WebSearchTool()],
    retries=5,
    model_settings=SLOW_CALL,
)


@desk.tool
async def search_news(ctx: RunContext[Looking], query: str, recency: str = "month") -> str:
    """Search the web through Perplexity: an answer with its sources and their dates. Best for what is recent
    (`recency`: day, week, month or year). Cite only the sources it lists."""
    try:
        found = await fetch.search(query, recency)
    except (RuntimeError, httpx.HTTPError) as e:
        return f"Search failed ({e}); use your other web search."
    ctx.deps.cited += [c.url for c in found.cited]
    return found.model_dump_json()


@desk.output_validator
def _only_real_sources(ctx: RunContext[Looking], look: DeskLook) -> DeskLook:
    real = _sources(ctx.messages) | {_key(u) for u in ctx.deps.cited}
    if not real and ctx.retry < 2:
        raise ModelRetry("You have not searched yet. Search the web: at least once for each of the three kinds.")
    invented = [f.source for f in look.findings if _key(f.source) not in real]
    if invented and ctx.retry >= 2:
        log.warning("desk findings dropped, never fetched: %s", ", ".join(sorted(set(invented))))
        return look.model_copy(update={"findings": [f for f in look.findings if _key(f.source) in real]})
    if invented:
        raise ModelRetry(
            "These links were not returned by any search, so they cannot be cited: "
            + ", ".join(invented)
            + ". Cite only links you actually got."
        )
    return look


def _fresh(s: Stocked, now: datetime) -> bool:
    if s.happens is not None:
        return s.happens >= now.date()
    return now - s.found <= KEEP_FOR


async def _one(asked: str) -> DeskLook:
    """One kind of finding: a run that fails finds nothing, and the desk looks again tomorrow."""
    try:
        return (await desk.run(asked, deps=Looking(), model_settings=SEARCH_SOURCES)).output
    except UnexpectedModelBehavior as e:
        log.warning("desk found nothing usable: %s", e)
        return DeskLook(findings=[], next_look=datetime.now(UTC) + timedelta(days=1), why="a look failed; try tomorrow")


class WorldNews(BaseModel):
    """Something that happened in the world, as the trade press reports it: what the desk is told, not what it found."""

    fact: str
    source: str
    published: str = ""
    topic: Topic
    speaks_to: list[Headache] = Field(default_factory=list)
    happens: date | None = None


async def heard(news: WorldNews, now: datetime) -> Stocked | None:
    """News from outside joins the stock only if the page it cites says it, as any finding does."""
    stock = load()
    if any(_key(s.source) == _key(news.source) for s in stock.items):
        return None
    if not await _stated(Finding(fact=news.fact, source=news.source, published=news.published, why_it_matters="")):
        return None
    item = Stocked(**news.model_dump(), found=now)
    stock.items.append(item)
    save(stock)
    log.info("desk heard: %s", news.fact)
    return item


class Matters(BaseModel):
    visitor: str = Field(description="The visitor's id, exactly as given.")
    why: str = Field(description="Why this matters to them in particular, in a sentence.")


class Cares(BaseModel):
    who: list[Matters] = Field(description="Only the people this genuinely changes something for. Often nobody.")


WHO_CARES = """A marketing studio's desk just learned something new (below). For each person the studio knows (below),
decide whether it changes something for them: their channel, their season, their kind of business, a problem they
have. Name only those it genuinely does; a loose fit is a no."""

carer = Agent(PLANNER_MODEL, output_type=Cares, instructions=WHO_CARES, model_settings=QUICK_CALL)


async def who_cares(item: Stocked, people: list[tuple[str, str]]) -> list[Matters]:
    if not people:
        return []
    known = "\n".join(f"- {pid}: {about}" for pid, about in people)
    got = (await carer.run(f"New: {item.fact} ({item.source})\n\nThe people:\n{known}")).output.who
    ids = {pid for pid, _ in people}
    return [m for m in got if m.visitor in ids]


async def look(now: datetime) -> list[Stocked]:
    """One look: search, check every finding against its page, keep what stands, and choose the next look."""
    stock = load()
    stock.items = [s for s in stock.items if _fresh(s, now)]
    shown = "\n".join(stock.shown(now.date())) or "- nothing"
    asked = f"Now: {now:%A %d %B %Y} ({now.isoformat()})\n\nIn stock:\n{shown}"
    looks = await asyncio.gather(*(_one(f"{asked}\n\nThis look: {t.name.lower()} only: {t.value}.") for t in Topic))
    found = DeskLook(
        findings=[f for got in looks for f in got.findings],
        next_look=min(got.next_look for got in looks),
        why=" ".join(dict.fromkeys(got.why for got in looks)),
    )
    have = {_key(s.source) for s in stock.items}
    new = [f for f in found.findings if _key(f.source) not in have and (f.happens is None or f.happens >= now.date())]
    stated = await asyncio.gather(
        *(_stated(Finding(fact=f.fact, source=f.source, published=f.published, why_it_matters=f.topic)) for f in new)
    )
    kept = [f for f, ok in zip(new, stated, strict=True) if ok]
    stock.items += [
        Stocked(
            fact=f.fact,
            source=f.source,
            published=f.published,
            topic=f.topic,
            speaks_to=f.speaks_to,
            happens=f.happens,
            found=now,
        )
        for f in kept
    ]
    stock.looked = now
    stock.next_look = min(max(found.next_look.astimezone(UTC), now + SOONEST), now + LATEST)
    stock.why = found.why
    save(stock)
    log.info("desk kept %d of %d new finding(s); next look %s: %s", len(kept), len(new), stock.next_look, stock.why)
    return stock.items[len(stock.items) - len(kept) :]


async def keep_stocked(on_new: Callable[[list[Stocked]], Awaitable[None]]) -> None:
    """Look whenever the desk chose to, and only then; whatever it keeps goes to `on_new`."""
    while True:
        now = datetime.now(UTC)
        due = load().next_look
        if due is None or due <= now:
            try:
                await on_new(await look(now))
            except Exception:
                log.exception("desk look failed")
                await asyncio.sleep(3600)
            continue
        await asyncio.sleep(min((due - now).total_seconds(), 3600))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines carry query strings, keys included
    asyncio.run(look(datetime.now(UTC)))
    got = load()
    print("\n".join(got.shown(datetime.now(UTC).date())))
    print(f"next look {got.next_look}: {got.why}")
