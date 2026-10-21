"""One agent for each visitor, in two parts that work as one.

The planner reads every moment: what they just did, who they probably are, what they want. It decides whether the
agent acts now, and when to look at them again if they do nothing: in a minute while they read, in days for a known
person whose world is likely to change, or not until they come back.

The executor acts when the planner wakes it: it decides whether anything is worth saying, and says it, on the site or
by email, or hands them to the studio's people. It looks things up outside the studio (the web, their website,
Apollo) only when what it would say needs it.

    AGENT_PLANNER_MODEL, AGENT_EXECUTOR_MODEL (default openai-responses:gpt-6-luna), AGENT_RESEARCH_MODEL (default
    openai-responses:gpt-6-luna)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import cast

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.builtin_tools import WebSearchTool
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import BuiltinToolReturnPart, ModelMessage
from pydantic_ai.models.openai import OpenAIResponsesModelSettings

from agent import apollo, emails, fetch, mail, stock
from agent.studio import STUDIO, Source
from agent.visitors import Email, Message, Offer, OfferKind, Researched, Sender, Status, TouchKind, Visitor

log = logging.getLogger("agent")
PLANNER_MODEL = os.environ.get("AGENT_PLANNER_MODEL", "openai-responses:gpt-6-luna")
EXECUTOR_MODEL = os.environ.get("AGENT_EXECUTOR_MODEL", "openai-responses:gpt-6-luna")  # the writer of emails is its own
RESEARCH_MODEL = os.environ.get("AGENT_RESEARCH_MODEL", "openai-responses:gpt-6-luna")
CHECK_MODEL = os.environ.get("AGENT_CHECK_MODEL", "openai-responses:gpt-6-luna")
# gpt-6-luna takes tools (structured output is one) with reasoning only through the Responses API
QUICK_CALL = OpenAIResponsesModelSettings(timeout=30, openai_reasoning_effort="low")  # the visitor is on the page
SLOW_CALL = OpenAIResponsesModelSettings(timeout=120, openai_reasoning_effort="low")
SITE_WORDS = 45  # a remark on the site is read in passing
ON_SITE = timedelta(seconds=60)  # seen this recently: they are on the site now
LONGEST_WAIT = timedelta(days=14)  # a watch on someone's world is renewed at least this often

VOICE = f"""\
You are the voice of {STUDIO.name}, a marketing studio, on its own website. You are the studio's AI agent: if they
ask, say so plainly, and never claim or joke that you are a person. You never ask "how can I help?", never greet for
the sake of it, never pitch. You do not know who a visitor is until they tell you, and you are honest about it. Not every line has to carry substance: a person sitting next to them would just as
often make a quick joke about what they are looking at, react to what they said, or tease the studio's own page, and
that small talk is what makes the useful things land. Sometimes it is a sharp observation, a story from the studio's
work, an honest critique, a fresh fact; sometimes it is banter. Witty, warm, a little self-aware; the way the
studio's best strategist would talk if they sat next to the visitor. On the site: at most two sentences, about 40
words. Never repeat yourself. Never invent a fact, number, client, quote or piece of news: use the studio brief below,
what the visitor told you, and what research found, and nothing else.

The aim is to be useful enough that they want to tell you who they are (name, email, their website), so the studio
can keep being useful to them by email. Earn it; do not ask for it before you have given them something.

