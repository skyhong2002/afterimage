"""Shared types for external image sources."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import httpx

USER_AGENT = "afterimage/1.0 (+https://afterimage.skyhong.tw)"

# Image downloads are only ever allowed from these hosts (suffix match), so the import
# endpoint cannot be turned into a way to fetch arbitrary URLs from inside the server.
ALLOWED_IMAGE_HOSTS = (
    "wallhaven.cc",          # w.wallhaven.cc, th.wallhaven.cc
    "steamstatic.com",       # shared.akamai.steamstatic.com, cdn.*.steamstatic.com
    "akamaihd.net",          # steamcdn-a.akamaihd.net
    "anilist.co",            # s4.anilist.co
    "tmdb.org",              # image.tmdb.org
    "donmai.us",             # cdn.donmai.us
)


def host_allowed(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_IMAGE_HOSTS)


class SourceError(Exception):
    """An external source failed; reported per source, never fails the whole search."""


@dataclass
class Found:
    source: str
    source_id: str
    source_url: str
    thumb_url: str
    preview_url: str
    full_url: str
    width: int | None = None
    height: int | None = None
    favorites: int | None = None
    artist: str | None = None
    label: str = ""
    importable: bool = True   # False: preview and link only, never stored (see tmdb.py)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict:
        return {**asdict(self), "id": self.id}


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def best_title_match(title: str, names: list[tuple[str, object]]):
    """Pick the entry whose name matches `title`: exact first, then whole-phrase containment."""
    want = norm(title)
    for name, payload in names:
        if norm(name) == want:
            return payload
    for name, payload in names:
        n = norm(name)
        if want and n and (f" {want} " in f" {n} " or f" {n} " in f" {want} ") and min(len(n), len(want)) >= 5:
            return payload
    return None


async def get_json(client: httpx.AsyncClient, url: str, **kw):
    try:
        r = await client.get(url, **kw)
    except httpx.HTTPError as e:
        raise SourceError(f"{type(e).__name__}: {e}") from e
    if r.status_code == 429:
        raise SourceError("rate limited")
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise SourceError("not JSON") from e
