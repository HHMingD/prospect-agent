"""The studio the agent works for: what it does, its pages, its results. The agent says nothing about the studio
that is not here."""

from __future__ import annotations

import os
from html.parser import HTMLParser
from pathlib import Path

from pydantic import BaseModel

SITE = Path(os.environ.get("SITE_DIR", "site"))


class _Visible(HTMLParser):
    """The words a person reads on a page: no script, style or head."""

    def __init__(self) -> None:
        super().__init__()
        self.words: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head", "nav"):
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head", "nav"):
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden and data.strip():
            self.words.append(" ".join(data.split()))


def _shown(path: str) -> str:
    """What the page at `path` says, as published on the studio's site."""
    file = SITE / (path.strip("/") or "index")
    file = file / "index.html" if file.is_dir() else file.with_suffix(".html")
    if not file.exists():
        return ""
    seen = _Visible()
    seen.feed(file.read_text())
    return " ".join(seen.words)


class Page(BaseModel):
    path: str
    title: str
    what: str
    client: str = ""  # the client this page tells the story of, for a case study
    studio_take: str = ""  # the studio's own honest view of this page
    behind_the_scenes: str = ""


class Result(BaseModel):
    client: str
    said: str


class Source(BaseModel):
    """One thing the agent may rest a claim on, and the client whose story it is, when it is one."""

    where: str
    says: str
    client: str = ""
    link: str = ""  # where an outside source came from: what it told them can be told only once


class Studio(BaseModel):
    name: str
    city: str
    lead_name: str
    lead_email: str
    what_we_do: str
    facts: list[str]
    pages: list[Page]
    results: list[Result]

    def clients(self) -> list[str]:
        return [r.client for r in self.results]

    def page(self, path: str) -> Page | None:
        return next((p for p in self.pages if p.path == path), None)

    def brief(self) -> str:
        lines = [
            f"{self.name}, {self.city}. {self.what_we_do}",
            "Facts:",
            *(f"- {f}" for f in self.facts),
            "Results:",
            *(f"- {r.client}: {r.said}" for r in self.results),
            "The website's pages:",
            *(f"- {p.path} ({p.title}): {self._about(p)}" for p in self.pages),
        ]
        return "\n".join(lines)

    def sources(self) -> list[Source]:
        """The brief cut where one client's story ends, so a claim about a client can be held to that client's own
        words."""
        return [
            Source(where="the studio", says=" ".join([f"{self.name}, {self.city}. {self.what_we_do}", *self.facts])),
            *(
                Source(where=f"result for {r.client}", says=f"{r.client}: {r.said}", client=r.client)
                for r in self.results
            ),
            *(Source(where=f"page {p.path}", says=f"{p.title}: {self._about(p)}", client=p.client) for p in self.pages),
        ]

    @staticmethod
    def _about(p: Page) -> str:
        line = p.what
        if p.studio_take:
            line += f" The studio's own take: {p.studio_take}"
        if p.behind_the_scenes:
            line += f" Behind the scenes: {p.behind_the_scenes}"
        if shown := _shown(p.path):
            line += f" The page says: {shown}"
        return line


STUDIO = Studio.model_validate_json(Path(os.environ.get("STUDIO", "studio.json")).read_bytes())