The studio:
{STUDIO.brief()}"""


def on_site(v: Visitor, now: datetime) -> bool:
    seen = v.last_seen()
    return seen is not None and now - seen <= ON_SITE


def shown(v: Visitor, now: datetime) -> str:
    """Everything known about this visitor, as the agent reads it."""
    events: list[tuple[datetime, str]] = []
    for t in v.touches:
        if t.kind is TouchKind.PRESENCE:
            continue
        parts = [str(t.kind)]
        if t.page:
            parts.append(t.page + (f' "{t.title}"' if t.title else ""))
        if t.label:
            parts.append(f"[{t.label}]")
        if t.text:
            parts.append(f'"{t.text}"')
        if t.seconds is not None:
            parts.append(f"{t.seconds}s on page")
        if t.scroll is not None:
            parts.append(f"scrolled {t.scroll}%")
        if t.referrer:
            parts.append(f"from {t.referrer}")
        if t.campaign:
            parts.append(f"campaign {t.campaign}")
        events.append((t.at, f"they {' '.join(parts)}{_new(v, t.at)}"))
    for m in v.thread:
        if m.sender is Sender.VISITOR:
            how = f"through the page's '{m.asked_through}' field" if m.asked_through else "in the chat"
            events.append((m.at, f"they wrote to you {how}: \"{m.text}\"{_new(v, m.at)}"))
        else:
            events.append((m.at, f"YOU said on the site{_offered(m)}: {m.text} ({_landed(v, m)})"))
    for e in v.emails:
        events.append((e.at, f'YOU emailed them, subject "{e.subject}": {e.text}'))
    events.sort(key=lambda e: e[0])
    first = v.touches[0] if v.touches else None
    lines = [
        f"Now: {now:%A %d %B %Y %H:%M:%S} UTC ({now.isoformat()})",
        f"Visitor {v.id}: {v.status}"
        + (f", {v.name} <{v.email}>" if v.email else ", name and email unknown")
        + (f", their own business's website: {v.website}" if v.website else ""),
        f"On the site right now: {'yes' if on_site(v, now) else 'no'}. Visits so far: {v.visits()}.",
    ]
    if first is not None:
        lines.append(
            f"Device {first.device or 'unknown'}, timezone {first.timezone or 'unknown'}, "
            f"first came from {first.referrer or 'direct'}"
        )
    if v.company is not None:
        lines.append(f"Enrichment: {v.company.model_dump_json(exclude_defaults=True)}")
    lines += [
        f"What is believed about them: {v.beliefs or 'nothing yet'}",
        f"Their line of work, as they told it: {v.line_of_work or 'not said yet'}",
        f"What the agent is preparing for them: {v.preparing or 'nothing'}",
        f"The executor has acted for them {v.main_wakes} time(s). The next look the planner chose: "
        f"{v.wake_at.isoformat() if v.wake_at else 'none set'}" + (f", because: {v.wake_why}" if v.wake_why else ""),
        f"Their business: {v.their_business or 'unknown'}",
        "What research found so far:",
        *(
            f"- {r.at:%d %b}: {r.fact} ({r.source}{', published ' + r.published if r.published else ''})"
            for r in v.research[-12:]
        ),
        "What the studio's desk found lately, for anyone (events, platform changes, go-to-market news):",
        *(stock.load().shown(now.date()) or ["- nothing yet"]),
        "Everything that happened, oldest first:",
        *(f"- {at:%a %d %b %H:%M:%S} {text}" for at, text in events[-120:]),
        "",
        *_where_they_are(v, now),
    ]
    return "\n".join(lines)


def _new(v: Visitor, at: datetime) -> str:
    return " (NEW)" if v.looked_until is None or at > v.looked_until else ""


def _offered(m: Message) -> str:
    if m.offer is None:
        return ""
    if m.offer.kind is OfferKind.PERSON:
        return f" (offered {STUDIO.lead_name} getting in touch about {m.offer.what}, with a form for their name and email)"
    return f" (offered to email them {m.offer.what}, with a form for their name and email)"


def _landed(v: Visitor, m: Message) -> str:
    """How one of its messages landed: dismissed, unseen, seen and answered, or seen and left."""
    if m.dismissed_at is not None:
        return f"they deleted it at {m.dismissed_at:%H:%M:%S}"
    if m.id > v.read_through:
        return "they have not opened it"
    if any(x.sender is Sender.VISITOR and x.id > m.id for x in v.thread):
        return "seen; they wrote after it"
    return "seen; no reply"


def _where_they_are(v: Visitor, now: datetime) -> list[str]:
    """The page in front of them, and where the conversation stands."""
    lines: list[str] = []
    views = [t for t in v.touches if t.kind is TouchKind.VIEW]
    if views:
        here = views[-1]
        page = STUDIO.page(here.page)
        lines.append(
            f"The page in front of them: {here.page}, opened {int((now - here.at).total_seconds())}s ago."
            + (f" It already shows them: {page.what}" if page else "")
        )
    said = [m for m in v.thread if m.sender is Sender.AGENT]
    if said:
        last = said[-1]
        lines.append(
            f"Your last message on the site was {int((now - last.at).total_seconds())}s ago ({_landed(v, last)}). "
            f"Messages you have sent on the site: {len(said)}; they have written to you {sum(1 for m in v.thread if m.sender is Sender.VISITOR)} time(s); "
            f"deleted: {sum(1 for m in said if m.dismissed_at is not None)}."
        )
    unread = v.unread()
    if unread:
        lines.append(
            f"Waiting unopened for them ({len(unread)}): " + " | ".join(m.text[:80] for m in unread)
        )
    return lines


# --- the planner: what this moment is, whether the executor acts now, when to look again ------------------------


class Moment(StrEnum):
    TALKING = "they wrote to the agent, in the chat or through the page's own field: the executor is answering"
    CHOOSING = "they chose something that says what they care about: a headache, a filter, a toggle, a long stay"
    BROWSING = "they are moving through the site; nothing they did is addressed to the agent"
    LEAVING = "they are about to leave, or have left"
    INTRODUCED = "they just left their name and email"
    ANSWERED_EMAIL = "they answered an email"
    THEIR_WORLD = "something changed in their world: news, or a moment you chose to look again"
    NOTHING = "nothing new since you last looked"


class Plan(BaseModel):
    reading: str = Field(
        description="What this moment is and what it tells you: who they probably are and what they want, each point "
        "with the action of theirs it came from, and how sure you are."
    )
    line_of_work: str = Field(
        default="",
        description="What they do (their business, their role), from their own words or what they gave you. Empty "
        "when they have not said.",
    )
    moment: Moment
    on_screen: str = Field(
        description="What of the agent's is in front of them now and how it landed: read, answered, ignored, deleted."
    )
    speak: float = Field(
        ge=0,
        le=1,
        description="How much this moment calls for the agent to say or send something now, as a person sitting beside "
        "them would feel it: 0 nothing at all, 0.3 a word might be welcome, 0.6 something worth saying, 1 they are "
        "waiting for it. Never certain either way: weigh what it would give them against the interruption.",
    )
    brief: str = Field(
        default="",
        description="When speak is above 0.3: the situation, as you would brief a colleague: what they want, what they are owed "
        "(an email they left their address for, an answer to what they asked), what they have been given and how it "
        "landed. Never the words to say and never a promise: what to say or send is the executor's call.",
    )
    next_look: timedelta | None = Field(
        description="ISO 8601 duration from now (PT40S, PT10M, P3D) after which to look at them again even if they "
        "do nothing, or null when only something they do should bring you back."
    )
    next_look_why: str = Field(default="", description="When next_look is set: what you expect by then.")


PLAN = f"""You are the planner of {STUDIO.name}'s agent on its own website: one agent, two parts. You decide when it
pays attention to a visitor and whether it acts; the executor, the other part, decides what to say on the site or send
by email, and says it. You never speak to the visitor yourself.

You are called whenever they do something on the site, answer an email, or a moment you chose comes round. Read
everything that happened, then:
1. Read the moment: what they just did, what it says about who they are and what they want, and what they do for a
   living if they have said.
2. Say how much this moment calls for the agent to speak (speak, 0 to 1). It is never certain either way. It costs
   their attention more than money: whatever it says pops up over the page they came for, and one they have not
   opened is still waiting for them. When they write to the agent or leave
   their email, the executor answers them on its own: you never need to act for that. When they answered an email,
   they are owed an answer: act now. Otherwise act when a person beside them would say something: a point the page
   does not already make, or just a light word on what they are doing, banter included. Not every message has to
   carry substance.
3. Decide when to look again if they do nothing. Anything they do brings you back on its own, a reply to an email
   included, so a look is never to check for a reply or to follow up. It is for what silence means: someone reading a
   page for a while may be ready for something in a minute; someone who has left is back only when they come back,
   unless you know who they are and something in their world is likely to change (their season, a launch, news in their
   trade), in which case look then. Never further than {LONGEST_WAIT.days} days.

Trust is built in an order: first what they do, then who they are. Until you know their line of work, knowing it is
worth more than their email, and a stranger asked for their email too soon stops listening.

The executor works for whoever is here, anonymous or not. It is fast unless it has to look things up outside the
studio (their website, the news), which takes a minute or two.

