"""What the agent knows about each visitor, kept in its memory (`minutehand.agent.store`) under `visitors/<id>`.

A visitor starts as a cookie and a stream of touches. Everything else is the agent's own: the conversation on the site
(its thread), the emails it sent, what it believes about them, what it is preparing, and when it chose to look at them
again. A name and an email turn the same record into a known person, and the anonymous history stays with them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

VISIT_GAP = timedelta(minutes=30)  # quiet this long, and what they do next is a new visit


class TouchKind(StrEnum):
    VIEW = "view"  # opened a page
    READ = "read"  # left a page: how long they stayed and how far they scrolled
    CLICK = "click"  # touched something on the page: a filter, the pricing toggle, an image, a link out
    LINGER = "linger"  # stayed on one thing on the page, an image or a result, for a few seconds
    SIGNAL = "signal"  # answered one of the page's own questions: a headache chip, "show me work for"
    CHAT_OPENED = "chat_opened"  # expanded the chat: interest, never a question
    CHAT_CLOSED = "chat_closed"
    LEAVING = "leaving"  # the pointer went for the tab bar or the tab was hidden
    PRESENCE = "presence"  # still on the page, nothing new: tells the agent they are here, never wakes it
    IDENTIFY = "identify"  # gave a name and an email in the form under an offer
    EMAIL_REPLY = "email_reply"  # answered one of the agent's emails
    UNSUBSCRIBE = "unsubscribe"


OFF_SITE = {TouchKind.EMAIL_REPLY, TouchKind.UNSUBSCRIBE}  # not something they did on the site


class Touch(BaseModel):
    """One thing a visitor did, as the site (or the mail provider) reports it."""

    kind: TouchKind
    at: datetime
    page: str = ""
    title: str = ""
    label: str = ""  # what they touched or chose, in the page's words
    text: str = ""  # what they typed into the page itself
    seconds: int | None = None  # time on the page, for READ
    scroll: int | None = None  # furthest scroll, percent, for READ
    referrer: str = ""
    campaign: str = ""
    device: str = ""
    timezone: str = ""


class SiteEvent(BaseModel):
    """What the widget posts: whose touch it is, and the touch."""

    visitor: str
    touch: Touch
    name: str = ""
    email: str = ""
    website: str = ""


class Sender(StrEnum):
    AGENT = "agent"
    VISITOR = "visitor"


class OfferKind(StrEnum):
    WRITEUP = "writeup"  # something the agent writes and emails them: a teardown, the closest case written up
    PERSON = "person"  # the studio's lead getting in touch


class Offer(BaseModel):
    kind: OfferKind
    what: str  # WRITEUP: what it will email, as it finishes "Want ...?"; PERSON: what the lead would get in touch about


class Message(BaseModel):
    """One message in the conversation on the site. The thread only grows: nothing in it is ever removed."""

    id: int  # its place in the thread
    at: datetime
    sender: Sender
    text: str
    asked_through: str = ""  # a visitor message sent through the page's own field ("Roast my site"), not the chat
    offer: Offer | None = None  # agent only: the form under it
    cites: list[str] = Field(default_factory=list)  # agent only: the outside sources it rested on
    dismissed_at: datetime | None = None  # agent only: they deleted its preview


class Email(BaseModel):
    """An email the agent sent them."""

    at: datetime
    subject: str
    text: str
    cites: list[str] = Field(default_factory=list)


class Status(StrEnum):
    ANONYMOUS = "anonymous"
    KNOWN = "known"  # gave a name and email: the agent writes to them when something real changes in their world
    STOPPED = "stopped"  # unsubscribed or said no: never written to again


class Company(BaseModel):
    """What enrichment found about them and where they work."""

    person_title: str = ""
    seniority: str = ""
    linkedin: str = ""
    company: str = ""
    domain: str = ""
    industry: str = ""
    employees: int | None = None
    revenue: str = ""
    city: str = ""
    description: str = ""
    keywords: list[str] = []


class Researched(BaseModel):
    """One thing the executor's research found, with where it came from."""

    at: datetime
    fact: str
    source: str
    published: str = ""


class Look(BaseModel):
    """The planner's last read of them, as the studio's people see it."""

    at: datetime
    decision: str  # what the moment was and what it decided, in a line
    learned: str = ""  # its read: who they probably are and what they want, each point with the action it came from
    brief: str = ""  # what it asked the executor for


