"""Turn a real visit to the site into a Minutehand scenario that replays it.

The visit's navigation (pages, reads, clicks, the page's own choices, requests, leaving) becomes scripted `uses` at
the offsets they happened. Its reactions to the agent (Tell me more, deleting a card, chat replies, the form) are left
out: in the replay the agent says new things, so a model playing the visitor reacts to those instead.

    PYTHONPATH=. uv run python simulation/from_visit.py <visitor id or its start> --name maya --profile "..." > simulation/replay.yaml

Reads the agent's own store (AGENT_DB, default studio_agent.db), never writes it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

from minutehand.agent import store

from agent.visitors import Touch, TouchKind, Visitor

NAVIGATION = {
    TouchKind.VIEW,
    TouchKind.READ,
    TouchKind.CLICK,
    TouchKind.SIGNAL,
    TouchKind.REQUEST,
    TouchKind.LEAVING,
}
"""What the visitor does on the site whatever the agent says; everything else is a reaction to it."""


def _use(t: Touch) -> tuple[str, dict[str, str]]:
    match t.kind:
        case TouchKind.VIEW:
            return "views", {"page": t.page, "title": t.title}
        case TouchKind.READ:
            return "reads", {"page": t.page, "how": f"{t.seconds or 0} seconds, scrolled {t.scroll or 0}%"}
        case TouchKind.CLICK:
            return "clicks", {"page": t.page, "label": t.label}
        case TouchKind.SIGNAL:
            return "picks", {"page": t.page, "question": t.label, "answer": t.text}
        case TouchKind.REQUEST:
            return "asks", {"page": t.page, "label": t.label, "text": t.text}
        case _:
            return "leaves", {"page": t.page}


def scenario(v: Visitor, key: str, name: str, email: str, profile: str, facts: list[str]) -> dict[str, object]:
    touches = [t for t in v.touches if t.kind in NAVIGATION]
    if not touches:
        raise SystemExit(f"visitor {v.id} did nothing on the site to replay")
    start = touches[0].at
    happenings: list[dict[str, object]] = []
    for t in touches:
        use, inputs = _use(t)
        seconds = int((t.at - start) / timedelta(seconds=1))
        happenings.append(
            {"kind": "uses", "person": key, "inbox": "site", "use": use, "after": f"PT{seconds}S", "inputs": inputs}
        )
    return {
        "name": f"replay_{v.id[:8]}",
        "starts_at": start.isoformat(),
        "runs_for": "P21D",
        "max_wakes": 80,
        "people": [
            {
                "key": key,
                "name": name,
                "email": email,
                "profile": profile,
                "facts": facts,
                "reply_within": {"min": "PT3H", "max": "P2D"},
                "reply": {"kind": "answers"},
            }
        ],
        "happenings": happenings,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("visitor", help="the visitor's id, or the start of it")
    p.add_argument("--key", default="visitor")
    p.add_argument("--name", default="A visitor")
    p.add_argument("--email", default="visitor@example.com")
    p.add_argument("--profile", required=True, help="who the model plays: their business and how they browse")
    p.add_argument("--fact", action="append", default=[], help="something they know; repeat for each")
    args = p.parse_args()
    store.configure(store.SqliteBackend(os.environ.get("AGENT_DB", "studio_agent.db")))
    found = [Visitor.model_validate(kept) for k, kept in store.list("visitors/") if k.startswith(f"visitors/{args.visitor}")]
    if len(found) != 1:
        raise SystemExit(f"{len(found)} visitors start with {args.visitor!r}; give more of the id")
    json.dump(scenario(found[0], args.key, args.name, args.email, args.profile, args.fact), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
