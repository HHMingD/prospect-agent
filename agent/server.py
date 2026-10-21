"""The agent's process: it serves the studio's site, hears every touch and every message, and takes a turn for a
visitor when they write to it, leave their email, do something worth a look, or a moment it chose comes round.

    GET  /, /work, ...         the studio's site, with the widget
    POST /api/events           the widget: one touch by one visitor
    GET  /api/thread?v=        the widget: the whole conversation, what they have seen, and whether the agent is writing
    POST /api/thread/send      the widget: they wrote to the agent (the chat, or the page's own field)
    POST /api/thread/read      the widget: they have seen the conversation up to a message
    POST /api/thread/dismiss   the widget: they deleted one of the agent's messages from its preview
    GET  /api/unsubscribe?v=   the link in every email
    GET  /console?key=         for the studio's people: what the agent believes, is preparing and is doing, per visitor
    POST /inbound/email?key=   the mail provider: someone answered an email

  Only in a Minutehand run (MINUTEHAND_ON), which owns the clock and the world:
    GET  /api/shown?v=         a run's visitor: what the widget puts in front of them, and what they can do
    POST /api/world            the run's newsroom
    POST /wake, GET /report    it is now `now`; when do you next want waking?

  The site is public, so whatever spends a model call is rationed: a few turns a minute per visitor, and a ceiling
  across everyone, so a script inventing visitors cannot spend more than one busy afternoon would. What shows every
  visitor or speaks for the mail provider asks for OPERATOR_KEY (`?key=`), or, with none set, a request from this
  machine.

    OPENAI_API_KEY=... APOLLO_API_KEY=... RESEND_API_KEY=... uv run python -m agent.server
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from minutehand.agent import store
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from agent import desk
from agent.desk import WorldNews
from agent.looks import Activity, Plan, Work, execute, plan
from agent.stock import Stocked
from agent.studio import SITE, STUDIO
from agent.visitors import (
    Look,
    Message,
    Offer,
    OfferKind,
    Sender,
    SiteEvent,
    Status,
    Touch,
    TouchKind,
    Visitor,
)

log = logging.getLogger("agent")
CONSOLE = Path("console/index.html")  # the studio's view of the agent: not part of the site
SIMULATED = bool(os.environ.get("MINUTEHAND_ON"))  # a Minutehand run owns the clock and wakes the agent itself
PLANNING = {
    TouchKind.SIGNAL,
    TouchKind.LINGER,
    TouchKind.READ,
    TouchKind.LEAVING,
    TouchKind.EMAIL_REPLY,
}  # what the planner looks at; a page view only when it starts a visit. Clicks, presence and the chat opening or
# closing only add to what it knows
CANNOT_BACK = (
    "I'd be guessing on that one, and I'd rather not. Ask it another way, or leave your email and someone at the "
    "studio will answer it properly."
)
CANNOT_BACK_KNOWN = (
    "I'd be guessing on that one, and I'd rather not. Ask it another way, or someone at the studio will answer it "
    "properly by email."
)


def load(visitor: str) -> Visitor | None:
    kept = store.get(f"visitors/{visitor}")
    return Visitor.model_validate(kept) if kept is not None else None


def save(v: Visitor) -> None:
    store.put(f"visitors/{v.id}", v.model_dump(mode="json"))
    plans_changed.set()


def everyone() -> list[Visitor]:
    return [Visitor.model_validate(kept) for _, kept in store.list("visitors/")]


def clock() -> datetime:
    """Now: under a Minutehand run, the moment of its latest wake, since the run owns the time."""
    if SIMULATED and (last := store.get("last_wake")):
        return datetime.fromisoformat(str(last))
    return datetime.now(UTC)


def stamped(at: datetime | None) -> datetime:
    """When something they did happened: a run's own time under Minutehand, else the server's."""
    return at if SIMULATED and at is not None else clock()


# --- one turn for one visitor -----------------------------------------------------------------------------------


def stranger_here(v: Visitor, personal: bool) -> bool:
    """On the site, the agent knows a person only if they said who they are on this visit. Its own looks and email
    replies are about the person who signed up; everything else on someone else's visit is a stranger's."""
    return v.status is Status.KNOWN and not v.identified_here() and not personal


async def turn(visitor: str, now: datetime) -> None:
    """Everything owed to one visitor now. What they wrote, or the email they just left, is answered first and at
    once; the planner then reads the moment. The two parts never run beside each other for the same person."""
    v = load(visitor)
    if v is None or v.status is Status.STOPPED:
        return
    unanswered = v.unanswered()
    introduced = any(t.kind is TouchKind.IDENTIFY for t in v.new_touches())
    if unanswered or introduced:
        await answer(visitor, now, unanswered, introduced)
        await look(visitor, now, answered=True)
    else:
        await look(visitor, now, answered=False)


async def answer(visitor: str, now: datetime, unanswered: list[Message], introduced: bool) -> None:
    """They wrote to the agent, or left their email: the executor answers on the site, now, without the planner."""
    v = load(visitor)
    if v is None:
        return
    why = (
        [f'they wrote to you: "{m.text}"' for m in unanswered]
        + (["they just left their name and email in the form under your offer"] if introduced else [])
    )
    stranger = stranger_here(v, personal=introduced)
    activity[visitor] = Activity.WRITING
    try:
        w = await execute(
            v.as_stranger() if stranger else v,
            now,
            "; ".join(why),
            "They are on the site waiting for you: answer them.",
            own_wake=False,
            answering=bool(unanswered),
            introduced=introduced,
            doing=lambda what: doing(visitor, what),
            publish=lambda m: publish(visitor, m),
            step=lambda a: activity.__setitem__(visitor, a),
        )
        if w.gave_up and not w.said and unanswered:  # they asked and are waiting: an honest "I don't know" beats silence
            known = v.identified_here()
            w.said.append(
                publish(
                    visitor,
                    Message(
                        id=-1,
                        at=now,
                        sender=Sender.AGENT,
                        text=CANNOT_BACK_KNOWN if known else CANNOT_BACK,
                        offer=None if known else Offer(kind=OfferKind.PERSON, what="answering it"),
                    ),
                )
            )
    finally:
        activity.pop(visitor, None)
        doing(visitor, "")
    keep(visitor, v, w, stranger, now, answered_through=max((m.id for m in unanswered), default=None))


async def look(visitor: str, now: datetime, answered: bool) -> None:
    """The planner reads the moment: when they did something worth a look, or when the look it chose came round. Right
    after an answer it reads the moment only to choose when to look again: the executor has just acted."""
    v = load(visitor)
    if v is None or v.status is Status.STOPPED:
        return
    due = v.wake_at is not None and now >= v.wake_at
    new = v.new_touches()
    started = v.visit_start()
    worth = [t for t in new if t.kind in PLANNING or (t.kind is TouchKind.VIEW and t.at == started)]
    if not due and not worth and not answered:
        return
    why: list[str] = []
    if answered:
        why.append("the executor has just answered what they wrote or the email they left (in the thread below)")
    if due:
        why.append(f"the look you chose: {v.wake_why}")
    if worth:
        why.append("they did something new (marked NEW below)")
    personal = due or any(t.kind in (TouchKind.IDENTIFY, TouchKind.EMAIL_REPLY) for t in new)
    stranger = stranger_here(v, personal)
    seen = v.as_stranger() if stranger else v
    if not answered:  # right after an answer the look only times the next one: nothing for them to wait on
        activity[visitor] = Activity.THINKING
    try:
        p = await plan(seen, now, "; ".join(why))
    finally:
        if activity.get(visitor) is Activity.THINKING:
            activity.pop(visitor, None)
    bar = speaking_bar(v)
    speaks = p.speak >= bar and not answered
    fresh = load(visitor) or v
    fresh.looked_until = max([now, *(t.at for t in new)])
    fresh.last_look = Look(
        at=now,
        decision=f"{p.moment.name.lower()}, {p.speak:.1f} against {bar:.1f}: "
        + ("woke the executor" if speaks else "stayed back"),
        learned=p.reading,
        brief=p.brief,
    )
    if not stranger:  # a stranger's visit leaves the person's record as it was
        fresh.beliefs = p.reading
        fresh.line_of_work = p.line_of_work or fresh.line_of_work
        fresh.wake_at = now + p.next_look if p.next_look is not None else None
        fresh.wake_why = p.next_look_why if p.next_look is not None else ""
    elif due:
        fresh.wake_at, fresh.wake_why = None, ""
    save(fresh)
    log.info(
        "plan %s %s %.1f/%.1f%s next %s (%s): %s | brief: %s",
        v.id[:8],
        p.moment.name.lower(),
        p.speak,
        bar,
        " +act" if speaks else "",
        p.next_look or "-",
        "answered" if answered else "own look" if due and not worth else "they acted",
        p.reading,
        p.brief or "-",
    )
    if speaks:
        await act(visitor, now, "; ".join(why), p, stranger, own_wake=due and not worth)


SPEAK = 0.5  # how much a moment must call for the agent before it speaks, with nothing of its own waiting
UNOPENED_COST = 0.15  # each of its messages still unopened raises that: another would pile on


def speaking_bar(v: Visitor) -> float:
    """How much a moment must call for the agent before it speaks: higher the more of its own it has left unopened."""
    return min(0.95, SPEAK + UNOPENED_COST * len(v.unread()))


async def act(visitor: str, now: datetime, why: str, p: Plan, stranger: bool, own_wake: bool) -> None:
    """The executor's turn, as the planner briefed it: something worth saying or sending, or nothing."""
    v = load(visitor)
    if v is None or v.status is Status.STOPPED:
        return
    activity[visitor] = Activity.WRITING
    try:
        w = await execute(
            v.as_stranger() if stranger else v,  # it works for whoever is here; nothing lands on the person's record
            now,
            why,
            p.brief,
            own_wake=own_wake,
            answering=False,
            introduced=False,
            doing=lambda what: doing(visitor, what),
            publish=lambda m: publish(visitor, m),
            step=lambda a: activity.__setitem__(visitor, a),
        )
    finally:
        activity.pop(visitor, None)
        doing(visitor, "")
    keep(visitor, v, w, stranger, now, answered_through=None)


