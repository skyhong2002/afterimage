"""The discovery feed: pictures the owner may want, across every work in their history.

1. targets   every game, movie, show, anime, manga and book in infovore's persisted history
             (not a recent window), minus ones that are only queued ("planned", "plan to watch").
2. scanner   a background task that searches the importable sources for each target, slowly,
             best-first, and stores what it finds as candidates. It resumes after a restart and
             rescans every few weeks.
3. feed      candidates from all works mixed into one ranked list, so the owner scrolls through
             pictures instead of opening works one by one.

Only importable sources are scanned (Steam, Wallhaven, Danbooru). TMDB is preview-only by its
terms and never enters the feed.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import imaging
from .db import Database, now
from .discover import State, search_all
from .sources.base import SourceError, norm

log = logging.getLogger("afterimage.feed")

KINDS = ("game", "movie", "show", "anime", "manga", "book")
QUEUED = {"planned", "plantowatch", "to-read"}           # wanted, not seen yet
KIND_WEIGHT = {"game": 1.0, "anime": 1.0, "movie": 0.85, "show": 0.85, "manga": 0.6, "book": 0.4}
SOURCES_BY_KIND = {
    "game": ["steam", "wallhaven", "danbooru"],
    "anime": ["wallhaven", "danbooru"],
    "movie": ["wallhaven"], "show": ["wallhaven"], "manga": ["wallhaven"], "book": ["wallhaven"],
}
CANDIDATE_KIND = {"manga": "book"}                         # works.kind has no "manga"
PER_SOURCE = 6
SCAN_LIMIT = 8                                              # results asked of each source
EDITION_WORDS = re.compile(
    r"\b(remastered|remaster|remake|definitive edition|director'?s cut|complete edition|deluxe edition|goty|"
    r"game of the year( edition)?|enhanced edition|royal|ultimate edition|anniversary( edition)?|hd)\b", re.I)


# ---- the owner's history ---------------------------------------------------------------------

def infovore_base(mcp_url: str) -> str:
    p = urlsplit(mcp_url)
    return f"{p.scheme}://{p.netloc}"


async def fetch_history(st: State, page_size: int = 500, max_pages: int = 40) -> list[dict]:
    """Every persisted activity of the media kinds, from infovore's public JSON API."""
    base = infovore_base(st.settings.infovore_mcp)
    rows: list[dict] = []
    try:
        for kind in KINDS:
            for page in range(max_pages):
                r = await st.http.get(f"{base}/api/activities.json", params={"kind": kind, "limit": page_size, "offset": page * page_size}, timeout=60)
                r.raise_for_status()
                data = r.json().get("data", [])
                rows += data
                if len(data) < page_size:
                    break
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise SourceError(f"infovore unavailable: {type(e).__name__}") from e
    return rows


