"""Who a person is and where they work, from Apollo: by their email, or by their company's website."""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agent.visitors import Company

URL = "https://api.apollo.io/api/v1"


class _Org(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None
    primary_domain: str | None = None
    industry: str | None = None
    estimated_num_employees: int | None = None
    annual_revenue_printed: str | None = None
    city: str | None = None
    short_description: str | None = None
    keywords: list[str] = []


class _Person(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    seniority: str | None = None
    linkedin_url: str | None = None
    organization: _Org | None = None


class _PersonMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    person: _Person | None = None


class _OrgMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    organization: _Org | None = Field(default=None)


def _company(person: _Person | None, org: _Org | None) -> Company | None:
    if person is None and org is None:
        return None
    o = org or _Org()
    return Company(
        person_title=(person.title if person else None) or "",
        seniority=(person.seniority if person else None) or "",
        linkedin=(person.linkedin_url if person else None) or "",
        company=o.name or "",
        domain=o.primary_domain or "",
        industry=o.industry or "",
        employees=o.estimated_num_employees,
        revenue=o.annual_revenue_printed or "",
        city=o.city or "",
        description=o.short_description or "",
        keywords=o.keywords[:15],
    )


async def enrich(email: str, name: str, website: str) -> Company | None:
    """The person by their email; failing that, their company by its website. None when Apollo knows neither."""
    key = os.environ.get("APOLLO_API_KEY", "")
    if not key:
        raise RuntimeError("APOLLO_API_KEY is not set")
    headers = {"X-Api-Key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as http:
        found = await http.post(f"{URL}/people/match", json={"email": email, "name": name or None})
        found.raise_for_status()
        person = _PersonMatch.model_validate(found.json()).person
        if person is not None and person.organization is not None:
            return _company(person, person.organization)
        domain = website.removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
        if not domain and "@" in email:
            domain = email.split("@", 1)[1]
        if not domain:
            return _company(person, None)
        org = await http.get(f"{URL}/organizations/enrich", params={"domain": domain})
        org.raise_for_status()
        return _company(person, _OrgMatch.model_validate(org.json()).organization)
