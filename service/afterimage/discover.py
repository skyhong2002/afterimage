"""Discovery: federated search across sources, duplicate marking, import, gaps, batch runs."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Any

import httpx
from PIL import Image

from . import imaging, store
from .config import Settings
from .db import Database, now
from .sources import anilist, danbooru, steam, tmdb, wallhaven
from .sources.base import USER_AGENT, Found, SourceError, host_allowed

log = logging.getLogger("afterimage.discover")

ADAPTERS = {"wallhaven": wallhaven, "steam": steam, "danbooru": danbooru, "anilist": anilist, "tmdb": tmdb}
KIND_SOURCES = {
    "game": ["steam", "wallhaven", "danbooru"],
    "anime": ["wallhaven", "danbooru", "anilist"],
    "movie": ["tmdb", "wallhaven"],
    "show": ["tmdb", "wallhaven"],
    "book": ["wallhaven"],
    "music": ["wallhaven"],
}
MAX_IMPORT_BYTES = 30 * 1024 * 1024
MAX_THUMB_BYTES = 2 * 1024 * 1024
SOURCE_TIMEOUT = 25
FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


class ImportError_(Exception):
    """A user-facing import failure (bad image, too large, host not allowed...)."""


@dataclass
class State:
    settings: Settings
    db: Database
    http: httpx.AsyncClient
    hash_cache: dict[str, str | None] = field(default_factory=dict)


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"user-agent": USER_AGENT}, timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=False)


# ---- search --------------------------------------------------------------------

def sources_for(kind: str, requested: list[str] | None) -> list[str]:
    names = requested or KIND_SOURCES.get(kind, ["wallhaven"])
    return [n for n in names if n in ADAPTERS]


async def search_all(st: State, title: str, kind: str, sources: list[str] | None = None,
                     limit: int = 12, device: str = "desktop") -> dict[str, Any]:
    names = sources_for(kind, sources)

    async def one(name: str):
        try:
            found = await asyncio.wait_for(
                ADAPTERS[name].search(st.http, title, device=device, limit=limit, tmdb_token=st.settings.tmdb_token),
                SOURCE_TIMEOUT)
            return name, {"ok": True, "error": None, "items": found}
        except SourceError as e:
            return name, {"ok": False, "error": str(e), "items": []}
        except asyncio.TimeoutError:
            return name, {"ok": False, "error": "timed out", "items": []}
        except Exception:  # a bug in one adapter must not take the others down
            log.exception("source %s failed", name)
            return name, {"ok": False, "error": "internal error", "items": []}

    results = dict(await asyncio.gather(*(one(n) for n in names)))
    for name, r in results.items():
        r["items"] = await annotate(st, r["items"])
        r["count"] = len(r["items"])
    return {"title": title, "kind": kind, "sources": results}


def _local_hashes(db: Database) -> list[tuple[str, str]]:
    return [(r["id"], r["dhash"]) for r in db.all("SELECT id, dhash FROM images WHERE dhash IS NOT NULL")]


def nearest(hashes: list[tuple[str, str]], h: str | None) -> tuple[str, int] | None:
    if not h or not hashes:
        return None
    best = min(((i, imaging.distance(h, x)) for i, x in hashes), key=lambda t: t[1])
    return best


async def _thumb_hash(st: State, url: str) -> str | None:
    if url in st.hash_cache:
        return st.hash_cache[url]
    h: str | None = None
    if host_allowed(url):
        try:
            data = await fetch_bytes(st, url, MAX_THUMB_BYTES)
            h = await asyncio.to_thread(lambda: imaging.dhash(imaging.open_image(data)))
        except Exception as e:  # a missing thumbnail only means "cannot compare"
            log.info("no hash for %s: %s", url, e)
    if len(st.hash_cache) > 4000:
        st.hash_cache.clear()
    st.hash_cache[url] = h
    return h


async def annotate(st: State, items: list[Found]) -> list[dict]:
    """Mark each result: already imported, already in the collection, or a close lookalike."""
    hashes = await asyncio.to_thread(_local_hashes, st.db)
    sem = asyncio.Semaphore(6)

    async def one(f: Found) -> dict:
        d = f.to_dict()
        d["imported"] = None
        row = st.db.one("SELECT id FROM images WHERE source_ref=?", (f.id,))
        if row:
            d["imported"] = row["id"]
        cand = st.db.one("SELECT status FROM candidates WHERE id=?", (f.id,))
        d["decided"] = cand["status"] if cand and cand["status"] != "new" else None
        d["have"] = d["similar"] = None
        d["dhash"] = None
        if not d["imported"]:
            async with sem:
                h = await _thumb_hash(st, f.thumb_url)
            d["dhash"] = h
            n = nearest(hashes, h)
            if n and n[1] <= imaging.SAME_DISTANCE:
                d["have"] = {"id": n[0], "distance": n[1]}
            elif n and n[1] <= imaging.SIMILAR_DISTANCE:
                d["similar"] = {"id": n[0], "distance": n[1]}
        return d

    return list(await asyncio.gather(*(one(f) for f in items)))


# ---- downloading and importing ------------------------------------------------

async def fetch_bytes(st: State, url: str, max_bytes: int) -> bytes:
    """Download an image from an allowed host, following at most 3 redirects, each re-checked."""
    for _ in range(4):
        if not host_allowed(url):
            raise ImportError_(f"host not allowed: {url}")
        async with st.http.stream("GET", url) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                url = str(httpx.URL(url).join(r.headers.get("location", "")))
                continue
            if r.status_code != 200:
                raise ImportError_(f"download failed: HTTP {r.status_code}")
            if int(r.headers.get("content-length") or 0) > max_bytes:
                raise ImportError_(f"larger than {max_bytes // 1048576} MB")
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ImportError_(f"larger than {max_bytes // 1048576} MB")
            return bytes(buf)
    raise ImportError_("too many redirects")


def ingest(st: State, data: bytes, *, source: str, source_url: str | None, source_ref: str | None,
           artist: str | None, work_id: str | None, caption: str | None, force: bool) -> dict[str, Any]:
    """Store one image: files, derivatives, row. Synchronous; run it in a thread."""
    try:
        im = imaging.open_image(data)
    except Exception as e:
        raise ImportError_(f"not a usable image ({type(e).__name__})") from e
    ext = FORMATS.get(im.format or "")
    if not ext:
        raise ImportError_(f"unsupported format {im.format!r} (JPEG, PNG and WebP only)")
    image_id = hashlib.sha256(data).hexdigest()[:12]
    if st.db.one("SELECT 1 FROM images WHERE id=?", (image_id,)):
        return {"status": "exists", "id": image_id}

    info = imaging.analyse(im)
    near = nearest(_local_hashes(st.db), info["dhash"])
    if near and near[1] <= imaging.SAME_DISTANCE and not force:
        return {"status": "duplicate", "nearest": {"id": near[0], "distance": near[1]}}

    st.settings.ensure_dirs()
    original = f"{image_id}{ext}"
    (st.settings.originals_dir / original).write_bytes(data)
    imaging.write_derivatives(im, image_id, st.settings.thumbs_dir, st.settings.view_dir)

    stamp = now()
    with st.db.tx() as c:
        c.execute(
            "INSERT INTO images(id, original, caption, device, width, height, bytes, palette, dhash, reviewed, personal,"
            " source_url, source, source_ref, artist, origin, added_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,'imported',?,?)",
            (image_id, original, caption or (source_ref or image_id).replace(":", "-"), info["device"], info["width"], info["height"],
             len(data), json.dumps(info["palette"]), info["dhash"], source_url, source, source_ref, artist, stamp, stamp))
        if work_id:
            c.execute("INSERT OR IGNORE INTO image_works(image_id, work_id) VALUES (?,?)", (image_id, work_id))
    return {"status": "imported", "id": image_id, "width": info["width"], "height": info["height"], "device": info["device"]}


async def import_image(st: State, *, url: str, source: str, source_url: str | None = None, source_ref: str | None = None,
                       artist: str | None = None, work_id: str | None = None, work_title: str | None = None,
                       work_kind: str = "game", caption: str | None = None, force: bool = False) -> dict[str, Any]:
    if source == "tmdb" or "tmdb.org" in (urlparse(url).hostname or ""):
        raise ImportError_("TMDB images cannot be imported: the TMDB API terms only allow preview and linking")
    if work_id and not st.db.one("SELECT 1 FROM works WHERE id=?", (work_id,)):
        raise ImportError_(f"unknown work {work_id!r}")
    if not work_id and work_title:
        work_id = store.add_work(st.db, work_title, work_kind)["id"]
    data = await fetch_bytes(st, url, MAX_IMPORT_BYTES)
    return await asyncio.to_thread(
        ingest, st, data, source=source, source_url=source_url, source_ref=source_ref,
        artist=artist, work_id=work_id, caption=caption, force=force)


# ---- gaps and batch discovery ---------------------------------------------------

async def gaps(st: State, days: int = 60, limit: int = 100) -> dict[str, Any]:
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "get_wallpaper_gaps", "arguments": {"days": days, "limit": limit}}}
    try:
        r = await st.http.post(st.settings.infovore_mcp, json=rpc,
                               headers={"accept": "application/json, text/event-stream"}, timeout=40)
        r.raise_for_status()
        return r.json()["result"]["structuredContent"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise SourceError(f"infovore unavailable: {type(e).__name__}") from e


# ---- external links for a work ---------------------------------------------------

async def resolve_links(st: State, title: str, kind: str) -> dict[str, str]:
    links: dict[str, str] = {}
    try:
        if kind == "game":
            if (u := await steam.resolve(st.http, title)):
                links["steam"] = u
        elif kind in {"anime", "book"}:
            if (u := await anilist.resolve(st.http, title)):
                links["anilist"] = u
        elif kind in {"movie", "show"}:
            if (u := await tmdb.resolve(st.http, title, st.settings.tmdb_token)):
                links["tmdb"] = u
    except SourceError as e:
        log.info("link lookup failed for %s: %s", title, e)
    return links
