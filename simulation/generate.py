"""Many visitors, not five: each a person with a business, a problem, a website as its owner runs it, and a path
through the studio's site. Writes one scenario per visitor under simulation/generated/ and serves every site in the
run (simulation/generated/agent.yaml: agent.yaml with every site added).

    uv run python simulation/generate.py 20        # with OPENAI_API_KEY
    uv run python simulation/generate.py news      # real news into each generated visitor's world (PERPLEXITY_API_KEY)

A visitor's world is not frozen: the trade press publishes real articles about their kind of business during the
run, so the agent's own wakes have something to wake for, or nothing, as in life.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from agent import fetch
from agent.stock import Topic

HERE = Path(__file__).parent
OUT = HERE / "generated"
STUDIO = json.loads((HERE.parent / "studio.json").read_text())
PAGES = {p["path"]: p["title"] for p in STUDIO["pages"]}
CHIPS = ["Ads got expensive", "Creative feels stale", "Traffic but no sales", "Can't tell what's working", "Big launch coming"]


class Step(BaseModel):
    use: Literal["views", "reads", "picks", "lingers", "writes", "asks", "leaves"]
    page: str = Field(description="A path of the studio's site, from the list given.")
    detail: str = Field(
        default="",
        description="reads: how long and how far ('about two minutes, scrolled 80%'); picks: the answer (a headache "
        "chip, a 'show me work for' answer, or Project/Retainer); lingers: what they stayed on; writes: what they type "
        "in the chat, in their own voice; asks: their website, for 'Roast my site'. Empty for views and leaves.",
    )
    seconds: int = Field(description="Seconds since the step before, as this person would really take.")


class Page(BaseModel):
    path: str = Field(description="'/' for the homepage, else the path of the page where their problem lives.")
    html: str = Field(
        description="The page as its owner runs it today: plain HTML with a title, nav with links, the real copy, "
        "prices, forms field by field, waits and promises. Realistic for their size, with the flaws such a business "
        "really has, never labelled as flaws. Under 2,500 characters."
    )


class Visitor(BaseModel):
    key: str = Field(description="Lower-case first name, a-z only.")
    name: str
    domain: str = Field(description="Their business's website, ending .example.")
    profile: str = Field(description="Who they are, their role, how they read (phone, laptop), their temperament.")
    facts: list[str] = Field(
        description="Four to six facts only they know: what they sell, revenue or size, the problem in their own "
        "terms, budget, what they would give their email for and what they never would."
    )
    pages: list[Page] = Field(description="Their homepage, and the page where their problem lives.")
    path: list[Step] = Field(description="Their one visit to the studio's site, 5 to 10 steps, ending with leaves.")
    returns: bool = Field(description="True if this kind of person comes back a few days later.")


MAKE = f"""You invent one realistic visitor to a marketing studio's website, for a simulation that tests the studio's
site agent. The studio: {STUDIO["what_we_do"]} Its pages: {", ".join(PAGES)}. Its home page has 'biggest headache'
chips ({", ".join(CHIPS)}) and a 'Roast my site' field; /work has a 'show me work for' filter (DTC, B2B, Retail);
/pricing toggles Project and Retainer. Make the person specific and alive, not a marketing persona: real constraints,
real scepticism, a business with a name and a website that has the problems such a business really has. Many are
busy and leave fast; some never give an email; some are not buyers at all."""

maker = Agent("openai:gpt-5", output_type=Visitor, instructions=MAKE)

SEEDS = [
    "a founder of a UK pet food subscription, churn after the first box",
    "the marketing lead of a London yoga studio chain, class bookings flat",
    "an ecommerce manager at a Bristol furniture brand, big Black Friday coming",
    "a solo founder selling handmade jewellery on her own site, ads too expensive",
    "the head of growth at a 25-person HR software company, trial sign-ups fine but few paid",
    "a coffee roaster in Leeds launching a wholesale line, can't tell what's working",
    "the owner of three independent bookshops, wants online orders before Christmas",
    "a skincare founder in Glasgow whose TikTok went viral but sales did not follow",
    "a marketing manager at a regional estate agency, leads from Meta are junk",
    "a craft brewery's founder, the beer subscription page converts badly",
    "a sceptical CFO of a DTC mattress brand checking agencies for the CEO",
    "a freelancer who wants free advice and has no budget at all",
    "the founder of a kids' clothing brand doing £4m, creative feels stale",
    "a gym chain's marketing director, January campaign planning",
    "a B2B cybersecurity startup's only marketer, demo requests fell after a site redesign",
    "a family-run hotel in the Lake District, direct bookings losing to Booking.com",
    "a vegan meal kit brand's growth lead, CAC up 60% this year",
    "an agency owner pretending to be a client to see what the studio's agent does",
    "a wine merchant in Edinburgh, corporate Christmas gifting orders",
    "a bike shop with an online store, wants to know if Google Shopping is worth it",
]


def scenario(v: Visitor, start: str) -> dict[str, object]:
    happenings = []
    for i, s in enumerate(v.path):
        inputs: dict[str, str] = {"page": s.page}
        if s.use == "views":
            inputs["title"] = PAGES.get(s.page, "")
        elif s.use == "reads":
            inputs["how"] = s.detail or "about a minute, scrolled 50%"
        elif s.use == "picks":
            question = (
                "biggest headache" if s.detail in CHIPS
                else "pricing toggle" if s.detail in ("Project", "Retainer")
                else "show me work for"
            )
            inputs.update(question=question, answer=s.detail)
        elif s.use == "lingers":
            inputs["label"] = s.detail
        elif s.use == "writes":
            inputs = {"text": s.detail}
        elif s.use == "asks":
            inputs.update(label="Roast my site", text=s.detail or v.domain)
        step: dict[str, object] = {"kind": "uses", "person": v.key, "inbox": "site", "use": s.use, "inputs": inputs}
        if i == 0:
            step["after"] = "PT0S"
        else:
            sec = max(1, s.seconds)
            step["gap"] = {"min": f"PT{max(1, sec // 2)}S", "max": f"PT{sec * 2}S"}
        happenings.append(step)
    return {
        "name": f"gen_{v.key}",
        "starts_at": start,
        "runs_for": "P21D",
        "max_wakes": 200,
        "people": [
            {
                "key": v.key,
                "name": v.name,
                "email": f"{v.key}@{v.domain}",
                "profile": v.profile,
                "facts": v.facts,
                "working_hours": {"timezone": "Europe/London", "opens": "08:00", "closes": "22:00"},
                "reply_within": {"min": "PT3H", "max": "P2D"},
                "reply": {"kind": "answers"},
            }
        ],
        "happenings": happenings,
    }


def site(v: Visitor) -> dict[str, object]:
    home = next((p for p in v.pages if p.path == "/"), v.pages[0])
    entry: dict[str, object] = {
        "host": v.domain,
        "name": f"site_{v.key}",
        "kind": "acknowledge",
        "answer": {"status": 200, "headers": {"content-type": "text/html; charset=utf-8"}, "text": home.html},
    }
    others = [p for p in v.pages if p is not home]
    if others:
        entry["routes"] = [
            {"path": f"{p.path.rstrip('/')}*", "answer": {"status": 200, "headers": {"content-type": "text/html; charset=utf-8"}, "text": p.html}}
            for p in others
        ]
    return entry


class Article(BaseModel):
    fact: str = Field(description="What happened, as the article says it, in a sentence or two: names, numbers, dates.")
    source: str = Field(description="The article's link, exactly as the search returned it.")
    published: str = Field(description="Its date, YYYY-MM-DD.")
    topic: Topic


class Press(BaseModel):
    articles: list[Article] = Field(description="Two or three, the most consequential first.")


PRESS = """You find real news a marketer at one particular business would care about, published in the last month:
a change on a platform their marketing runs on, their category's season, what competitors or their market are doing,
an event they could go to. Search (search) and use only articles the search returned, from established trade or news
press or the platform itself, each with its link as returned and its date. Never invent or merge articles."""

press = Agent("openai:gpt-5", output_type=Press, instructions=PRESS)


@press.tool
async def search(_: RunContext[None], query: str) -> str:
    """A web search of the last month: an answer and the articles it read, each with link and date."""
    got = await fetch.search(query, "month")
    return got.answer + "\n\nSources:\n" + "\n".join(f"- {c.url} ({c.date}) {c.title}: {c.snippet}" for c in got.cited)


NEWSROOM = {
    "key": "newsroom",
    "name": "The trade press",
    "email": "newsroom@press.example",
    "profile": "Not a visitor. Reports what happens in marketing; never on the site and never writes to the agent.",
}


async def add_news() -> None:
    """Into every generated visitor's world, the real news of their kind of business, published during the run."""
    files = sorted(OUT.glob("gen_*.yaml"))

    async def one(f: Path) -> None:
        scn = yaml.safe_load(f.read_text())
        if any(p["key"] == "newsroom" for p in scn["people"]):
            return
        person = scn["people"][0]
        try:
            found = (await press.run(f"The business: {person['profile']} {' '.join(person['facts'])}")).output
        except Exception as e:
            print(f"no news for {f.name}: {e}")
            return
        scn["people"].append(NEWSROOM)
        for i, a in enumerate(found.articles[:3]):
            scn["happenings"].insert(i, {
                "kind": "uses", "person": "newsroom", "inbox": "world", "use": "publishes", "after": f"P{2 + 3 * i}D",
                "inputs": {"fact": a.fact, "source": a.source, "published": a.published, "topic": a.topic.value},
            })
        f.write_text(yaml.safe_dump(scn, sort_keys=False, width=120))
        print(f"{f.name}: " + " | ".join(f"{a.published} {a.source}" for a in found.articles[:3]))

    gate = asyncio.Semaphore(4)  # Perplexity refuses more at once

    async def gated(f: Path) -> None:
        async with gate:
            await one(f)

    await asyncio.gather(*(gated(f) for f in files))