def publish(visitor: str, m: Message) -> Message:
    """One of the agent's messages joins their thread the moment it is ready, stamped then: a thanks is on screen
    while the email it promises is still being written."""
    fresh = load(visitor) or Visitor(id=visitor)
    said = fresh.append(Sender.AGENT, clock(), m.text, offer=m.offer, cites=m.cites)
    save(fresh)
    log.info("main %s site: %s", visitor[:8], said.text)
    return said


def keep(visitor: str, v: Visitor, w: Work, stranger: bool, now: datetime, answered_through: int | None) -> None:
    """What a turn of the executor leaves, beyond the messages it already published: its emails on the record, and what
    it learned, which stays with the person unless it was a stranger's visit."""
    fresh = load(visitor) or Visitor(id=v.id)
    for e in w.emailed:
        log.info("main %s email: [%s] %s", v.id[:8], e.subject, e.text)
    for h in w.handed_over:
        log.info("main %s handed over: %s", v.id[:8], h)
    fresh.emails = [*fresh.emails, *w.emailed]
    if answered_through is not None:
        fresh.answered_through = max(fresh.answered_through, answered_through)
    if not stranger:
        fresh.preparing, fresh.research, fresh.their_business = v.preparing, v.research, v.their_business
        fresh.company = v.company
        fresh.handed_over = v.handed_over or fresh.handed_over
        fresh.owed_missed = v.owed_missed or fresh.owed_missed
        fresh.beliefs = v.beliefs or fresh.beliefs
        if v.owed_missed is not None and v.owed_missed == now:  # the email they are owed gets one more try, soon
            fresh.wake_at, fresh.wake_why = v.wake_at, v.wake_why
    fresh.main_wakes += 1
    save(fresh)