class Visitor(BaseModel):
    id: str
    touches: list[Touch] = []
    thread: list[Message] = []
    read_through: int = -1  # the last message of the thread they have seen
    emails: list[Email] = []
    status: Status = Status.ANONYMOUS
    name: str = ""
    email: str = ""
    website: str = ""
    company: Company | None = None
    beliefs: str = ""  # who the agent thinks they are, how sure, what they seem to want
    line_of_work: str = ""  # what they do, in their own words: asked before who they are
    preparing: str = ""  # what it is working on for them
    their_business: str = ""  # what their website and the lookup say about their business
    research: list[Researched] = []  # what the executor found, oldest first
    wake_at: datetime | None = None  # when the planner chose to look at them again
    wake_why: str = ""
    looked_until: datetime | None = None  # every touch and message up to here has been in front of the planner
    answered_through: int = -1  # the last visitor message the executor has answered
    main_wakes: int = 0  # turns the executor has taken for them
    last_look: Look | None = None
    main_now: str = ""  # what the agent is doing for them this moment, for the studio's people
    handed_over: datetime | None = None  # when the studio's people took them over: never handed over twice
    owed_missed: datetime | None = None  # when a turn failed to write the email they left their address for

    # --- when -------------------------------------------------------------------------------------------------

    def on_site_times(self) -> list[datetime]:
        return sorted(
            [t.at for t in self.touches if t.kind not in OFF_SITE]
            + [m.at for m in self.thread if m.sender is Sender.VISITOR]
        )

    def last_seen(self) -> datetime | None:
        times = self.on_site_times()
        return times[-1] if times else None

    def visit_start(self) -> datetime | None:
        """When the visit they are on (or last made) began: the first thing they did after a quiet of VISIT_GAP."""
        times = self.on_site_times()
        if not times:
            return None
        start = times[-1]
        for earlier in reversed(times[:-1]):
            if start - earlier > VISIT_GAP:
                break
            start = earlier
        return start

    def visits(self) -> int:
        times = self.on_site_times()
        return sum(1 for i, t in enumerate(times) if i == 0 or t - times[i - 1] > VISIT_GAP)

    def page_now(self) -> str:
        return next((t.page for t in reversed(self.touches) if t.kind is TouchKind.VIEW), "")

    # --- who --------------------------------------------------------------------------------------------------

    def identified_here(self) -> bool:
        """They told the agent who they are on this visit. A browser is not a person: on any other visit, whoever is
        in front of it may be someone else."""
        start = self.visit_start()
        return start is not None and any(t.kind is TouchKind.IDENTIFY and t.at >= start for t in self.touches)

    def as_stranger(self) -> Visitor:
        """This visit alone, with nobody's name, email, business or research on it: what the agent may use on the
        site when the browser belongs to someone who has not said, on this visit, that it is them."""
        start = self.visit_start()
        return Visitor(
            id=self.id,
            touches=[t for t in self.touches if start is not None and t.at >= start],
            thread=[m for m in self.thread if start is not None and m.at >= start],
            read_through=self.read_through,
            looked_until=self.looked_until,
            answered_through=self.answered_through,
        )

    # --- the conversation -------------------------------------------------------------------------------------

    def unread(self) -> list[Message]:
        return [
            m for m in self.thread if m.sender is Sender.AGENT and m.id > self.read_through and m.dismissed_at is None
        ]

    def unanswered(self) -> list[Message]:
        """What they wrote that the executor has not answered yet."""
        return [m for m in self.thread if m.sender is Sender.VISITOR and m.id > self.answered_through]

    def taken_offers(self) -> set[int]:
        """The offers they gave their email for: the one open when each form was sent."""
        taken: set[int] = set()
        for t in self.touches:
            if t.kind is TouchKind.IDENTIFY:
                before = [m for m in self.thread if m.offer is not None and m.at <= t.at]
                if before:
                    taken.add(before[-1].id)
        return taken

    def open_offer(self) -> Message | None:
        """The one offer whose form is on screen: the latest, unless they already gave their email for it."""
        offers = [m for m in self.thread if m.offer is not None]
        if not offers or offers[-1].id in self.taken_offers():
            return None
        return offers[-1]

    def new_touches(self) -> list[Touch]:
        return [t for t in self.touches if self.looked_until is None or t.at > self.looked_until]

    def append(
        self,
        sender: Sender,
        at: datetime,
        text: str,
        asked_through: str = "",
        offer: Offer | None = None,
        cites: list[str] | None = None,
    ) -> Message:
        m = Message(
            id=len(self.thread),
            at=at,
            sender=sender,
            text=text,
            asked_through=asked_through,
            offer=offer,
            cites=cites or [],
        )
        self.thread.append(m)
        return m
