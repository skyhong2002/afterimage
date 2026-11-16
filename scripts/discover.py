"""Find new wallpaper candidates for what the owner recently played or watched.

1. Targets: works from infovore's `get_wallpaper_gaps` (recent games, anime, shows,
   movies that have no wallpaper yet), or titles given with --work.
2. Search Wallhaven (public API, SFW only, no key) for each target.
3. Drop anything already seen (accepted or rejected before) and anything that looks
   like an image already in the collection (CLIP cosine against catalog/embeddings.npz).
4. Write catalog/inbox/<year>-W<week>.json for a human to accept or reject.

Nothing is downloaded into the collection here; see scripts/accept_candidates.py.

Usage:
  uv run --group enrich scripts/discover.py                      # targets from infovore
  uv run --group enrich scripts/discover.py --work "Celeste" --work "Hades"
  uv run scripts/discover.py --no-clip --dry-run                 # no torch needed, prints only
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import yaml

import catalog_io

ROOT = catalog_io.ROOT
INBOX_DIR = ROOT / "catalog" / "inbox"
STATE = ROOT / "catalog" / "discover-state.yaml"
INFOVORE_MCP = "https://infovore.skyhong.tw/mcp"
WALLHAVEN = "https://wallhaven.cc/api/v1/search"
UA = "afterimage-discover/1.0 (+https://afterimage.skyhong.tw)"
MIN_INTERVAL = 1.6  # Wallhaven allows 45 requests per minute
_last_call = 0.0


def http_json(url: str, data: dict | None = None, headers: dict | None = None, timeout: int = 40):
    global _last_call
    if "wallhaven.cc" in url:  # be polite: stay well under the rate limit
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"user-agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---- targets --------------------------------------------------------------

def targets_from_infovore(days: int, limit: int) -> list[dict]:
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "get_wallpaper_gaps", "arguments": {"days": days, "limit": 100}}}
    out = http_json(INFOVORE_MCP, rpc, {"content-type": "application/json",
                                        "accept": "application/json, text/event-stream"})
    gaps = out["result"]["structuredContent"]["gaps"]
    return [
        {"title": g["title"], "kind": g["kind"], "reason": f"recent {g['kind']} ({g['source']}), no wallpaper yet"}
        for g in gaps
        if g["kind"] in {"game", "anime", "movie", "show"}
    ][:limit]


def too_generic(title: str, kind: str = "game") -> bool:
    """A single common word ("Escape", "Dark") matches unrelated art on Wallhaven. Game and
    anime names are usually distinctive even as one word ("Celeste", "Hades"); movie and show
    titles are not. Applies to automatic targets only; --work always searches."""
    words = title.replace(":", " ").split()
    if len(words) >= 2:
        return False
    return len(title) < (5 if kind in {"game", "anime"} else 10)


def query_variants(title: str) -> list[str]:
    """Full title first, then the part before a subtitle ("Final Fantasy XIV: Endwalker")."""
    plain = " ".join(title.replace(":", " ").replace("/", " ").split())
    variants = [plain]
    for sep in (":", " - ", " – "):
        if sep in title:
            head = title.split(sep)[0].strip()
            if len(head) >= 4 and head not in variants:
                variants.append(head)
    return variants


# ---- Wallhaven ------------------------------------------------------------

def search_wallhaven(query: str, device: str, min_res: str, per_page: int) -> list[dict]:
    params = {
        "q": f'"{query}"' if " " in query else query,
        "categories": "110",  # general + anime, no "people"
        "purity": "100",      # SFW only
        "sorting": "favorites",
        "order": "desc",
    }
    if device == "phone":
        params.update(ratios="portrait", atleast="1080x1920")
    else:
        params.update(ratios="landscape", atleast=min_res)
    try:
        doc = http_json(f"{WALLHAVEN}?{urllib.parse.urlencode(params)}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("  rate limited by Wallhaven, backing off 30s", file=sys.stderr)
            time.sleep(30)
            return []
        raise
    return doc.get("data", [])[:per_page]


def usable_ratio(hit: dict, device: str) -> bool:
    """Wallhaven's "landscape" includes 4:1 banners; only keep shapes that work as a wallpaper."""
    w, h = (int(x) for x in hit["resolution"].split("x"))
    r = w / h
    return 1.3 <= r <= 2.4 if device == "desktop" else 0.4 <= r <= 0.77


def to_candidate(hit: dict, target: dict, query: str) -> dict:
    w, h = (int(x) for x in hit["resolution"].split("x"))
    return {
        "id": f"wallhaven:{hit['id']}",
        "source": "wallhaven",
        "source_url": hit["url"],
        "thumb_url": hit["thumbs"]["large"],
        "preview_url": hit["thumbs"].get("original") or hit["thumbs"]["large"],
        "full_url": hit["path"],
        "width": w,
        "height": h,
        "favorites": hit.get("favorites", 0),
        "file_size": hit.get("file_size", 0),
        "work_query": target["title"],
        "work_kind": target.get("kind", "game"),
        "matched_query": query,
        "reason": target["reason"],
        "status": "new",
    }


# ---- duplicate check against the collection ------------------------------