def doing(visitor: str, what: str) -> None:
    current = load(visitor)
    if current is not None:
        current.main_now = what
        save(current)


running: dict[str, asyncio.Task[None]] = {}  # one turn per visitor at a time
again: set[str] = set()  # something arrived during the turn: take another as soon as it ends
activity: dict[str, Activity] = {}  # what the agent is doing for them now: the widget shows it
plans_changed = asyncio.Event()


async def _turns(visitor: str) -> None:
    try:
        while True:
            again.discard(visitor)
            if not affordable(visitor):
                log.warning("turn for %s skipped: over the ration", visitor[:8])
                return
            spend(visitor)
            try:
                await turn(visitor, clock())
            except Exception:
                log.exception("turn for %s failed", visitor)
            if visitor not in again:
                return
    finally:
        running.pop(visitor, None)


TURNS_EACH = 6  # turns a minute for one visitor: above anyone typing and clicking, the most a script on one id gets
TURNS_ALL = 30  # turns a minute across everyone: the most a script inventing visitors gets
LONGEST = 2000  # characters in one message they write
spent: dict[str, deque[float]] = {}
spent_all: deque[float] = deque()


def affordable(visitor: str) -> bool:
    """Whether a turn for this visitor fits the last minute's ration, for them and for everyone."""
    now = time.monotonic()
    mine = spent.get(visitor, deque())
    for q in (mine, spent_all):
        while q and now - q[0] > 60:
            q.popleft()
    if not mine:
        spent.pop(visitor, None)
    return len(mine) < TURNS_EACH and len(spent_all) < TURNS_ALL