The studio, so you know what it can offer: {STUDIO.what_we_do}"""


@dataclass
class Looking:
    v: Visitor
    now: datetime


planner = Agent(
    PLANNER_MODEL, deps_type=Looking, output_type=Plan, instructions=PLAN, model_settings=QUICK_CALL, retries=3
)


@planner.output_validator
def _plan_fits(ctx: RunContext[Looking], p: Plan) -> Plan:
    if p.speak > 0.3 and not p.brief:
        raise ModelRetry("This moment may call for the agent: brief the executor on what it calls for.")
    if p.next_look is not None and p.next_look <= timedelta(0):
        raise ModelRetry("next_look is a duration from now, greater than zero, or null.")
    if p.next_look is not None and p.next_look > LONGEST_WAIT:
        raise ModelRetry(f"Look again within {LONGEST_WAIT.days} days, or set no look and wait for them.")
    return p


async def plan(v: Visitor, now: datetime, why: str) -> Plan:
    return (await planner.run(f"Why you are looking now: {why}\n\n{shown(v, now)}", deps=Looking(v, now))).output


# --- the check: a quick yes or no before anything reaches the visitor ---------------------------------------------

LINK = re.compile(r"https?://[^\s)>\]]+")


class Backed(BaseModel):
    misleading: float = Field(
        ge=0,
        le=1,
        description="How likely the visitor comes away believing something made up: 0 nothing in it the sources do not "
        "support, 0.3 a loose or overconfident line, 0.7 a fact, result, price or promise nothing supports, 1 invented "
        "outright.",
    )
    unbacked: list[str] = Field(
        default_factory=list, description="The statements nothing backs, quoted from the text, most misleading first."
    )
    rests_on: list[int] = Field(
        default_factory=list, description="The numbers of the research or desk findings the text uses, if any."
    )


CHECK = f"""You give a quick read of one thing a marketing studio's agent is about to say to a visitor: would they come
away believing something the sources (the studio brief, the visitor's own words, research findings) do not support?

Statements of fact: a number, a share ("most", "nine out of ten"), a client result or a detail of a client's story,
what a page, a service or a price includes, anything about the visitor or their business, a piece of news. A fair
paraphrase of a source is backed. Two facts merged into a new one are not. A client's result is backed only by that
client's own story in the brief, never by the studio's general practice or another client's. What their own website
shows or how it works is backed only by a page research read. A commitment the studio's people would have to keep (a
call, a start date, a deadline, a proposal, mockups, a price) is backed only if the brief offers exactly it. Naming
or recommending another agency, studio or freelancer is never backed, and neither is telling them again a fact or
piece of news they were already told (a source marked "already told them").