class Dedupe:
    def __init__(self) -> None:
        import numpy as np
        import open_clip
        import torch
        from enrich import EMBEDDINGS, MODEL, MODEL_TAG, PRETRAINED, square_crops

        data = np.load(EMBEDDINGS, allow_pickle=False)
        if str(data["model"]) != MODEL_TAG:
            raise SystemExit(f"embeddings were made with {data['model']}, expected {MODEL_TAG}; run enrich.py")
        self.np, self.torch, self.square_crops = np, torch, square_crops
        self.ids = [str(i) for i in data["ids"]]
        v = data["vectors"].astype("float32")
        self.vectors = v / np.linalg.norm(v, axis=1, keepdims=True)
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
        self.model.eval()

    def embed(self, image_bytes: bytes):
        from PIL import Image

        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        batch = self.torch.stack([self.preprocess(c) for c in self.square_crops(im)])
        with self.torch.no_grad():
            f = self.model.encode_image(batch)
            f = f / f.norm(dim=-1, keepdim=True)
            v = f.mean(dim=0)
            return (v / v.norm()).numpy().astype("float32")

    def nearest(self, vec) -> tuple[str, float]:
        sims = self.vectors @ vec
        i = int(sims.argmax())
        return self.ids[i], float(sims[i])


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"user-agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read()


# ---- state ------------------------------------------------------------------

def load_state() -> dict:
    if STATE.exists():
        return yaml.safe_load(STATE.read_text(encoding="utf-8")) or {}
    return {}


def seen_ids(state: dict) -> set[str]:
    return set(state.get("accepted", [])) | set(state.get("rejected", []))


def inbox_path(today: date) -> Path:
    y, w, _ = today.isocalendar()
    return INBOX_DIR / f"{y}-W{w:02d}.json"


def known_candidate_ids() -> set[str]:
    ids: set[str] = set()
    for f in INBOX_DIR.glob("*.json"):
        ids |= {c["id"] for c in json.loads(f.read_text(encoding="utf-8")).get("candidates", [])}
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", action="append", help="look for this title (repeatable); default: gaps from infovore")
    ap.add_argument("--days", type=int, default=60, help="infovore window for recent activity")
    ap.add_argument("--max-works", type=int, default=6)
    ap.add_argument("--per-work", type=int, default=6, help="candidates kept per work")
    ap.add_argument("--device", choices=["desktop", "phone"], default="desktop")
    ap.add_argument("--min-res", default="1920x1080")
    # Calibrated 2026-11-17 on 12 collection images, each shrunk to 640px / cropped 4% / brightened:
    # altered copies scored 0.888-0.99 (never matched anything else), while different images of the
    # same work topped out at 0.85. Candidates between 0.80 and the threshold are kept, with
    # nearest_id and similarity in the inbox, so a person can judge the borderline ones.
    ap.add_argument("--dup-threshold", type=float, default=0.87)
    ap.add_argument("--no-clip", action="store_true", help="skip the visual duplicate check")
    ap.add_argument("--dry-run", action="store_true", help="print what would be written")
    args = ap.parse_args()

    if args.work:
        targets = [{"title": t, "kind": "game", "reason": "requested by hand"} for t in args.work]
    else:
        print("asking infovore for wallpaper gaps...")
        everything = targets_from_infovore(args.days, 100)
        generic = [t["title"] for t in everything if too_generic(t["title"], t["kind"])]
        if generic:
            print(f"skipping too-generic titles (use --work to search them anyway): {generic}")
        targets = [t for t in everything if not too_generic(t["title"], t["kind"])][: args.max_works]
    if not targets:
        print("nothing to look for")
        return 0
    print(f"{len(targets)} targets: {[t['title'] for t in targets]}")

    state = load_state()
    skip = seen_ids(state) | known_candidate_ids()
    dedupe = None if args.no_clip else Dedupe()

    found: list[dict] = []
    batch_ids: set[str] = set()
    for target in targets:
        hits: list[dict] = []
        used = ""
        for q in query_variants(target["title"]):
            hits = search_wallhaven(q, args.device, args.min_res, per_page=24)
            used = q
            if len(hits) >= 3:
                break
        fresh = [h for h in hits if usable_ratio(h, args.device) and f"wallhaven:{h['id']}" not in skip | batch_ids]
        kept = 0
        for hit in fresh:
            if kept >= args.per_work:
                break
            cand = to_candidate(hit, target, used)
            if dedupe:
                try:
                    vec = dedupe.embed(fetch_bytes(hit["thumbs"]["large"]))
                except Exception as e:  # a broken thumbnail must not stop the run
                    print(f"  skip {hit['id']}: {e}", file=sys.stderr)
                    continue
                nearest, sim = dedupe.nearest(vec)
                cand["nearest_id"], cand["similarity"] = nearest, round(sim, 4)
                if sim >= args.dup_threshold:
                    print(f"  duplicate of {nearest} ({sim:.3f}): {cand['source_url']}")
                    continue
            found.append(cand)
            batch_ids.add(cand["id"])
            kept += 1
        print(f"{target['title']!r}: {len(hits)} hits via {used!r}, {kept} kept")

    if args.dry_run:
        for c in found:
            sim = f" sim={c['similarity']:.2f}" if "similarity" in c else ""
            print(f"  {c['id']:20} {c['width']}x{c['height']} fav={c['favorites']:<4}{sim}  {c['work_query']}  {c['source_url']}")
        print(f"dry run: {len(found)} candidates, nothing written")
        return 0

    path = inbox_path(date.today())
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "candidates": []}
    have = {c["id"] for c in existing["candidates"]}
    existing["candidates"] += [c for c in found if c["id"] not in have]
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(found)} new candidates -> {path.relative_to(ROOT)} ({len(existing['candidates'])} in file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