def spend(visitor: str) -> None:
    now = time.monotonic()
    spent.setdefault(visitor, deque()).append(now)
    spent_all.append(now)


def operator(request: Request) -> bool:
    """The studio, not a visitor: the key in the URL, or this machine when no key is set."""
    key = os.environ.get("OPERATOR_KEY", "")
    if key:
        return request.query_params.get("key", "") == key
    return request.client is not None and request.client.host in ("127.0.0.1", "::1")


def kick(visitor: str) -> None:
    """Take a turn for this visitor now, or as soon as the one under way ends."""
    if visitor in running:
        again.add(visitor)
    else:
        running[visitor] = asyncio.create_task(_turns(visitor))


async def own_moments() -> None:
    """Wake for the moments the agent chose, and only those."""
    while True:
        now = clock()
        moments = [v for v in everyone() if v.wake_at is not None and v.status is not Status.STOPPED]
        for v in moments:
            if v.wake_at is not None and v.wake_at <= now:
                kick(v.id)
        later = [v.wake_at for v in moments if v.wake_at is not None and v.wake_at > now]
        plans_changed.clear()
        wait = (min(later) - now).total_seconds() if later else None
        try:
            await asyncio.wait_for(plans_changed.wait(), timeout=wait)
        except TimeoutError:
            pass


# --- what the site and the mail provider tell it ----------------------------------------------------------------

TURNS = PLANNING | {TouchKind.VIEW, TouchKind.IDENTIFY}  # touches that may need a turn; `look` decides


def heard(event: SiteEvent) -> Visitor:
    touch = event.touch.model_copy(update={"at": stamped(event.touch.at)})
    v = load(event.visitor) or Visitor(id=event.visitor)
    v.touches.append(touch)
    email = event.email.strip().lower()
    if touch.kind is TouchKind.IDENTIFY and v.email and email != v.email:
        # Someone else on this browser: the person who signed up keeps their own record (and their emails), and
        # the browser starts over with this visit only.
        save(v.model_copy(update={"id": f"{v.id}~{v.email}", "touches": v.touches[:-1]}))
        v = v.as_stranger()
        v.touches = [*v.touches, touch] if touch not in v.touches else v.touches
    if touch.kind is TouchKind.IDENTIFY and v.status is not Status.STOPPED:
        v.name, v.email, v.status = event.name or v.name, email, Status.KNOWN
    if event.website and (v.status is Status.ANONYMOUS or v.identified_here()):
        v.website = event.website  # someone else on a known person's browser does not change the person's website
    if touch.kind is TouchKind.UNSUBSCRIBE:
        v.status, v.wake_at = Status.STOPPED, None
    save(v)
    return v