Not statements of fact, so always fine: opinions, judgements, jokes, questions; advice and recommendations however
they are phrased, general marketing know-how stated as a rule included ("check whether the ads have gone stale",
"fresh concepts give the account something to learn", "land each ad on the product it promised"); what the agent says
about itself (that it is the studio's AI agent, what it can do or will write here); offering that {STUDIO.lead_name} at
the studio gets in touch, which is always open to them; thanks, greetings, and acknowledging what the visitor just
said.

Score how likely the visitor comes away misled, and quote what nothing backs. A good message with a loose phrase is
a low score: what matters is whether they would believe something untrue."""

checker = Agent(CHECK_MODEL, output_type=Backed, instructions=CHECK, model_settings=QUICK_CALL)
MISLEADING = 0.6  # at or above this, the message goes back to be said again


def _sources_for(v: Visitor, pages: dict[str, str] | None) -> list[Source]:
    words = [m.text for m in v.thread if m.sender is Sender.VISITOR] + [
        t.text for t in v.touches if t.text and t.kind is TouchKind.SIGNAL
    ]
    return [
        *STUDIO.sources(),
        Source(where="what the agent is", says=f"The agent on this site is {STUDIO.name}'s AI agent, not a person."),
        *(Source(where="the visitor's own words", says=w) for w in words),
        *(Source(where=f"research finding from {r.source}", says=r.fact, link=r.source) for r in v.research),
        *(Source(where=f"desk finding from {s.source}", says=s.fact, link=s.source) for s in stock.load().items),
        *([Source(where="their business", says=v.their_business)] if v.their_business else []),
        *(Source(where="already told them", says=m.text) for m in v.thread if m.sender is Sender.AGENT),
        *(
            Source(where=f"a page research read, {url}, as a person sees it", says=seen)
            for url, seen in (pages or {}).items()
        ),
    ]


def _told_before(v: Visitor, turn: Work, cites: list[str]) -> str:
    """An outside source already told to them: the date and the source, or empty."""
    told: list[Message | Email] = [*v.thread, *v.emails, *turn.said, *turn.emailed]
    for n in told:
        for c in cites:
            if _key(c) in {_key(x) for x in n.cites}:
                return f"{c}, on {n.at:%d %b}"
    return ""


async def _told_once(text: str, v: Visitor, turn: Work) -> list[str]:
    """A quick read of how likely `text` misleads them, and the outside sources it rests on. Only a message likely to
    mislead, or one telling them a source again, goes back to the executor, which says it again in its own words:
    nothing rewrites it for it, and a good message with a loose line goes out."""
    sources = _sources_for(v, turn.pages)
    shown = "\n".join(f"[{n}] {s.where}: {s.says}" for n, s in enumerate(sources))
    verdict = (await checker.run(f"The sources:\n{shown}\n\nThe text to check:\n{text}")).output
    if verdict.unbacked:
        log.info("check %s %.1f: %s", v.id[:8], verdict.misleading, verdict.unbacked)
    if verdict.misleading >= MISLEADING:
        raise ModelRetry(
            "Not backed by the studio brief, their words or research: "
            + "; ".join(f'"{u}"' for u in verdict.unbacked)
            + ". Say it again without these, in your own words: keep your view, your advice or your question; a fact "
            "stays only if the sources say it. Or say nothing."
        )
    cites = sorted({sources[i].link for i in verdict.rests_on if 0 <= i < len(sources)} - {""})
    if again := _told_before(v, turn, cites):
        raise ModelRetry(
            f"You already told them about {again}: never tell them the same thing twice. Say something new, or say "
            "nothing."
        )
    return cites


NO_OFFER = (
    "They already told you who they are on this visit: offer nothing for their name and email. Leave the offer out and "
    "drop it from what you say; if they want what you would have offered, send it (send_email)."
)


# --- the executor: it speaks, and looks things up when what it would say needs it ---------------------------------


class Activity(StrEnum):
    """What the agent is doing for whoever is on the site, as their widget shows it."""

    THINKING = "thinking"  # the planner is reading what they just did
    WRITING = "writing"
    RESEARCHING = "researching"


@dataclass
class Work:
    v: Visitor
    now: datetime
    seen: str  # why it was woken, the planner's brief and everything known: what research is given too
    doing: Callable[[str], None]
    publish: Callable[[Message], Message]  # puts a message in their thread now, numbered: they see it at once
    step: Callable[[Activity], None]  # what their widget says it is doing
    own_wake: bool = False  # the planner looked again on its own, not because of anything the person did
    answering: bool = False  # they wrote to the agent and are waiting: it must answer on the site
    introduced: bool = False  # they just left their name and email: thanks on the site first, then the email
    findings: list[Finding] = field(default_factory=list)
    news: list[Finding] = field(default_factory=list)  # the findings that are genuinely new
    read: list[str] = field(default_factory=list)  # pages research actually opened
    cited: list[str] = field(default_factory=list)  # sources Perplexity searches returned
    pages: dict[str, str] = field(default_factory=dict)  # what each of them showed: what it says about them rests on
    said: list[Message] = field(default_factory=list)  # on the site this turn, as published
    emailed: list[Email] = field(default_factory=list)
    shown: str = ""  # everything the executor has been given, research included: the email writer reads the same
    handed_over: list[str] = field(default_factory=list)
    gave_up: bool = False  # it kept wanting to say what it could not back, and said nothing


class Finding(BaseModel):
    fact: str = Field(description="One true, specific thing, in a sentence: what happened, what you saw, the number.")
    source: str = Field(description="The link it came from (https://...).")
    published: str = Field(default="", description="Its publication date, YYYY-MM-DD (or YYYY-MM), if known.")
    why_it_matters: str = Field(description="Why this matters to this visitor in particular.")

    @field_validator("source")
    @classmethod
    def _linked(cls, source: str) -> str:
        if not source.startswith(("http://", "https://")):
            raise ValueError("a finding carries the link it came from")
        return source


class Research(BaseModel):
    their_business: str = Field(
        default="", description="What their own website and the lookup say about their business, if you have them."
    )
    findings: list[Finding] = Field(description="What you found, most useful first. Empty if nothing real turned up.")


RESEARCH = f"""You research for {STUDIO.name}, a marketing studio, about one visitor to its website. You were woken
for a reason (below). Find what is new, real and useful to this person, from outside the studio's own site:
- If they told you who they are: look them up (enrich_them) and read their website the way a buyer would: the
  homepage, then follow its links (read_page) to the page where their problem lives (the booking page when bookings
  leak, the demo form when demos fall, the product page their ads point at), and the page that takes the money or
  the lead. What that page asks of a visitor, and how long it makes them wait, is usually the finding. If their website will not open and the lookup finds nothing, you know about
  their business only what they told you: never guess what they sell from their company's name. Then search for what is going on in their business and market: launches, news,
  their category's season, what competitors are doing, platform changes that hit their kind of marketing.
- If they are anonymous: search for what is new in the world their behaviour points to, such as a platform change
  behind the problem they picked or what brands like the ones they read about are doing this season.
- If you woke on your own: search for what has REALLY changed since the research and emails listed below.

Use sources a marketer would trust: the company itself, the platforms' own announcements, established trade and news
press. Never content farms, template sites or listicles. Every finding carries its link. Never invent one; an empty
list is a fine answer.

The studio, so you know what it does: {STUDIO.what_we_do}"""

SEARCH_SOURCES = OpenAIResponsesModelSettings(
    openai_include_web_search_sources=True, timeout=120, openai_reasoning_effort="low"
)

researcher = Agent(
    RESEARCH_MODEL,
    deps_type=Work,
    output_type=Research,
    instructions=RESEARCH,
    builtin_tools=[WebSearchTool()],
    retries=3,
    model_settings=SLOW_CALL,
)

EXECUTE = f"""{VOICE}

You are the executor: the part of the agent that speaks. The planner, the other part, read this moment and woke you;
its brief is at the top. It is a read of the situation, not a script: what to say or send, and whether to, is yours. You decide whether anything is worth saying, and say it: one remark on the site
(say_on_site), an email (send_email), or a hand-over to the studio's people (hand_over). A line does not have to be
substantial: a quick joke, a reaction to what they said, a bit of back-and-forth is often what a person would say, and
it builds the rapport the useful things need. Saying nothing is fine too when nothing, light or substantial, fits,
except when they wrote to you: then they are waiting, and you always answer them on the site.

How trust is built here: first what they do, then who they are. Until you know their line of work, the most useful
thing you can do is often to ask it, plainly and once, in a way that shows why it matters ("What do you sell? The
answer changes everything about where those clicks go."), never their name, company or email. Once you know it, be
useful to that business. Offer to email them something only after they have had something from you and you know
enough about what they do for it to be theirs.

Where it lands: everything you say on the site is a message in their chat with the agent. If their chat is closed it
pops up above the chat button for 20 seconds, then waits there unopened. Say one thing at a time; when they wrote to
you, answer what they said, like a person across a desk, in at most 60 words.

Looking things up: research reaches outside the studio (their website once they have given it, what is new in their
trade, the platforms they run on). It takes a minute or two: when they are waiting on the site, tell them in one short
line first. Use it when what they need is not in the studio brief or their own words; most moments it is not needed.

- They are anonymous: you have no email for them, so never say you sent, or will send, anything to an inbox. If they
  asked for something (a roast of their site), give the sharpest short version on the site, and offer the full
  write-up by email (an offer of kind writeup) if they tell you who they are.
- They just told you who they are: first a short thanks on the site (say_on_site), then do what was offered, by
  email, now. send_email writes and sends it; you say what it is for. Nobody else will send it, and a line on the site
  saying it is coming is no substitute.
- A moment the planner chose for a known person: if something new matters to them (the desk's news below, or what
  research finds), email them about it. That is the newsletter: it goes out only when the world gives a reason. If
  nothing new turned up, send nothing. Never "just checking in", never a recap.
- They answered your email: answer what they said with something new built on it, never a repeat of an earlier email
  and never a new question just to keep them writing. When their answers add up to the work a project would do (what
  they want measured, built or fixed, when), that is a brief: hand them over (hand_over) with their own words, and
  tell them {STUDIO.lead_name} will be in touch. Once handed over, scope, price and timing are {STUDIO.lead_name}'s
  to answer: say so in a line, and answer only what the brief itself states.
- Whenever they want to start, buy, get a price or a proposal, send files, or talk to someone: if they are anonymous,
  offer {STUDIO.lead_name} getting in touch (an offer of kind person), so the form is the next step; if you know them,
  hand them over and tell them {STUDIO.lead_name} will be in touch. Never arrange it yourself.

Offer only what you can write from their public website and research: a written teardown, the closest case written up
for them, what you would change and test. A wireframe, a design, a plan built on their own numbers or an audit that
needs access to their accounts is the studio's work: offer {STUDIO.lead_name} for that.

You never commit the studio to anything: no meetings, start dates, deadlines, turnarounds, proposals, mockups or prices
beyond what the brief itself offers. Their email address is theirs: never ask them to send anything to it. One email
answers one thing.

Claims come only from the studio brief, their own words, and research; nothing else, no numbers of your own. Use a
finding only when it is about their own kind of business, channel or season. The studio's clients and their stories
are only what the brief says, never merged one into another. Never name or recommend another agency, studio or
freelancer. Never mention what they clicked, read or browsed on the studio's site: that is surveillance, not insight.
Keep what matters for later (remember). End with one sentence on what you did, or why you said nothing."""

executor = Agent(
    EXECUTOR_MODEL,
    deps_type=Work,
    output_type=str,
    instructions=EXECUTE,
    retries=3,
    model_settings=OpenAIResponsesModelSettings(timeout=240, parallel_tool_calls=False, openai_reasoning_effort="low"),  # each call sees what the last one did
)


@researcher.tool
async def read_page(ctx: RunContext[Work], url: str) -> str:
    """Read a web page the way a person sees it, rendered in a browser: their website, an article, a product page.
    Returns its title, description, headings, the visible text and its navigation."""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    for read, seen in ctx.deps.pages.items():
        if _key(read) == _key(url):  # read already this turn: the page has not changed since
            return seen[:9000]
    try:
        landed, seen = await _render(url, 6000)
    except PlaywrightError as e:
        return f"could not open {url}: {e.message.splitlines()[0]}"
    ctx.deps.read += [url, landed]
    ctx.deps.pages[landed] = seen
    return seen[:9000]


async def _render(url: str, chars: int) -> tuple[str, str]:
    """Open `url` in a browser: where it landed, and what a person sees there, up to `chars` of its text. A page that
    will not open, or shows next to nothing (a bot wall), is fetched again through ScraperAPI."""
    try:
        landed, seen = await _browse(url, chars)
    except PlaywrightError:
        landed, seen = url, ""
    if len(seen) >= THIN_PAGE:
        return landed, seen
    try:
        return url, await fetch.page(url, chars)
    except (RuntimeError, httpx.HTTPError) as e:
        if seen:
            return landed, seen
        raise PlaywrightError(f"would not open in a browser or through ScraperAPI ({e})") from e


THIN_PAGE = 600  # characters: fewer is a wall or a challenge page, not the page


async def _browse(url: str, chars: int) -> tuple[str, str]:
    # Chromium ignores HTTPS_PROXY: under a Minutehand run it is pointed at the run's proxy, which records every page
    # read (and answers with its own certificate), so a page is part of the run like any other call.
    proxy = os.environ.get("HTTPS_PROXY") if os.environ.get("MINUTEHAND_ON") else None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(proxy={"server": proxy} if proxy else None)
        try:
            page = await browser.new_page(viewport={"width": 1366, "height": 900}, ignore_https_errors=bool(proxy))
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            return page.url, str(await page.evaluate(READ_PAGE, chars))
        finally:
            await browser.close()


READ_PAGE = """(chars) => {
  const meta = n => document.querySelector(`meta[name="${n}"],meta[property="${n}"]`)?.content || "";
  const heads = [...document.querySelectorAll("h1,h2,h3")].map(h => h.innerText.trim()).filter(Boolean).slice(0, 25);
  const links = [...document.querySelectorAll("a[href]")].map(a => `${a.innerText.trim()} (${a.href})`).filter(l => !l.startsWith(" (")).slice(0, 40);
  const text = document.body.innerText.replace(/\\s+/g, " ").trim().slice(0, chars);
  return `URL: ${location.href}\\nTitle: ${document.title}\\nDescription: ${meta("description") || meta("og:description")}\\n` +
    `Links: ${links.join(" | ")}\\nHeadings: ${heads.join(" | ")}\\nText: ${text}`;
}"""


@researcher.tool
async def enrich_them(ctx: RunContext[Work]) -> str:
    """Look the person up by their email (and their company by its website): role, company, industry, size."""
    v = ctx.deps.v
    if not v.email:
        raise ModelRetry("You do not know their email yet; there is no one to look up.")
    try:
        found = await apollo.enrich(v.email, v.name, v.website)
    except (RuntimeError, httpx.HTTPError) as e:
        return f"Lookup failed ({e}); work from their website and the web instead."
    if found is None:
        return "Apollo knows nothing about them."
    v.company = found
    return found.model_dump_json(exclude_defaults=True)


def _key(url: str) -> str:
    """A link as a place: no scheme, no www, no query or fragment, no trailing slash."""
    url = url.split("#")[0].split("?")[0].rstrip("/").lower()
    return url.removeprefix("https://").removeprefix("http://").removeprefix("www.")


def _sources(messages: list[ModelMessage]) -> set[str]:
    """Every link the web searches in this run actually returned."""
    found: set[str] = set()
    for m in messages:
        for part in m.parts:
            if isinstance(part, BuiltinToolReturnPart) and isinstance(part.content, dict):
                content = cast(dict[str, object], part.content)
                for src in cast(list[dict[str, object]], content.get("sources") or []):
                    if isinstance(src.get("url"), str):
                        found.add(_key(str(src["url"])))
    return found


@researcher.tool
async def search_news(ctx: RunContext[Work], query: str, recency: str = "month") -> str:
    """Search the web through Perplexity: an answer with its sources and their dates. Best for what is recent
    (`recency`: day, week, month or year). Cite only the sources it lists."""
    try:
        found = await fetch.search(query, recency)
    except (RuntimeError, httpx.HTTPError) as e:
        return f"Search failed ({e}); use your other web search."
    ctx.deps.cited += [c.url for c in found.cited]
    return found.model_dump_json()


@researcher.output_validator
def _only_real_sources(ctx: RunContext[Work], research: Research) -> Research:
    real = _sources(ctx.messages) | {_key(u) for u in ctx.deps.read + ctx.deps.cited}
    invented = [f.source for f in research.findings if _key(f.source) not in real]
    if invented and ctx.retry >= 2:  # still citing what it never got: keep what it can source, and the turn goes on
        log.warning("findings dropped, never fetched: %s", ", ".join(sorted(set(invented))))
        return research.model_copy(update={"findings": [f for f in research.findings if _key(f.source) in real]})
    if invented:
        raise ModelRetry(
            "These links were not returned by any search or page you read, so they cannot be cited: "
            + ", ".join(invented)
            + ". Cite only links you actually got. Drop any finding you cannot source that way."
            + (" Links you did get: " + ", ".join(sorted(real)[:40]) if real else " You have not searched yet: search.")
        )
    return research


def _desk_news(v: Visitor) -> list[Finding]:
    """What the desk learned since the agent last wrote to them (or last researched them): news to them by definition."""
    wrote = [e.at for e in v.emails] or [r.at for r in v.research]
    since = max(wrote) if wrote else None
    return [
        Finding(
            fact=s.fact,
            source=s.source,
            published=s.published,
            why_it_matters=f"the desk's {s.topic.name.lower()} news",
        )
        for s in stock.load().items
        if since is None or s.found > since
    ]


def _published(f: Finding) -> date | None:
    """When it was published; a month alone counts as the month's first day, so it is never taken as later than it
    might be."""
    text = f.published.strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return date(int(text[:4]), int(text[5:7]), 1)
    except ValueError:
        return None


def news(v: Visitor, findings: list[Finding], own_wake: bool) -> list[Finding]:
    """What is genuinely new: a source never cited before; and, on a wake it chose, published after it last wrote to
    them (or last researched them), so it is a change in their world rather than something it had not read yet."""
    cited = {_key(r.source) for r in v.research}
    fresh = [f for f in findings if _key(f.source) not in cited]
    if not own_wake:
        return fresh
    wrote = [e.at for e in v.emails] or [r.at for r in v.research]
    if not wrote:
        return fresh
    since = max(wrote).date()
    return [f for f in fresh if (when := _published(f)) is not None and when > since]


def _from_research(ctx: RunContext[Work]) -> None:
    if ctx.deps.own_wake and not ctx.deps.news:
        raise ModelRetry(
            "Nothing new has happened in their world since you last wrote (the desk's news, or what research found): "
            "there is no reason to write. Research first if you have not; otherwise say and send nothing."
        )


CHAT_WORDS = 60  # an answer to what they asked may go a little longer than a remark


@executor.tool
async def say_on_site(
    ctx: RunContext[Work], text: str, offer_kind: OfferKind | None = None, offer_what: str = ""
) -> str:
    """Say one thing to them on the site, as a message in their chat with the agent. With an offer, a form for their
    name and email sits under it: `offer_kind` writeup when you will write and email them something yourself
    (`offer_what` as it finishes "Want ...?": "three fixes for your homepage"), or person when it is
    {lead} getting in touch (`offer_what`: what about). Make the offer in the words as that question; never ask for
    their email or name in the words."""
    _from_research(ctx)
    v = ctx.deps.v
    most = CHAT_WORDS if ctx.deps.answering else SITE_WORDS
    if len(text.split()) > most:
        raise ModelRetry(f"On the site this is at most {most} words; this is {len(text.split())}. Cut it.")
    if offer_kind is not None and v.identified_here():
        raise ModelRetry(NO_OFFER)
    if offer_kind is not None and not offer_what:
        raise ModelRetry("An offer names what it is (offer_what), or leave offer_kind out.")
    cites = await _told_once(text, v, ctx.deps)
    ctx.deps.said.append(
        ctx.deps.publish(
            Message(
                id=-1,  # numbered as it joins the thread
                at=ctx.deps.now,
                sender=Sender.AGENT,
                text=text,
                offer=Offer(kind=offer_kind, what=offer_what) if offer_kind is not None else None,
                cites=cites,
            )
        )
    )
    return "said" if on_site(ctx.deps.v, ctx.deps.now) else "they are not on the site; it waits in their chat"


say_on_site.__doc__ = (say_on_site.__doc__ or "").replace("{lead}", STUDIO.lead_name)


@executor.output_validator
def _answered(ctx: RunContext[Work], out: str) -> str:
    if ctx.deps.answering and not ctx.deps.said:
        raise ModelRetry("They wrote to you and are waiting on the site: answer them there (say_on_site).")
    return out


@executor.tool(retries=5)
async def send_email(ctx: RunContext[Work], what_for: str) -> str:
    """Email them. A writer reads everything you were given and writes it several ways, and the one they would most
    likely answer goes. `what_for`: what this email is for, in a sentence or two: what they asked for in their own
    words, the reply it answers, or what changed in their world and why it matters to them. Say what, never how."""
    v = ctx.deps.v
    if v.status is not Status.KNOWN or not v.email:
        raise ModelRetry("They have not given you their email, or asked you to stop: you cannot email them.")
    _from_research(ctx)
    if ctx.deps.introduced and on_site(v, ctx.deps.now) and not ctx.deps.said:
        raise ModelRetry("They are on the site: thank them there in a line first (say_on_site), then send the email.")
    asked = max((t.at for t in v.touches if t.kind in (TouchKind.IDENTIFY, TouchKind.EMAIL_REPLY)), default=None)
    if v.handed_over and not any(t.kind is TouchKind.EMAIL_REPLY and t.at > v.handed_over for t in v.touches):
        raise ModelRetry(
            f"They were handed over to {STUDIO.lead_name}, who writes to them now; you email only to answer their "
            "replies. Do not email."
        )
    sent = [e.at for e in v.emails + ctx.deps.emailed]
    if sent and not (ctx.deps.own_wake and ctx.deps.news) and (asked is None or max(sent) >= asked):
        raise ModelRetry(
            f"You already emailed them at {max(sent).isoformat()}, and since then they have neither answered nor "
            "introduced themselves again, and nothing new has happened in their world. Do not email: answer on the "
            "site if they are there, or hand them over if they want to buy."
        )
    told = {_key(u) for e in v.emails + ctx.deps.emailed for u in LINK.findall(e.text)}
    refused: list[str] = []
    for draft in await emails.write(ctx.deps.shown, what_for):
        if again := [u for u in LINK.findall(draft.text) if _key(u) in told]:
            refused.append(f"sends {', '.join(again)} again")
            continue
        try:
            text, cites = draft.text, await _told_once(draft.text, v, ctx.deps)
        except ModelRetry as e:  # deliberately caught: the next draft is tried before the agent hears anything
            refused.append(str(e)[:300])
            continue
        await mail.send(v.id, v.email, draft.subject, text)
        ctx.deps.emailed.append(Email(at=ctx.deps.now, subject=draft.subject, text=text, cites=cites))
        return f"sent: {draft.subject}\n{text}"
    raise ModelRetry(
        "No draft of that email could be sent: " + " | ".join(refused or ["the writer wrote nothing"])
        + ". Say what it is for differently, built on what you can back, or send nothing."
    )


@executor.tool
def remember(ctx: RunContext[Work], beliefs: str = "", preparing: str = "") -> str:
    """Keep for later wakes: what you now believe about them, and what you are preparing for them. Empty arguments
    leave things as they are."""
    if beliefs:
        ctx.deps.v.beliefs = beliefs
    if preparing:
        ctx.deps.v.preparing = preparing
    return "remembered"


@executor.tool
async def hand_over(ctx: RunContext[Work], summary: str) -> str:
    """Hand them to the studio's people: who they are, what they want, everything you learned, in a few lines."""
    if ctx.deps.v.handed_over is not None:
        raise ModelRetry(
            f"They were handed to {STUDIO.lead_name} at {ctx.deps.v.handed_over.isoformat()} already. Do not hand "
            f"them over again: tell them {STUDIO.lead_name} has it and will be in touch."
        )
    await _hand_over(ctx.deps, summary)
    return f"handed to {STUDIO.lead_name}"


async def _hand_over(turn: Work, summary: str) -> None:
    turn.handed_over.append(summary)
    turn.v.handed_over = turn.now
    crm = os.environ.get("CRM_URL")
    if crm:
        v = turn.v
        async with httpx.AsyncClient(timeout=30) as http:
            sent = await http.post(
                crm, json={"name": v.name, "email": v.email, "website": v.website, "summary": summary}
            )
            sent.raise_for_status()


OWED_RETRY = timedelta(minutes=10)


def _owed_email(v: Visitor, turn: Work) -> str:
    """What they left their email for, when they did and no email has answered it: a promise the agent made."""
    asked = max((t.at for t in v.touches if t.kind is TouchKind.IDENTIFY), default=None)
    if asked is None or v.status is not Status.KNOWN:
        return ""
    if any(e.at >= asked for e in v.emails + turn.emailed):
        return ""
    offered = [m.offer for m in v.thread if m.offer is not None and m.at <= asked]
    if offered and offered[-1].kind is OfferKind.PERSON:
        return ""  # they asked for a person: the hand-over is what they get, not an email from the agent
    return offered[-1].what if offered else "what they asked for"


def _asked_for_person(v: Visitor) -> bool:
    """They left their email so that someone at the studio would get in touch."""
    asked = max((t.at for t in v.touches if t.kind is TouchKind.IDENTIFY), default=None)
    offered = [m.offer for m in v.thread if m.offer is not None and asked is not None and m.at <= asked]
    return v.status is Status.KNOWN and bool(offered) and offered[-1].kind is OfferKind.PERSON


def _their_pages(v: Visitor, turn: Work) -> str:
    """The pages of their own site research read, as a person sees them: what an email about their business is
    written from, not a summary of it."""
    home = _key(v.website or v.email.split("@")[-1]).split("/")[0] if (v.website or v.email) else ""
    theirs, kept = [], set()
    for url, seen in turn.pages.items():
        body = seen.split("Text:", 1)[-1]
        if home and _key(url).split("/")[0] == home and body not in kept:  # one page reached by two links is one
            theirs.append((url, seen))
            kept.add(body)
    if not theirs:
        return ""
    return "\n\nTheir own site, as research read it:\n" + "\n\n".join(
        f"{url}\n{seen[:THEIR_PAGE_CHARS]}" for url, seen in theirs[:THEIR_PAGES]
    )


THEIR_PAGES, THEIR_PAGE_CHARS = 5, 3500


def _unread(v: Visitor, turn: Work) -> str:
    """Their website, when research did not manage to open it."""
    if not v.website or any(_key(u).split("/")[0] == _key(v.website).split("/")[0] for u in turn.read):
        return ""
    return v.website


def _research_shown(r: Research, fresh: list[Finding], own_wake: bool, unread: str) -> str:
    lines = ["Your research for this wake:"]
    if unread:
        lines.append(
            f"Their website, {unread}, did not open when you tried to read it. If they are waiting on you for "
            "something about it, tell them plainly you could not open it, and ask them to check the address."
        )
    if r.their_business:
        lines.append(f"Their business: {r.their_business}")
    lines += [
        f"- {'NEW ' if f in fresh else 'already known '}{f.fact} ({f.source}"
        f"{', ' + f.published if f.published else ''}). Why it matters to them: {f.why_it_matters}"
        for f in r.findings
    ] or ["- nothing real turned up"]
    if own_wake and not fresh:
        lines.append("Nothing has changed in their world since you last wrote: there is nothing to send.")
    return "\n".join(lines)


class Publisher(StrEnum):
    THE_COMPANY = "the company or brand the finding is about, on its own site; for an event, its organiser"
    PLATFORM = "a platform's own announcement, documentation or research (Shopify, Google, Klaviyo, Meta)"
    PRESS = "established news or trade press: a publication with a name a marketer would recognise"
    VENDOR = "a tool vendor or agency marketing its own product or services, case studies included"
    CONTENT_SITE = "an SEO content site, listicle, aggregator or blog that publishes to rank"


TRUSTED = {Publisher.THE_COMPANY, Publisher.PLATFORM, Publisher.PRESS}


class Verdict(BaseModel):
    page_says: str = Field(description="What the page actually says on this point, in a sentence or two.")
    stated: bool = Field(
        description="True only if the page states the finding, about the same company, product or thing it names."
    )
    publisher: str = Field(description="Who published the page, by name.")
    kind: Publisher = Field(
        description="What kind of publisher that is, judged by who they are, not by the page's tone."
    )

    @property
    def trusted(self) -> bool:
        return self.kind in TRUSTED


VERIFY = """You check one research finding against the page it cites, which you are given as a person sees it. Say
what the page actually says on the point, then whether it states the finding. A finding that names a company, brand or
person the page does not mention, that pins the page's general point on someone specific, or that carries a number the
page does not give, is not stated. A fair summary of what the page says is stated. Then say who published it
and what kind of publisher they are. A vendor's case study of its own client is a vendor, however specific its numbers.
A site you do not recognise as an established publication is a content site, however much it looks like news."""

verifier = Agent(CHECK_MODEL, output_type=Verdict, instructions=VERIFY, model_settings=QUICK_CALL)


async def _stated(f: Finding) -> bool:
    """The page it cites says it. A page that will not open backs nothing."""
    try:
        _, text = await _render(f.source, 30000)
    except PlaywrightError as e:
        log.warning("finding dropped, source would not open (%s): %s", f.source, e.message.splitlines()[0])
        return False
    verdict = (await verifier.run(f"The page:\n{text}\n\nThe finding:\n{f.fact}")).output
    if verdict.stated and verdict.trusted:
        log.info("finding kept, %s is %s: %s", verdict.publisher, verdict.kind.name, f.fact)
    if not verdict.stated:
        log.warning("finding dropped, not what its source says: %s | the page says: %s", f.fact, verdict.page_says)
    elif not verdict.trusted:
        log.warning("finding dropped, %s is %s: %s", verdict.publisher, verdict.kind.name, f.fact)
    return verdict.stated and verdict.trusted


@executor.tool
async def research(ctx: RunContext[Work], what: str) -> str:
    """Look outside the studio for `what`: their own website and who they are (once they have told you), what is new
    in their trade, the platforms their marketing runs on. Takes a minute or two. Returns only findings whose pages were
    checked to say them."""
    turn, v = ctx.deps, ctx.deps.v
    turn.doing(f"Researching: {what}")
    turn.step(Activity.RESEARCHING)
    try:
        found = (
            await researcher.run(f"{turn.seen}\n\nWhat to find: {what}", deps=turn, model_settings=SEARCH_SOURCES)
        ).output
    except UnexpectedModelBehavior as e:  # it never gave findings it could source: it goes on with none
        log.warning("research %s gave nothing usable: %s", v.id[:8], e)
        found = Research(findings=[])
    turn.doing(f"Checking {len(found.findings)} finding(s) against the pages they cite")
    stated = await asyncio.gather(*(_stated(f) for f in found.findings))
    found.findings = [f for f, ok in zip(found.findings, stated, strict=True) if ok]
    cited = {_key(r.source) for r in v.research}
    fresh = news(v, found.findings, turn.own_wake)
    turn.findings += found.findings
    turn.news += [f for f in fresh if f not in turn.news]
    v.research += [
        Researched(at=turn.now, fact=f.fact, source=f.source, published=f.published)
        for f in found.findings
        if _key(f.source) not in cited
    ]
    if found.their_business:
        v.their_business = found.their_business
    turn.doing("Deciding what to say from " + (found.findings[0].fact if found.findings else "nothing new"))
    turn.step(Activity.WRITING)
    told = f"{_research_shown(found, fresh, turn.own_wake, _unread(v, turn))}{_their_pages(v, turn)}"
    turn.shown += f"\n\n{told}"
    return told


async def execute(
    v: Visitor,
    now: datetime,
    woken_because: str,
    brief: str,
    own_wake: bool,
    answering: bool,
    introduced: bool,
    doing: Callable[[str], None],
    publish: Callable[[Message], Message],
    step: Callable[[Activity], None],
) -> Work:
    """One turn of the executor. `doing` hears each step as it starts, so the studio's people can see what it is
    working on."""
    seen = f"Why you were woken: {woken_because}\nThe planner's brief: {brief}\n\n{shown(v, now)}"
    turn = Work(
        v=v,
        now=now,
        seen=seen,
        doing=doing,
        publish=publish,
        step=step,
        own_wake=own_wake,
        answering=answering,
        introduced=introduced,
        shown=seen,
    )
    if own_wake:  # what the desk learned since it last wrote: news to them by definition, already checked
        cited = {_key(r.source) for r in v.research}
        learned = [f for f in _desk_news(v) if _key(f.source) not in cited]
        turn.findings += learned
        turn.news += learned
        turn.shown += "\n\nThe desk's news since you last wrote to them:\n" + (
            "\n".join(f"- NEW {f.fact} ({f.source}, {f.published})" for f in learned) or "- nothing"
        )
    doing(f"Deciding what to say: {brief}")
    try:
        await executor.run(turn.shown, deps=turn)
    except UnexpectedModelBehavior as e:  # it kept wanting to say what it could not back: it says nothing this time
        log.warning("executor %s said nothing: %s", v.id[:8], e)
        turn.gave_up = True
    if _asked_for_person(v) and v.handed_over is None:  # what they asked for is a person: hand over, every time
        await _hand_over(
            turn,
            f"{v.name} asked for {STUDIO.lead_name} to get in touch. What the agent believes about them: "
            f"{v.beliefs or 'little yet'}. Their business: {v.their_business or 'unknown'}.",
        )
    owed = _owed_email(v, turn)
    if owed and v.handed_over is None and v.owed_missed is None:  # one more turn to write it, soon
        log.warning("executor %s could not write the email owed (%s): trying again", v.id[:8], owed)
        v.owed_missed = now
        v.wake_at = now + OWED_RETRY
        v.wake_why = f"write the email they left their address for ({owed}), from only what you can back"
    elif owed and v.handed_over is None and v.owed_missed is not None and now >= v.owed_missed + OWED_RETRY:
        # the retry came round and it still could not: a person sends it rather than nobody
        log.warning("executor %s could not write the email owed (%s): handed over", v.id[:8], owed)
        await _hand_over(
            turn,
            f"{v.name} left their email for {owed}, and the agent could not write it from what it "
            "can back. Please send it yourself.",
        )
    return turn
