"""The email. A writer given nothing but the goal writes it several times over, each its own way; the draft the person
would most likely answer or forward goes first. Only facts and promises are held to anything, and that check runs on
the draft picked, by whoever sends it."""

from __future__ import annotations

import asyncio
import logging
import os

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIResponsesModelSettings

from agent.studio import STUDIO

log = logging.getLogger(__name__)

WRITER = os.environ.get("AGENT_WRITER_MODEL", "openai-responses:gpt-6-luna")
DRAFTS = int(os.environ.get("EMAIL_DRAFTS", "4"))  # quantity, then choice: never a template
WRITING = OpenAIResponsesModelSettings(timeout=240, openai_reasoning_effort="low")  # nobody waits on the page for an email

WRITE = f"""You write one email from {STUDIO.name}, a marketing studio, to one person, from everything you are
given about them below. It has one test to pass: they forward it to a colleague, or answer it today.

Everything else is yours: what it says, the angle, how long it is, the subject, and what form it takes: a teardown of
the page where their problem lives, copy they could paste in this afternoon, the test to run next week, a plan, an
event worth their time, the one question that changes their mind, something true about their own business they had
not seen. Write it the way the studio's best strategist writes to someone they would like to work with.

Two things are not yours. Facts: every fact, number, client story and piece of news comes from what you are given
(the studio brief, their own site, their words, the research), never from you; your own judgement, advice and
opinions need no source. Promises: the studio commits to nothing in an email (no calls, meetings, dates, deadlines,
prices or work to come); when they want any of that, {STUDIO.lead_name} from the studio answers it. Sign it
"{STUDIO.name}"."""


class Draft(BaseModel):
    subject: str
    text: str = Field(description="The body, signed.")


drafter = Agent(WRITER, output_type=Draft, instructions=WRITE, model_settings=WRITING, retries=2)


class Pick(BaseModel):
    order: list[int] = Field(description="Every draft's number, the one they would most likely answer or forward first.")
    why: str = Field(description="In a sentence, why the first beats the rest, for this person.")


PICK = """You are given what a marketing studio knows about one person, and several emails it could send them. Read
each as that person would: on their phone, between other work, with their own business on their mind. Put them in the
order they would most likely answer it or forward it to a colleague, best first. What they asked for, their own site,
their own words and their real situation decide it; polish, length and cleverness do not."""

picker = Agent(WRITER, output_type=Pick, instructions=PICK, model_settings=WRITING, retries=2)


async def write(context: str, what_for: str, drafts: int = DRAFTS) -> list[Draft]:
    """Drafts of the email `what_for` asks for, best first. Empty if every attempt failed."""
    asked = f"{context}\n\nThe email is for: {what_for}"
    made = await asyncio.gather(*(drafter.run(asked) for _ in range(drafts)), return_exceptions=True)
    written = [m.output for m in made if not isinstance(m, BaseException)]
    for m in made:
        if isinstance(m, BaseException):
            log.warning("email draft failed: %s", m)
    if len(written) < 2:
        return written
    shown = "\n\n".join(f"=== Draft {i}\nSubject: {d.subject}\n{d.text}" for i, d in enumerate(written))
    try:
        pick = (await picker.run(f"{asked}\n\nThe drafts:\n\n{shown}")).output
    except Exception as e:  # an unpicked draft is still a draft: keep the order they came in
        log.warning("email pick failed: %s", e)
        return written
    order = [i for i in dict.fromkeys(pick.order) if 0 <= i < len(written)]
    order += [i for i in range(len(written)) if i not in order]
    log.info("email picked draft %d of %d: %s", order[0], len(written), pick.why)
    return [written[i] for i in order]
