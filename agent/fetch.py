"""The outside data Ayven uses, here too: Perplexity for search with sources, ScraperAPI for a page that will not open
in a plain browser (a bot wall, a Cloudflare challenge).

    PERPLEXITY_API_KEY, SCRAPERAPI_KEY
"""

from __future__ import annotations

import asyncio
import os
from html.parser import HTMLParser

import httpx
from pydantic import BaseModel

PERPLEXITY = "https://api.perplexity.ai/chat/completions"
SEARCHING = asyncio.Semaphore(2)  # Perplexity refuses bursts: two at a time, and a 429 waited out
SCRAPER = "https://api.scraperapi.com"


class Cited(BaseModel):
    url: str
    title: str = ""
    date: str = ""
    snippet: str = ""


class Searched(BaseModel):
    answer: str
    cited: list[Cited]


async def search(query: str, recency: str = "month") -> Searched:
    """One Perplexity search: its answer and every source it read. `recency`: day, week, month or year."""
    key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not key:
        raise RuntimeError("PERPLEXITY_API_KEY is not set")
    body = {
        "model": "sonar",
        "messages": [{"role": "user", "content": query}],
        "return_citations": True,
        "search_recency_filter": recency,
    }
    async with SEARCHING, httpx.AsyncClient(timeout=60) as http:
        for wait in (2, 4, 8, 16, 0):
            got = await http.post(PERPLEXITY, headers={"Authorization": f"Bearer {key}"}, json=body)
            if got.status_code != 429 or not wait:
                break
            await asyncio.sleep(wait)
        got.raise_for_status()
    said = PerplexityAnswer.model_validate(got.json())
    cited = [
        Cited(url=r.url, title=r.title or "", date=r.date or "", snippet=r.snippet or "")
        for r in said.search_results
        if r.url
    ] or [Cited(url=u) for u in said.citations if u]
    return Searched(answer=said.choices[0].message.content if said.choices else "", cited=cited)


class _Message(BaseModel):
    content: str = ""


class _Choice(BaseModel):
    message: _Message


class _Result(BaseModel):
    url: str = ""
    title: str | None = None
    date: str | None = None
    snippet: str | None = None


class PerplexityAnswer(BaseModel):
    """The part of Perplexity's answer read here: its wire format, not ours."""

    choices: list[_Choice] = []
    search_results: list[_Result] = []
    citations: list[str] = []


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.words: list[str] = []
        self.title = ""
        self._hidden = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._hidden += 1
        self._in_title = tag == "title"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._hidden -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()
        elif not self._hidden and data.strip():
            self.words.append(" ".join(data.split()))


async def page(url: str, chars: int) -> str:
    """The page's visible text through ScraperAPI, rendered, as a person would read it."""
    key = os.environ.get("SCRAPERAPI_KEY", "")
    if not key:
        raise RuntimeError("SCRAPERAPI_KEY is not set")
    async with httpx.AsyncClient(timeout=70) as http:
        got = await http.get(SCRAPER, params={"api_key": key, "url": url, "render": "true"})
    if got.is_error:  # never raise httpx's own error: its message carries the request URL, and the key with it
        raise RuntimeError(f"ScraperAPI answered {got.status_code} for {url}")
    seen = _Text()
    seen.feed(got.text)
    return f"URL: {url}\nTitle: {seen.title}\nText: {' '.join(seen.words)[:chars]}"