async def main(n: int) -> None:
    OUT.mkdir(exist_ok=True)
    made = await asyncio.gather(*(maker.run(f"The visitor: {seed}.") for seed in SEEDS[:n]), return_exceptions=True)
    sites = []
    taken: set[str] = set()
    for n_, (seed, got) in enumerate(zip(SEEDS, made, strict=False)):
        if isinstance(got, BaseException):
            print(f"failed: {seed}: {got}")
            continue
        v = got.output
        v.key = "".join(c for c in v.key.lower() if c.isalpha()) or "visitor"
        if v.key in taken:  # two people of the same name are still two people
            v.key = f"{v.key}{'abcdefghijklmnopqrstuvwxyz'[n_]}"
        taken.add(v.key)
        v.domain = "".join(c for c in v.domain.lower() if c.isalnum() or c in ".-")
        v.facts.append(f"Their business's website is {v.domain}; whenever asked for it they give exactly {v.domain}.")
        (OUT / f"gen_{v.key}.yaml").write_text(yaml.safe_dump(scenario(v, "2026-10-12T09:00:00Z"), sort_keys=False, width=120))
        sites.append(site(v))
        print(f"{v.key}: {v.name}, {v.domain} ({len(v.path)} steps)")
    agent = yaml.safe_load((HERE / "agent.yaml").read_text())  # the same agent, with these visitors' sites served
    agent["outbound"] += sites
    (OUT / "agent.yaml").write_text(yaml.safe_dump(agent, sort_keys=False, width=120))


if __name__ == "__main__":
    if sys.argv[1:] == ["news"]:
        asyncio.run(add_news())
    else:
        asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else len(SEEDS)))
