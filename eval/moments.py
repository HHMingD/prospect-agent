"""Did the agent speak at the right moments, and did people answer? Read from what the visitor did, not from what the
agent wrote.

For every simulated visitor: their visit and the conversation on one timeline. Each of the agent's messages is judged
by what the person did with it: wrote back after it, opened the chat on it, deleted its pop-up, or never opened it.
Each message they wrote is judged by how long its answer took in real seconds (the simulated clock waits for the
agent; a person does not), and one with no answer before they wrote again or left is a missed answer.

    uv run python eval/moments.py <minutehand state> [--timelines]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LATE = 20.0  # real seconds a person waits for an answer before it has missed them
SAID = re.compile(r"^(\S+ \S+) main \S+ site: (.*)$", re.MULTILINE)
PLANNED = re.compile(
    r"^(\S+ \S+) plan \S+ (\w+) [\d.]+/[\d.]+( \+act)? next \S+(?: \d+:\d\d:\d\d)? \((answered|own look|they acted)\):",
    re.MULTILINE,
)


def query(state: Path, run: str, sql: str) -> list[dict[str, object]]:
    out = subprocess.run(
        ["uv", "run", "minutehand", "query", run, "--state", str(state), "--format", "json", sql],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    try:
        return json.loads(out or "[]")
    except json.JSONDecodeError:
        return []


def wall(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").astimezone()


@dataclass
class Said:
    """One of the agent's messages."""

    text: str
    real: datetime  # when it joined the thread
    id: int | None = None  # its place in the thread, once the visitor's widget showed it
    shown_at: str = ""  # simulated time it was first in front of them
    fate: str = "never opened"  # wrote back, opened, deleted, never opened


@dataclass
class Wrote:
    """One message the visitor wrote."""

    text: str
    sim: str
    real: datetime | None
    answer_after: float | None = None  # real seconds until the agent's next message


@dataclass
class Visit:
    name: str
    rows: list[tuple[str, float, str]] = field(default_factory=list)
    said: list[Said] = field(default_factory=list)
    wrote: list[Wrote] = field(default_factory=list)
    looks: int = 0
    acted: int = 0
    gave_email: bool = False


def read(state: Path, run: Path) -> Visit:
    visit = Visit(json.loads((run / "scenario.json").read_text())["name"])
    log = (run / "agent.log").read_text() if (run / "agent.log").exists() else ""
    visit.said = [Said(text=m[2].strip(), real=wall(m[1])) for m in SAID.finditer(log)]
    planned = list(PLANNED.finditer(log))
    visit.looks, visit.acted = len(planned), sum(1 for m in planned if m[3])
    acted = query(state, run.name, "select at, wall_time from events where actor = 'person' and operation = 'create' order by seq")
    real_at = {str(a["at"]): datetime.fromisoformat(str(a["wall_time"])) for a in acted}
    calls = query(state, run.name, "select at, path, request_body b, response_body r from calls where is_agent = 0 and "
                  "(path like '/api/events%' or path like '/api/thread/%' or path like '/api/shown%') order by call_id")
    read_through, dismissed = -1, set[int]()
    for c in calls:
        at, path = str(c["at"]), str(c["path"])
        body = json.loads(str(c["b"])) if c["b"] else {}
        if path.startswith("/api/events"):
            t = body["touch"]
            visit.gave_email |= t["kind"] == "identify"
            what = t.get("text") or t.get("label") or t.get("page") or ""
            visit.rows.append((at, 0, f"  they  {t['kind']:11} {str(what)[:90]}"))
        elif path.startswith("/api/thread/send"):
            visit.wrote.append(Wrote(text=body["text"], sim=at, real=real_at.get(at)))
            how = f"through '{body['asked_through']}'" if body.get("asked_through") else "in the chat"
            visit.rows.append((at, 0, f"  they  wrote {how}: {body['text'][:90]}"))
        elif path.startswith("/api/thread/read"):
            read_through = max(read_through, int(body["through"]))
            visit.rows.append((at, 0, "  they  opened the chat"))
        elif path.startswith("/api/thread/dismiss"):
            dismissed.add(int(body["id"]))
            visit.rows.append((at, 0, "  they  deleted a pop-up"))
        elif path.startswith("/api/shown"):
            try:
                items = json.loads(str(c["r"]))["items"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            for item in items:
                text = str(item["summary"]).split("\n[Under it")[0]
                said = next((s for s in visit.said if s.id is None and s.text[:60] == text[:60]), None)
                if said is not None:
                    said.id, said.shown_at = int(item["id"]), at
    for w in visit.wrote:  # how long until the agent's next message, in real time
        after = [s.real for s in visit.said if w.real is not None and s.real >= w.real]
        if after and w.real is not None:
            w.answer_after = (min(after) - w.real).total_seconds()
    for s in visit.said:
        wrote_after = any(w.real is not None and w.real > s.real for w in visit.wrote)
        if s.id is not None and s.id in dismissed:
            s.fate = "deleted"
        elif wrote_after:
            s.fate = "wrote back"
        elif s.id is not None and s.id <= read_through:
            s.fate = "opened"
        visit.rows.append((s.shown_at or "~", 1, f"  agent [{s.fate}] {s.text[:110]}"))
    for w in visit.wrote:
        if w.answer_after is None:
            visit.rows.append((w.sim, 2, "  MISSED: nothing answered what they wrote"))
        elif w.answer_after > LATE:
            visit.rows.append((w.sim, 2, f"  answered after {w.answer_after:.0f}s real"))
    return visit


def main() -> None:
    state, show = Path(sys.argv[1]), "--timelines" in sys.argv
    visits = [read(state, r) for r in sorted((state / "runs").iterdir()) if (r / "scenario.json").exists()]
    said = [s for v in visits for s in v.said]
    wrote = [w for v in visits for w in v.wrote]
    waits = sorted(w.answer_after for w in wrote if w.answer_after is not None)
    if show:
        for v in visits:
            print(f"\n### {v.name}\n" + "\n".join(f"{s[11:19]} {line}" for s, _, line in sorted(v.rows)))
    n = max(1, len(said))
    fates = {f: sum(s.fate == f for s in said) for f in ("wrote back", "opened", "deleted", "never opened")}
    print(f"\n{len(visits)} visitors, {len(said)} agent messages ({len(said) / max(1, len(visits)):.1f} each)")
    print("  what they did with them: " + ", ".join(f"{k} {c} ({100 * c // n}%)" for k, c in fates.items()))
    print(f"  messages they wrote: {len(wrote)}, never answered: {sum(w.answer_after is None for w in wrote)}")
    if waits:
        print(f"  real wait for an answer: median {waits[len(waits) // 2]:.0f}s, slower than {LATE:.0f}s "
              f"{sum(w > LATE for w in waits)} of {len(waits)}")
    print(f"  planner looks: {sum(v.looks for v in visits)}, the executor acted on {sum(v.acted for v in visits)}")
    print(f"  visitors who gave their email: {sum(v.gave_email for v in visits)}")


if __name__ == "__main__":
    main()
