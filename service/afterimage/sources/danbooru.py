"""Danbooru: anime illustration with artist credit. Public API, general-rated posts only.

These are fan works; every import keeps the artist and the post link.
"""

from __future__ import annotations

import re

import httpx

from .base import Found, get_json

NAME = "danbooru"
BASE = "https://danbooru.donmai.us"
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")


def tag_guess(title: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", re.sub(r"[\s:/-]+", "_", title.lower())).strip("_")


async def find_tag(client: httpx.AsyncClient, title: str) -> str | None:
    guess = tag_guess(title)
    if not guess:
        return None
    tags = await get_json(client, f"{BASE}/tags.json", params={
        "search[name_matches]": f"{guess}*", "search[order]": "count", "search[hide_empty]": "true", "limit": 8})
    if not isinstance(tags, list) or not tags:
        return None
    for t in tags:                                   # exact copyright tag (category 3) wins
        if t["name"] == guess and t.get("category") == 3:
            return t["name"]
    for t in tags:                                   # then any exact name
        if t["name"] == guess:
            return t["name"]
    return tags[0]["name"] if tags[0].get("category") == 3 else None


async def search(client: httpx.AsyncClient, title: str, *, device: str = "desktop", limit: int = 12, **_) -> list[Found]:
    tag = await find_tag(client, title)
    if not tag:
        return []
    posts = await get_json(client, f"{BASE}/posts.json", params={"tags": f"{tag} rating:g", "limit": 60})
    out: list[Found] = []
    for p in sorted((p for p in posts if isinstance(p, dict)), key=lambda p: -(p.get("fav_count") or 0)):
        full = p.get("file_url") or ""
        if not full.lower().endswith(IMAGE_EXT):
            continue
        w, h = p.get("image_width") or 0, p.get("image_height") or 0
        if not w or not h or min(w, h) < 900:
            continue
        if (device == "phone") != (h > w):
            continue
        thumb = p.get("preview_file_url") or p.get("large_file_url") or full
        out.append(Found(
            source=NAME, source_id=str(p["id"]), source_url=f"{BASE}/posts/{p['id']}",
            thumb_url=thumb, preview_url=p.get("large_file_url") or thumb, full_url=full,
            width=w, height=h, favorites=p.get("fav_count"),
            artist=(p.get("tag_string_artist") or "").replace(" ", ", ") or None, label=f"#{tag}",
        ))
        if len(out) >= limit:
            break
    return out