def build_targets(rows: list[dict]) -> list[dict]:
    """One target per work. A work counts as seen unless every entry for it is queued."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        kind, title = r.get("mediaKind"), (r.get("title") or "").strip()
        if kind in KINDS and title:
            groups.setdefault(f"{kind}:{norm(title)}", []).append(r)
    out = []
    for key, entries in groups.items():
        seen = [e for e in entries if (e.get("status") or "") not in QUEUED]
        if not seen:
            continue
        dated = sorted((e for e in seen if (e.get("occurredAt") or "")[:4].isdigit()), key=lambda e: e["occurredAt"])
        ratings = [e["rating"]["value"] / e["rating"]["scale"] for e in seen if e.get("rating") and e["rating"].get("scale")]
        newest = dated[-1] if dated else seen[0]
        out.append({"key": key, "title": newest["title"].strip(), "kind": newest["mediaKind"],
                    "last_at": dated[-1]["occurredAt"] if dated else None,
                    "rating": max(ratings) if ratings else None, "plays": len(seen)})
    return out


def priority(t: dict, today: datetime | None = None) -> float:
    """Which works to scan first (and favour in the feed): well rated, recent, played a lot."""
    today = today or datetime.now(timezone.utc)
    days = 1500.0
    if t.get("last_at"):
        try:
            days = max(0.0, (today - datetime.fromisoformat(t["last_at"].replace("Z", "+00:00"))).days)
        except ValueError:
            pass
    rating = t["rating"] if t.get("rating") is not None else 0.55
    return round(KIND_WEIGHT.get(t["kind"], 0.5) * (0.4 * rating + 0.5 * math.exp(-days / 900) + 0.1 * min(t.get("plays", 1) / 5, 1)), 4)


def sync_targets(db: Database, targets: list[dict], today: datetime | None = None) -> int:
    stamp = now()
    with db.tx() as c:
        for t in targets:
            c.execute(
                "INSERT INTO targets(key, title, kind, last_at, rating, plays, priority, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET title=excluded.title, last_at=excluded.last_at, rating=excluded.rating,"
                " plays=excluded.plays, priority=excluded.priority, updated_at=excluded.updated_at",
                (t["key"], t["title"], t["kind"], t["last_at"], t["rating"], t["plays"], priority(t, today), stamp))
    return len(targets)


# ---- scanning one work ---------------------------------------------------------------------------

def too_generic(title: str, kind: str) -> bool:
    """A single common word ("Escape", "Dark") matches unrelated art on image sites. A distinctive
    game or anime name is fine as one word; movie, show and book titles are not."""
    words = title.replace(":", " ").split()
    if len(words) >= 2:
        return False
    return len(title) < (5 if kind in {"game", "anime"} else 10)


def query_variants(title: str) -> list[str]:
    """The title as written, then without a subtitle, then without edition words."""
    plain = " ".join(re.sub(r"[:/\-–]", " ", title).split())
    head = re.split(r"\s*[:\-–]\s+|\s*\(", title)[0].strip()
    stripped = " ".join(EDITION_WORDS.sub(" ", head).split())
    out = [plain]
    for v in (head, stripped):
        v = " ".join(re.sub(r"[:/\-–]", " ", v).split())
        if len(v) >= 4 and v not in out:
            out.append(v)
    return out


async def scan_target(st: State, t: dict) -> dict[str, Any]:
    """Search the importable sources for one work and store the usable results as candidates."""
    kind = t["kind"]
    sources = SOURCES_BY_KIND.get(kind, ["wallhaven"])
    kept: dict[str, dict] = {}
    ok_any = False
    status: dict[str, str] = {}
    count: dict[str, int] = {s: 0 for s in sources}
    for variant in query_variants(t["title"]):
        asking = [s for s in sources if count[s] < 2]      # a source that already gave results is not asked again
        if not asking:
            break
        res = await search_all(st, variant, CANDIDATE_KIND.get(kind, kind), asking, limit=SCAN_LIMIT, device="desktop")
        for name, r in res["sources"].items():
            ok_any |= r["ok"]
            status[name] = "ok" if r["ok"] else r["error"]
            for it in r["items"]:
                if it["importable"] and not (it["imported"] or it["decided"] or it["have"] or it["similar"]):
                    if it["id"] not in kept:
                        kept[it["id"]] = it
                        count[name] += 1
    if not ok_any:
        raise SourceError("every source failed: " + "; ".join(f"{k}: {v}" for k, v in status.items()))

    per_source: dict[str, int] = {}
    inserted = 0
    stamp = now()
    with st.db.tx() as c:
        for it in sorted(kept.values(), key=lambda i: -(i["favorites"] or 0)):
            if per_source.get(it["source"], 0) >= PER_SOURCE:
                continue
            n = c.execute(
                "INSERT OR IGNORE INTO candidates(id, source, source_url, thumb_url, preview_url, full_url, width, height, favorites,"
                " artist, dhash, work_query, work_kind, reason, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (it["id"], it["source"], it["source_url"], it["thumb_url"], it["preview_url"], it["full_url"], it["width"], it["height"],
                 it["favorites"], it["artist"], it["dhash"], t["title"], CANDIDATE_KIND.get(kind, kind), it["label"], stamp)).rowcount
            if n:
                inserted += 1
                per_source[it["source"]] = per_source.get(it["source"], 0) + 1
    return {"found": inserted, "sources": status}


def next_target(db: Database, rescan_days: int = 21):
    row = db.one("SELECT * FROM targets WHERE scanned_at IS NULL ORDER BY priority DESC, key LIMIT 1")
    if row:
        return row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=rescan_days)).isoformat(timespec="seconds")
    return db.one("SELECT * FROM targets WHERE scanned_at < ? ORDER BY scanned_at LIMIT 1", (cutoff,))


# ---- the background scanner -----------------------------------------------------------------------

class Scanner:
    def __init__(self, st: State, *, pause: float = 3.0, rescan_days: int = 21, refresh_hours: float = 12,
                 idle: float = 900, backoff: float = 300) -> None:
        self.st, self.pause, self.rescan_days, self.refresh_hours = st, pause, rescan_days, refresh_hours
        self.idle, self.backoff = idle, backoff
        self.current: str | None = None
        self.last_refresh: str | None = None
        self.last_error: str | None = None
        self.seconds_per_target: float | None = None
        self.max_fails = 3
        self._fails: dict[str, int] = {}
        self._refreshed_at = 0.0
        self._kick = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def kick(self, refresh: bool = False) -> None:
        """Wake the scanner now; refresh=True also re-reads the history from infovore."""
        if refresh:
            self._refreshed_at = 0.0
        self._kick.set()

    async def refresh(self) -> None:
        targets = build_targets(await fetch_history(self.st))
        sync_targets(self.st.db, targets)
        self._refreshed_at = asyncio.get_running_loop().time()
        self.last_refresh = now()
        log.info("history refreshed: %d works", len(targets))

    async def step(self) -> bool:
        """One unit of work. Returns False when there is nothing to do."""
        loop = asyncio.get_running_loop()
        if self._refreshed_at == 0.0 or loop.time() - self._refreshed_at > self.refresh_hours * 3600:
            try:
                await self.refresh()
                self.last_error = None
            except SourceError as e:
                self.last_error = str(e)
                if not self.st.db.one("SELECT 1 FROM targets LIMIT 1"):
                    return False
                self._refreshed_at = loop.time()   # keep working from what we have; try again later
        row = next_target(self.st.db, self.rescan_days)
        if not row:
            return False
        t = dict(row)
        if too_generic(t["title"], t["kind"]):
            res: dict[str, Any] = {"found": 0, "sources": {}, "note": "too generic to search"}
        else:
            self.current = t["title"]
            started = loop.time()
            try:
                res = await scan_target(self.st, t)
            except SourceError as e:
                self._fails[t["key"]] = self._fails.get(t["key"], 0) + 1
                if self._fails[t["key"]] < self.max_fails:
                    raise                                   # retried after a pause
                res = {"found": 0, "sources": {}, "note": f"gave up after {self.max_fails} failures: {e}"}
            self._fails.pop(t["key"], None)
            took = loop.time() - started
            self.seconds_per_target = took if self.seconds_per_target is None else 0.8 * self.seconds_per_target + 0.2 * took
        note = res.get("note") or ", ".join(f"{k}:{v}" for k, v in res["sources"].items())
        with self.st.db.tx() as c:
            c.execute("UPDATE targets SET scanned_at=?, found=found+?, note=? WHERE key=?", (now(), res["found"], note[:300], t["key"]))
        self.current = None
        return True

    async def run(self) -> None:
        while True:
            try:
                busy = await self.step()
                delay = self.pause if busy else self.idle
            except asyncio.CancelledError:
                raise
            except SourceError as e:           # every source failed: network trouble, try the same work again later
                self.last_error, delay = str(e), self.backoff
                self.current = None
            except Exception:
                log.exception("scanner step failed")
                self.last_error, delay = "internal error", self.backoff
                self.current = None
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()

    def progress(self) -> dict[str, Any]:
        db = self.st.db
        total = db.one("SELECT COUNT(*) n FROM targets")["n"]
        scanned = db.one("SELECT COUNT(*) n FROM targets WHERE scanned_at IS NOT NULL")["n"]
        remaining = db.one("SELECT COUNT(*) n FROM targets WHERE scanned_at IS NULL")["n"]
        by_kind = {r["kind"]: r["n"] for r in db.all("SELECT work_kind AS kind, COUNT(*) n FROM candidates WHERE status='new' GROUP BY work_kind")}
        return {
            "running": self._task is not None and not self._task.done(),
            "works_total": total, "works_scanned": scanned,
            "candidates": db.one("SELECT COUNT(*) n FROM candidates WHERE status='new'")["n"],
            "candidates_by_kind": by_kind, "current": self.current,
            "last_refresh": self.last_refresh, "error": self.last_error,
            "eta_minutes": round(remaining * self.seconds_per_target / 60) if self.seconds_per_target and remaining else None,
        }


# ---- the feed -----------------------------------------------------------------------------------------

_DUP_CACHE: dict[Any, Any] = {}

_norm = lru_cache(maxsize=20000)(norm)


def phrase_match(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 5 and f" {short} " in f" {long} "


def coverage(db: Database) -> list[tuple[str, int]]:
    """(work title, how many images of it the collection has)"""
    return [(r["title"], r["n"]) for r in db.all(
        "SELECT w.title, COUNT(iw.image_id) n FROM works w LEFT JOIN image_works iw ON iw.work_id=w.id GROUP BY w.id")]


def feed(db: Database, *, limit: int = 36, offset: int = 0, kind: str | None = None, source: str | None = None,
         device: str | None = None, work: str | None = None, uncovered: bool = False) -> dict[str, Any]:
    """Candidates from every work, best first, but never one work in a row.

    Score = how much the owner likely wants this work (rating, recency, play count, fewer wallpapers
    already) and how good the picture looks (popularity within its source, resolution), minus a
    penalty for each earlier picture from the same work."""
    rows = [dict(r) for r in db.all("SELECT * FROM candidates WHERE status='new'")]
    local = [r["dhash"] for r in db.all("SELECT dhash FROM images WHERE dhash IS NOT NULL")]
    targets = {r["title"]: dict(r) for r in db.all("SELECT * FROM targets")}
    owned = coverage(db)

    # a candidate whose picture is now in the collection (imported meanwhile) drops out
    sig = (len(local), hash(tuple(sorted(local))))
    if _DUP_CACHE.get("sig") != sig:
        _DUP_CACHE.clear()
        _DUP_CACHE["sig"] = sig

    def dup(h: str | None) -> bool:
        if not h:
            return False
        if h not in _DUP_CACHE:
            _DUP_CACHE[h] = any((d := imaging.distance(h, x)) is not None and d <= imaging.SIMILAR_DISTANCE for x in local)
        return _DUP_CACHE[h]

    # popularity as a rank within its source, so Wallhaven and Danbooru favourites can be compared
    by_source: dict[str, list[float]] = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(math.log1p(r["favorites"] or 0))
    for v in by_source.values():
        v.sort()

    def popularity(r: dict) -> float:
        v = by_source[r["source"]]
        if not r["favorites"] or len(v) < 2:
            return 0.35
        return sum(1 for x in v if x <= math.log1p(r["favorites"])) / len(v)

    cover_of = {q: sum(n for title, n in owned if phrase_match(title, q)) for q in {r["work_query"] for r in rows}}
    scored = []
    for r in rows:
        if dup(r["dhash"]):
            continue
        t = targets.get(r["work_query"])
        cover = cover_of[r["work_query"]]
        w = (t["priority"] if t else 0.3) / (1 + 0.25 * cover)
        dev = "desktop" if (r["width"] or 1) >= (r["height"] or 0) else "phone"
        if (kind and r["work_kind"] != kind) or (source and r["source"] != source) or (device and dev != device) \
                or (work and r["work_query"] != work) or (uncovered and cover):
            continue
        q = popularity(r) + (0.1 if (r["width"] or 0) >= 3840 else 0)
        scored.append({**r, "cover": cover, "device": dev, "_w": w, "_q": q})

    by_work: dict[str, list[dict]] = {}
    for r in scored:
        by_work.setdefault(r["work_query"], []).append(r)
    for items in by_work.values():
        items.sort(key=lambda r: -r["_q"])
        for rank, r in enumerate(items):
            r["score"] = round(0.6 * r["_w"] + 0.4 * r["_q"] - 0.3 * rank, 4)
    ordered = sorted(scored, key=lambda r: (-r["score"], r["id"]))
    page = ordered[offset: offset + limit]
    return {
        "total": len(ordered), "offset": offset,
        "items": [{
            "id": r["id"], "source": r["source"], "source_url": r["source_url"], "thumb_url": r["thumb_url"],
            "preview_url": r["preview_url"] or r["thumb_url"], "width": r["width"], "height": r["height"],
            "favorites": r["favorites"], "artist": r["artist"], "label": r["reason"], "device": r["device"],
            "work": r["work_query"], "kind": r["work_kind"], "have_wallpapers": r["cover"], "score": r["score"],
        } for r in page],
    }
