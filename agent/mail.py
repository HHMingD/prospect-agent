"""Sending the agent's emails: through Resend in the world, or to the mail host a Minutehand run declares."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx

FROM = os.environ.get("MAIL_FROM", "Fieldwork Studio <hello@fieldwork.example>")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8760")


def footer(visitor: str) -> str:
    return f"\n\n--\nNot useful? One click and I stop: {PUBLIC_URL}/api/unsubscribe?v={visitor}"


def _append(path: Path, text: str) -> None:
    with path.open("a") as f:
        f.write(text)


async def send(visitor: str, to: str, subject: str, text: str) -> None:
    body = text + footer(visitor)
    outbox = os.environ.get("MAIL_OUTBOX")  # local trials: write the email to a file instead of sending it
    if outbox:
        await asyncio.to_thread(_append, Path(outbox), f"\n=== to {to} | {subject}\n{body}\n")
        return
    async with httpx.AsyncClient(timeout=30) as http:
        sim = os.environ.get("MAIL_URL")
        if sim:
            sent = await http.post(sim, json={"to": to, "from": FROM, "subject": subject, "text": body})
        else:
            key = os.environ.get("RESEND_API_KEY", "")
            if not key:
                raise RuntimeError("RESEND_API_KEY is not set")
            sent = await http.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}"},
                json={"from": FROM, "to": [to], "subject": subject, "text": body},
            )
        sent.raise_for_status()
