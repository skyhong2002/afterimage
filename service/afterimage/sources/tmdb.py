"""TMDB: movie and TV backdrops. Needs a free API Read Access Token (TMDB_TOKEN)."""

from __future__ import annotations

import httpx

from .base import Found, SourceError, best_title_match, get_json

NAME = "tmdb"
API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "accept": "application/json"}


async def _find(client: httpx.AsyncClient, title: str, token: str) -> tuple[str, int] | None:
    doc = await get_json(client, f"{API}/search/multi", params={"query": title, "include_adult": "false"}, headers=_headers(token))
    names: list[tuple[str, tuple[str, int]]] = []
    for r in doc.get("results", []):
        if r.get("media_type") not in {"movie", "tv"}:
            continue
        for n in {r.get("title"), r.get("name"), r.get("original_title"), r.get("original_name")}:
            if n:
                names.append((n, (r["media_type"], int(r["id"]))))
    return best_title_match(title, names)


async def search(client: httpx.AsyncClient, title: str, *, limit: int = 12, tmdb_token: str = "", **_) -> list[Found]:
    if not tmdb_token:
        raise SourceError("TMDB_TOKEN is not set")
    hit = await _find(client, title, tmdb_token)
    if not hit:
        return []
    media, mid = hit
    page = f"https://www.themoviedb.org/{media}/{mid}"
    doc = await get_json(client, f"{API}/{media}/{mid}/images", params={"include_image_language": "null"}, headers=_headers(tmdb_token))
    backdrops = [b for b in doc.get("backdrops", []) if b.get("width", 0) >= 1920]
    backdrops.sort(key=lambda b: (-(b.get("vote_average") or 0), -b["width"]))
    return [
        Found(NAME, f"{mid}:{b['file_path'].strip('/').split('.')[0]}", page,
              f"{IMG}/w300{b['file_path']}", f"{IMG}/w780{b['file_path']}", f"{IMG}/original{b['file_path']}",
              width=b["width"], height=b["height"], favorites=round((b.get("vote_average") or 0) * 10) or None, label="backdrop")
        for b in backdrops[:limit]
    ]


async def resolve(client: httpx.AsyncClient, title: str, token: str) -> str | None:
    if not token:
        return None
    hit = await _find(client, title, token)
    return f"https://www.themoviedb.org/{hit[0]}/{hit[1]}" if hit else None
