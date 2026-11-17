"""Wallhaven: community wallpapers. Public API, SFW only, no key (45 requests/minute)."""

from __future__ import annotations

import asyncio
import time

import httpx

from .base import Found, SourceError, get_json

NAME = "wallhaven"
API = "https://wallhaven.cc/api/v1/search"
MIN_INTERVAL = 1.5

_lock = asyncio.Lock()
_last = 0.0


async def _polite() -> None:
    global _last
    async with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last = time.monotonic()


def usable_ratio(width: int, height: int, device: str) -> bool:
    """Wallhaven's "landscape" includes 4:1 banners; keep shapes that work as a wallpaper."""
    r = width / height
    return 1.3 <= r <= 2.4 if device == "desktop" else 0.4 <= r <= 0.77


async def search(client: httpx.AsyncClient, title: str, *, device: str = "desktop", limit: int = 12, **_) -> list[Found]:
    params = {
        "q": f'"{title}"' if " " in title.strip() else title,
        "categories": "110",  # general + anime, no "people"
        "purity": "100",      # SFW only
        "sorting": "favorites",
        "order": "desc",
        "ratios": "portrait" if device == "phone" else "landscape",
        "atleast": "1080x1920" if device == "phone" else "1920x1080",
    }
    await _polite()
    doc = await get_json(client, API, params=params)
    out: list[Found] = []
    for h in doc.get("data", []):
        try:
            w, ht = (int(x) for x in h["resolution"].split("x"))
        except (KeyError, ValueError) as e:
            raise SourceError("unexpected response shape") from e
        if not usable_ratio(w, ht, device):
            continue
        out.append(Found(
            source=NAME, source_id=h["id"], source_url=h["url"],
            thumb_url=h["thumbs"]["large"], preview_url=h["thumbs"].get("original") or h["thumbs"]["large"],
            full_url=h["path"], width=w, height=ht, favorites=h.get("favorites"), label="wallpaper",
        ))
        if len(out) >= limit:
            break
    return out