async def events(request: Request) -> Response:
    event = SiteEvent.model_validate_json(await request.body())
    v = heard(event)
    if event.touch.kind in TURNS and not SIMULATED:
        kick(v.id)
    return JSONResponse({})


class Sent(BaseModel):
    visitor: str
    text: str
    asked_through: str = ""  # the page's own field it came from ("Roast my site"); empty for the chat
    website: str = ""  # what they typed into that field, when it is their website
    at: datetime | None = None  # a Minutehand run's time


class SentId(BaseModel):
    id: int


async def send(request: Request) -> Response:
    """They wrote to the agent. Writing in the chat means it is open in front of them, so they have seen all of it;
    the page's own field is not the chat, and says nothing about what they have seen."""
    sent = Sent.model_validate_json(await request.body())
    if len(sent.text) > LONGEST:
        return JSONResponse({"error": "too long"}, status_code=413)
    if not SIMULATED and not affordable(sent.visitor):  # the widget shows it as not sent, to retry
        return JSONResponse({"error": "too many"}, status_code=429)
    v = load(sent.visitor) or Visitor(id=sent.visitor)
    m = v.append(Sender.VISITOR, stamped(sent.at), sent.text.strip(), asked_through=sent.asked_through)
    if not sent.asked_through:
        v.read_through = m.id
    if sent.website and v.status is Status.ANONYMOUS:
        v.website = sent.website
    save(v)
    if not SIMULATED:
        kick(v.id)
    return Response(SentId(id=m.id).model_dump_json(), media_type="application/json")


class Read(BaseModel):
    visitor: str
    through: int


async def read(request: Request) -> Response:
    seen = Read.model_validate_json(await request.body())
    v = load(seen.visitor)
    if v is not None and seen.through > v.read_through:
        v.read_through = min(seen.through, len(v.thread) - 1)
        save(v)
    return JSONResponse({})


class Dismissed(BaseModel):
    visitor: str
    id: int
    at: datetime | None = None


async def dismiss(request: Request) -> Response:
    """They deleted one of the agent's messages from its preview: it stays in the thread, marked, and wakes nothing."""
    gone = Dismissed.model_validate_json(await request.body())
    v = load(gone.visitor)
    if v is not None and 0 <= gone.id < len(v.thread) and v.thread[gone.id].sender is Sender.AGENT:
        v.thread[gone.id].dismissed_at = stamped(gone.at)
        save(v)
    return JSONResponse({})


class ShownMessage(BaseModel):
    id: int
    at: datetime
    sender: Sender
    text: str
    asked_through: str
    offer: Offer | None
    offer_taken: bool
    dismissed: bool


class Thread(BaseModel):
    messages: list[ShownMessage]
    read_through: int
    activity: Activity | None
    open_offer: int | None
    now: datetime


async def thread(request: Request) -> Response:
    """The whole conversation on this browser, every visit, and what the widget needs to draw it. Reading it changes
    nothing."""
    v = load(request.query_params.get("v", ""))
    if v is None:
        empty = Thread(messages=[], read_through=-1, activity=None, open_offer=None, now=clock())
        return Response(empty.model_dump_json(), media_type="application/json")
    taken = v.taken_offers()
    offer = v.open_offer()
    shown = Thread(
        messages=[
            ShownMessage(
                id=m.id,
                at=m.at,
                sender=m.sender,
                text=m.text,
                asked_through=m.asked_through,
                offer=m.offer,
                offer_taken=m.id in taken,
                dismissed=m.dismissed_at is not None,
            )
            for m in v.thread
        ],
        read_through=v.read_through,
        activity=activity.get(v.id),
        open_offer=offer.id if offer is not None else None,
        now=clock(),
    )
    return Response(shown.model_dump_json(), media_type="application/json")


