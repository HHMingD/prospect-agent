"""The email, on its own. Every email the agent sent in a simulated run is a case: what the writer was given that turn,
the person it went to as they really are, and what was sent. The writer is run on each case again and judged against
what was sent, by a judge who knows the person, so the email improves by volume without a simulation.

    uv run python eval/emails.py capture <minutehand state>...      # cases into eval/cases/emails/
    uv run python eval/emails.py run [--drafts 4] [--only name,...] [--send you@example.com]
"""

from __future__ import annotations

import asyncio
import json

import httpx
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from agent import emails, mail
from agent.studio import STUDIO

CASES = Path(__file__).parent / "cases" / "emails"
WRITTEN = Path(__file__).parent / "written" / "emails"  # every email the writer wrote, to read in full
JUDGE_MODEL = "openai:gpt-5"
WHAT_FOR = (
    "what this turn calls for, from why you were woken (above): what they left their email for, the reply they "
    "wrote, or what changed in their world"
)


class Case(BaseModel):
    name: str
    run: str
    person: dict[str, object]
    shown: str = Field(description="What the writer was given: everything the main agent was given that turn.")
    sent_subject: str
    sent: str


def capture(states: list[Path]) -> None:
    """One case per email-writing turn of the main agent, from the model calls each run recorded."""
    CASES.mkdir(parents=True, exist_ok=True)
    made = 0
    for state in states:
        for run in sorted((state / "runs").iterdir()):
            scenario = run / "scenario.json"
            if not scenario.exists():
                continue
            person = json.loads(scenario.read_text())["people"][0]
            out = subprocess.run(
                ["uv", "run", "minutehand", "model-calls", run.name, "--state", str(state)],
                capture_output=True, text=True,
            ).stdout
            turns: dict[str, dict[str, str]] = {}
            for line in filter(None, out.split("\n")):
                messages = json.loads(line)["body"].get("messages") or []
                if len(messages) < 2 or "You are the executor" not in str(messages[0].get("content")):
                    continue
                shown = str(messages[1]["content"])
                answered = {m.get("tool_call_id"): str(m.get("content")) for m in messages if m.get("role") == "tool"}
                for m in messages:
                    for call in m.get("tool_calls") or []:
                        if call["function"]["name"] == "send_email" and answered.get(call["id"], "").startswith("sent"):
                            args = json.loads(call["function"]["arguments"])
                            if "text" in args:  # only what really went: an attempt the agent was refused is not one
                                turns.setdefault(shown, args)
            for i, (shown, args) in enumerate(turns.items()):
                case = Case(
                    name=f"{state.name}-{run.name[:8]}-{person['key']}-{i}",
                    run=run.name,
                    person=person,
                    shown=shown,
                    sent_subject=args.get("subject", ""),
                    sent=args["text"],
                )
                (CASES / f"{case.name}.json").write_text(case.model_dump_json(indent=1))
                made += 1
    print(f"{made} case(s) in {CASES}")


class Score(BaseModel):
    specific: int = Field(ge=1, le=5, description="Could only have been written to this person and their business.")
    useful: int = Field(ge=1, le=5, description="Gives them something they can act on or did not know.")
    voice: int = Field(ge=1, le=5, description="Sharp, warm, human; not a sales email, not a template, not a recap.")
    fits: int = Field(ge=1, le=5, description="Answers what they asked for or what really matters to them now.")
    would_reply: int = Field(ge=1, le=5, description="How likely this person, as they really are, answers or forwards it.")
    invented: list[str] = Field(
        description="Each fact, number, client story or piece of news in it that nothing the agent was given supports. "
        "Opinions and advice are not facts."
    )
    promises: list[str] = Field(description="Each commitment the studio makes: a call, date, price, work to come.")
    worst: str = Field(description="The single biggest thing wrong with it, in a sentence.")

    @property
    def total(self) -> int:
        return self.specific + self.useful + self.voice + self.fits + self.would_reply


JUDGE = """You judge an email a marketing studio's agent sent to a person who left their email on its site. You know
the person as they really are: their profile and private facts. You are also given everything the agent was given
when it wrote: never mark it down for not knowing what it could not, but check every fact in it against that. Score
it hard, as the person would read it on their phone between other work: would they forward it to a colleague, or
answer it today? A 3 is competent and forgettable; most agency emails are a 2; a 5 is one you would screenshot.
Anything that could have gone to another business in their category, filler statistics, a template shape or
marketing-speak caps specific and voice at 2."""

