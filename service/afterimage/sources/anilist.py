"""AniList: cover art and banners for anime and manga. Public GraphQL, no key.

Mostly useful for the work's page and ids; the images are covers, not wallpapers.
"""

from __future__ import annotations

import httpx

from .base import Found, SourceError, best_title_match

NAME = "anilist"
API = "https://graphql.anilist.co"
QUERY = """
query ($search: String) {
  Page(perPage: 6) {
    media(search: $search, sort: SEARCH_MATCH) {
      id idMal siteUrl type
      title { romaji english native }
      bannerImage
      coverImage { extraLarge large }
    }
  }
}
"""


async def _lookup(client: httpx.AsyncClient, title: str) -> dict | None:
    try:
        r = await client.post(API, json={"query": QUERY, "variables": {"search": title}})
    except httpx.HTTPError as e:
        raise SourceError(f"{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code}")
    media = r.json().get("data", {}).get("Page", {}).get("media", [])
    names = [(n, m) for m in media for n in (m["title"].get("english"), m["title"].get("romaji"), m["title"].get("native")) if n]
    return best_title_match(title, names)


async def search(client: httpx.AsyncClient, title: str, *, limit: int = 12, **_) -> list[Found]:
    m = await _lookup(client, title)
    if not m:
        return []
    out: list[Found] = []
    cover = (m.get("coverImage") or {}).get("extraLarge")
    if cover:
        out.append(Found(NAME, f"{m['id']}:cover", m["siteUrl"], cover, cover, cover, label="cover"))
    if m.get("bannerImage"):
        out.append(Found(NAME, f"{m['id']}:banner", m["siteUrl"], m["bannerImage"], m["bannerImage"], m["bannerImage"], label="banner"))
    return out[:limit]


async def resolve(client: httpx.AsyncClient, title: str) -> str | None:
    m = await _lookup(client, title)
    return m["siteUrl"] if m else None