class Shown(BaseModel):
    id: str
    summary: str
    decisions: list[str]


class ShownPage(BaseModel):
    items: list[Shown]


STILL_THERE = timedelta(minutes=30)


def present(v: Visitor, now: datetime) -> bool:
    """The widget is in front of them: their last touch on the site was not leaving it, and not long ago. A run's
    visitor sends no presence every 20 seconds, as the widget does, so a read page counts until they leave."""
    site = [t for t in v.touches if t.kind not in (TouchKind.EMAIL_REPLY, TouchKind.UNSUBSCRIBE)]
    return bool(site) and site[-1].kind is not TouchKind.LEAVING and now - site[-1].at <= STILL_THERE


def _form(offer: Offer) -> str:
    if offer.kind is OfferKind.PERSON:
        return f"\n[Under it, a form: name, work email, website. Button: Have {STUDIO.lead_name} get in touch]"
    return f"\n[Under it, a form: Want {offer.what}? Name, work email, website. Button: Email it to me]"


async def shown(request: Request) -> Response:
    """What the widget puts in front of a Minutehand run's visitor, from the same state the widget draws: each message
    of the agent's they have not opened (popped up over the page, then waiting behind the chat button), and the offer
    whose form is in their chat. Reading it changes nothing."""
    v = load(request.query_params.get("v", ""))
    items: list[Shown] = []
    if v is not None and present(v, clock()):
        offer = v.open_offer()
        unread = v.unread()
        for m in unread:
            can = ["open", "reply", "dismiss", *(["introduce"] if offer is not None and m.id == offer.id else [])]
            form = _form(m.offer) if m.offer is not None and offer is not None and m.id == offer.id else ""
            items.append(Shown(id=str(m.id), summary=m.text + form, decisions=can))
        if offer is not None and offer.offer is not None and all(m.id != offer.id for m in unread):
            items.append(Shown(id=str(offer.id), summary=offer.text + _form(offer.offer), decisions=["reply", "introduce"]))
    return Response(ShownPage(items=items).model_dump_json(), media_type="application/json")


class Visitors(BaseModel):
    visitors: list[Visitor]


async def visitors(request: Request) -> Response:
    """For the studio's people: every visitor, most recently seen first, with what the agent thinks and is doing."""
    if not operator(request):
        return JSONResponse({}, status_code=404)
    seen = sorted(everyone(), key=lambda v: v.last_seen() or datetime.min.replace(tzinfo=UTC), reverse=True)
    return Response(Visitors(visitors=seen).model_dump_json(), media_type="application/json")


async def console(request: Request) -> Response:
    if not operator(request):
        return JSONResponse({}, status_code=404)
    return FileResponse(CONSOLE, headers={"Cache-Control": "no-cache"})


async def unsubscribe(request: Request) -> Response:
    visitor = request.query_params.get("v", "")
    if load(visitor) is not None:
        heard(SiteEvent(visitor=visitor, touch=Touch(kind=TouchKind.UNSUBSCRIBE, at=clock())))
    return HTMLResponse("<p style='font-family:sans-serif'>Done. You won't hear from us again.</p>")


class InboundEmail(BaseModel):
    from_: str = Field(alias="from")
    subject: str = ""
    text: str
    sent_at: datetime | None = None


async def inbound_email(request: Request) -> Response:
    if not operator(request):
        return JSONResponse({}, status_code=404)
    mail = InboundEmail.model_validate_json(await request.body())
    sender = mail.from_.split("<")[-1].rstrip(">").strip().lower()
    v = next((x for x in everyone() if x.email == sender), None)
    if v is not None:
        heard(
            SiteEvent(
                visitor=v.id,
                touch=Touch(kind=TouchKind.EMAIL_REPLY, at=stamped(mail.sent_at), label=mail.subject, text=mail.text),
            )
        )
        if not SIMULATED:
            kick(v.id)
    return JSONResponse({})


# --- a Minutehand run: it owns the clock --------------------------------------------------------------------------


class Wake(BaseModel):
    now: datetime