judge = Agent(JUDGE_MODEL, output_type=Score, instructions=JUDGE, model_settings=ModelSettings(timeout=240), retries=2)


async def judged(case: Case, subject: str, text: str) -> Score:
    p = case.person
    return (
        await judge.run(
            f"The person: {p['name']}. {p['profile']}\nTheir private facts: {' '.join(map(str, p.get('facts', [])))}"
            f"\n\nWhat the agent was given: the studio brief\n{STUDIO.brief()}\n\nand this turn\n{case.shown}\n\nThe email, subject \"{subject}\":\n{text}"
        )
    ).output


def line(s: Score) -> str:
    flags = (f" INVENTED {len(s.invented)}" if s.invented else "") + (f" PROMISES {len(s.promises)}" if s.promises else "")
    return (
        f"{s.total:2}/25 (specific {s.specific}, useful {s.useful}, voice {s.voice}, fits {s.fits}, reply "
        f"{s.would_reply}){flags}"
    )


SENDING = asyncio.Lock()  # Resend allows two a second: one at a time, spaced, and a 429 waited out


async def _send(to: str, subject: str, text: str) -> None:
    async with SENDING:
        for attempt in range(6):
            try:
                await mail.send("examples", to, subject, text)
                await asyncio.sleep(1)
                return
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 429:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
        print(f"   ! not sent after retries: {subject}")


async def one(case: Case, drafts: int, send: str, gate: asyncio.Semaphore) -> tuple[Score, Score | None]:
    async with gate:
        written = await emails.write(case.shown, WHAT_FOR, drafts)
        before = await judged(case, case.sent_subject, case.sent)
        after = await judged(case, written[0].subject, written[0].text) if written else None
    print(f"\n=== {case.name} -> {case.person['name']}")
    print(f"BEFORE {line(before)}  worst: {before.worst}")
    if after is None:
        print("AFTER  nothing written")
        return before, None
    print(f"AFTER  {line(after)}  worst: {after.worst}")
    for x in after.invented + after.promises:
        print(f"   ! {x}")
    print(f"subject: {written[0].subject}\n{written[0].text}")
    WRITTEN.mkdir(parents=True, exist_ok=True)
    (WRITTEN / f"{case.name}.txt").write_text(
        f"To: {case.person['name']} ({case.person['profile']})\nJudge: {line(after)}\nWorst: {after.worst}\n"
        + "".join(f"! {x}\n" for x in after.invented + after.promises)
        + f"\nSubject: {written[0].subject}\n\n{written[0].text}\n\n==== Sent before ({line(before)})\n"
        f"Subject: {case.sent_subject}\n\n{case.sent}\n"
    )
    if send:
        await _send(
            send,
            f"[email eval · {case.person['name']}] {written[0].subject}",
            f"{written[0].text}\n\n----\nSimulated person: {case.person['name']}, {case.person['profile']}\n"
            f"Judge: {line(after)}. Worst: {after.worst}\nWas sent before ({before.total}/25): {case.sent_subject}",
        )
    return before, after


async def run(args: list[str]) -> None:
    drafts = int(args[args.index("--drafts") + 1]) if "--drafts" in args else emails.DRAFTS
    only = set(args[args.index("--only") + 1].split(",")) if "--only" in args else set()
    skip = set(args[args.index("--skip") + 1].split(",")) if "--skip" in args else set()
    send = args[args.index("--send") + 1] if "--send" in args else ""
    cases = [Case.model_validate_json(f.read_text()) for f in sorted(CASES.glob("*.json"))]
    cases = [c for c in cases if (not only or any(o in c.name for o in only)) and c.name not in skip]
    gate = asyncio.Semaphore(4)
    results = await asyncio.gather(*(one(c, drafts, send, gate) for c in cases))
    before = [b for b, _ in results]
    after = [a for _, a in results if a is not None]
    mean = lambda xs: sum(x.total for x in xs) / max(1, len(xs))  # noqa: E731
    print(
        f"\n{len(cases)} case(s), {drafts} draft(s) each. BEFORE mean {mean(before):.1f}/25, "
        f"{sum(bool(b.invented) for b in before)} with inventions. AFTER mean {mean(after):.1f}/25 over {len(after)}, "
        f"{sum(bool(a.invented) for a in after)} with inventions, {sum(bool(a.promises) for a in after)} with promises."
    )


if __name__ == "__main__":
    if sys.argv[1] == "capture":
        capture([Path(p) for p in sys.argv[2:]])
    else:
        asyncio.run(run(sys.argv[2:]))
