"""TMDB: movie and TV backdrops. Needs a free API Read Access Token (TMDB_TOKEN).

TMDB's API terms limit caching to 6 months, forbid using the content with AI/ML applications and
require attribution. So results are preview-and-link only: they are never imported into the
collection (enforced in discover.import_image) and the pages that show them carry the attribution.
"""

from __future__ import annotations

import httpx

from .base import Found, SourceError, best_title_match, get_json, norm

NAME = "tmdb"
API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "accept": "application/json"}


async def _matches(client: httpx.AsyncClient, title: str, token: str, limit: int = 3) -> list[tuple[str, int, str, str]]:
    """Every movie/show whose title is exactly `title` (up to `limit`), as (kind, id, name, year).
    Same-name works are common ("Hello Ghost" is three different films), so all of them are returned."""
    doc = await get_json(client, f"{API}/search/multi", params={"query": title, "include_adult": "false"}, headers=_headers(token))
    want = norm(title)
    out: list[tuple[str, int, str, str]] = []
    phrase: list[tuple[str, tuple[str, int, str, str]]] = []
    for r in doc.get("results", []):
        if r.get("media_type") not in {"movie", "tv"}:
            continue
        names = [n for n in (r.get("title"), r.get("name"), r.get("original_title"), r.get("original_name")) if n]
        entry = (r["media_type"], int(r["id"]), names[0] if names else title, (r.get("release_date") or r.get("first_air_date") or "")[:4])
        if any(norm(n) == want for n in names):
            out.append(entry)
        phrase += [(n, entry) for n in names]
    if not out:
        fallback = best_title_match(title, phrase)
        if fallback:
            out.append(fallback)
    return out[:limit]


async def search(client: httpx.AsyncClient, title: str, *, limit: int = 12, tmdb_token: str = "", **_) -> list[Found]:
    if not tmdb_token:
        raise SourceError("TMDB_TOKEN is not set")
    found: list[Found] = []
    for media, mid, name, year in await _matches(client, title, tmdb_token):
        page = f"https://www.themoviedb.org/{media}/{mid}"
        doc = await get_json(client, f"{API}/{media}/{mid}/images", headers=_headers(tmdb_token))
        wide = [b for b in doc.get("backdrops", []) if b.get("width", 0) >= 1920]
        clean = [b for b in wide if b.get("iso_639_1") is None]
        pool, note = (clean, "backdrop") if clean else (wide, "backdrop, has text")
        pool.sort(key=lambda b: (-(b.get("vote_average") or 0), -b["width"]))
        tag = f"{name} ({year})" if year else name
        found += [
            Found(NAME, f"{mid}:{b['file_path'].strip('/').split('.')[0]}", page,
                  f"{IMG}/w300{b['file_path']}", f"{IMG}/w780{b['file_path']}", f"{IMG}/original{b['file_path']}",
                  width=b["width"], height=b["height"], favorites=round((b.get("vote_average") or 0) * 10) or None,
                  label=f"{tag} {note}", importable=False)
            for b in pool[:limit]
        ]
    return found[:limit]


async def resolve(client: httpx.AsyncClient, title: str, token: str) -> str | None:
    if not token:
        return None
    hits = await _matches(client, title, token, limit=1)
    return f"https://www.themoviedb.org/{hits[0][0]}/{hits[0][1]}" if hits else None
