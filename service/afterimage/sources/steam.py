"""Steam store: official screenshots and the store background. Public, no key.

This is the source for games that wallpaper sites do not cover (indie and new releases).
"""

from __future__ import annotations

import httpx

from .base import Found, SourceError, best_title_match, get_json

NAME = "steam"
SEARCH = "https://store.steampowered.com/api/storesearch/"
DETAILS = "https://store.steampowered.com/api/appdetails"


async def find_app(client: httpx.AsyncClient, title: str) -> tuple[int, str] | None:
    doc = await get_json(client, SEARCH, params={"term": title, "cc": "us", "l": "en"})
    items = [(i["name"], (int(i["id"]), i["name"])) for i in doc.get("items", []) if i.get("type", "app") == "app"]
    return best_title_match(title, items)


async def search(client: httpx.AsyncClient, title: str, *, limit: int = 12, **_) -> list[Found]:
    app = await find_app(client, title)
    if not app:
        return []
    appid, name = app
    doc = await get_json(client, DETAILS, params={"appids": appid, "l": "en"})
    entry = doc.get(str(appid)) or {}
    if not entry.get("success"):
        raise SourceError("no store details")
    data = entry["data"]
    page = f"https://store.steampowered.com/app/{appid}"
    out: list[Found] = []
    if data.get("background_raw"):
        bg = data["background_raw"].split("?")[0]
        out.append(Found(NAME, f"{appid}:background", page, bg, bg, bg, label="store background"))
    for s in data.get("screenshots", []):
        out.append(Found(NAME, f"{appid}:{s['id']}", page, s["path_thumbnail"], s["path_thumbnail"], s["path_full"],
                         label=f"{name} screenshot"))
    return out[:limit]


async def resolve(client: httpx.AsyncClient, title: str) -> str | None:
    app = await find_app(client, title)
    return f"https://store.steampowered.com/app/{app[0]}" if app else None