async def wake(request: Request) -> Response:
    now = Wake.model_validate_json(await request.body()).now
    store.put("last_wake", now.isoformat())
    for v in everyone():  # a run owns the clock: everything finishes before the next moment
        try:  # as on the live site (`_turns`): one visitor's failed turn never stops the others
            await turn(v.id, now)
        except Exception:
            log.exception("turn for %s failed", v.id)
    return JSONResponse({})


async def news_arrived(items: list[Stocked], now: datetime) -> None:
    """Something new in the world: the planner looks again at each known person it changes something for, and at no
    one else. Whether it is worth an email is still the agent's call."""
    known = [v for v in everyone() if v.status is Status.KNOWN and v.handed_over is None]
    for item in items:
        about = [
            (v.id, f"{v.name}: {v.beliefs or 'little known'}; their business: {v.their_business or 'unknown'}")
            for v in known
        ]
        for m in await desk.who_cares(item, about):
            v = load(m.visitor)
            if v is None or v.status is Status.STOPPED:
                continue
            v.wake_at, v.wake_why = (
                now,
                f"the desk learned something that may change things for them: {item.fact} ({m.why})",
            )
            save(v)
            log.info("news %s: wakes for %s", item.source, v.id[:8])
    plans_changed.set()


async def world(request: Request) -> Response:
    """The world's news as it happens: a trade press feed, or a simulated run's newsroom."""
    item = await desk.heard(WorldNews.model_validate_json(await request.body()), clock())
    if item is not None:
        await news_arrived([item], clock())
    return JSONResponse({"kept": item is not None})


async def report(request: Request) -> Response:
    last = store.get("last_wake")
    floor = datetime.fromisoformat(str(last)) + timedelta(seconds=1) if last else None
    people = everyone()
    moments = [v.wake_at for v in people if v.wake_at is not None and v.status is not Status.STOPPED]
    moments += [t.at for v in people for t in v.new_touches() if t.kind in TURNS]
    moments += [m.at for v in people for m in v.unanswered()]
    soonest = min(moments) if moments else None
    if soonest is not None and floor is not None:
        soonest = max(soonest, floor)
    return JSONResponse({"status": "idle", "next_wake": soonest.isoformat() if soonest else None})


# --- the site -----------------------------------------------------------------------------------------------------


async def page(request: Request) -> Response:
    path = request.url.path.strip("/") or "index"
    for candidate in (SITE / f"{path}.html", SITE / path / "index.html", SITE / path):
        if candidate.is_file() and SITE.resolve() in candidate.resolve().parents:
            return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
    return HTMLResponse("Not found", status_code=404)


@asynccontextmanager
async def lifespan(_: Starlette) -> AsyncIterator[None]:
    store.configure(store.SqliteBackend(os.environ.get("AGENT_DB", "studio_agent.db")))
    if not SIMULATED:
        moments = asyncio.create_task(own_moments())
        stocking = asyncio.create_task(desk.keep_stocked(lambda items: news_arrived(items, clock())))
        yield
        moments.cancel()
        stocking.cancel()
    else:
        yield


app = Starlette(
    routes=[
        Route("/api/events", events, methods=["POST"]),
        Route("/api/thread", thread),
        Route("/api/thread/send", send, methods=["POST"]),
        Route("/api/thread/read", read, methods=["POST"]),
        Route("/api/thread/dismiss", dismiss, methods=["POST"]),
        Route("/api/visitors", visitors),
        Route("/console", console),
        Route("/api/unsubscribe", unsubscribe),
        Route("/inbound/email", inbound_email, methods=["POST"]),
        *(  # a Minutehand run's own: it owns the clock and the world, and nobody else may move either
            [
                Route("/api/shown", shown),
                Route("/api/world", world, methods=["POST"]),
                Route("/wake", wake, methods=["POST"]),
                Route("/report", report),
            ]
            if SIMULATED
            else []
        ),
        Route("/{path:path}", page),
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines carry query strings, keys included
    port = int(os.environ.get("PORT") or "8760")
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=port, log_level="warning")
