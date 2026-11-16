"""Apply accept / reject decisions on discovery candidates.

Decision file (exported by the /discover page, or written by hand):
  {"version": 1, "decisions": {"wallhaven:abc123": "accept", "wallhaven:def456": "reject"}}

accept  downloads the original into wallpaper/ (or "phone wallpaper/" for portrait),
        adds it to the catalog with its source link and work (the work is created if
        it is new), then refreshes palette/similar and the site data.
reject  is remembered in catalog/discover-state.yaml so it never comes back.

Run it under the enrich group so the refresh step can use CLIP:
  uv run --group enrich scripts/accept_candidates.py decisions.json [--dry-run]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml
from PIL import Image

import catalog_io
import discover

ROOT = catalog_io.ROOT
WORKS = ROOT / "catalog" / "works.yaml"
MAX_BYTES = 30 * 1024 * 1024
KINDS = {"game", "anime", "movie", "show", "book", "music"}


def slug(title: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "work"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"user-agent": discover.UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"larger than {MAX_BYTES // 1048576} MB")
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    doc = json.loads(Path(args.file).read_text(encoding="utf-8"))
    decisions: dict[str, str] = doc.get("decisions", {})
    bad = {k: v for k, v in decisions.items() if v not in {"accept", "reject"}}
    if doc.get("version") != 1 or bad:
        print(f"bad decision file (version {doc.get('version')!r}, bad values {bad})", file=sys.stderr)
        return 1

    inboxes = {p: json.loads(p.read_text(encoding="utf-8")) for p in sorted(discover.INBOX_DIR.glob("*.json"))}
    by_id = {c["id"]: (p, c) for p, d in inboxes.items() for c in d["candidates"]}
    unknown = [i for i in decisions if i not in by_id]
    if unknown:
        print(f"not in any inbox file: {unknown}", file=sys.stderr)
        return 1

    accepts = [i for i, v in decisions.items() if v == "accept"]
    rejects = [i for i, v in decisions.items() if v == "reject"]
    print(f"{len(accepts)} to accept, {len(rejects)} to reject")
    if args.dry_run:
        for i in accepts:
            c = by_id[i][1]
            print(f"  accept {i} -> {c['work_query']} ({c['width']}x{c['height']})  {c['source_url']}")
        return 0

    works = yaml.safe_load(WORKS.read_text(encoding="utf-8"))
    works_text = WORKS.read_text(encoding="utf-8")
    state = discover.load_state()
    state.setdefault("accepted", [])
    state.setdefault("rejected", [])
    added: dict[str, dict] = {}  # saved path -> metadata to attach after build_catalog
    failed: list[str] = []

    for cid in accepts:
        _, c = by_id[cid]
        try:
            data = download(c["full_url"])
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
                im.verify()
        except Exception as e:
            print(f"  FAILED {cid}: {e}", file=sys.stderr)
            failed.append(cid)
            continue
        ext = Path(c["full_url"]).suffix.lower() or ".jpg"
        folder = "wallpaper" if w >= h else "phone wallpaper"
        dest = ROOT / folder / f"wallhaven-{cid.split(':', 1)[1]}{ext}"
        dest.write_bytes(data)

        title = c["work_query"]
        work = next((x for x in works if norm(x["title"]) == norm(title)), None)
        if not work:
            kind = c.get("work_kind") if c.get("work_kind") in KINDS else "game"
            work = {"id": slug(title, {x["id"] for x in works}), "title": title, "kind": kind}
            works.append(work)
            print(f"  new work: {work['title']} ({work['kind']})")
        added[dest.relative_to(ROOT).as_posix()] = {"source": c["source_url"], "works": [work["id"]]}
        print(f"  saved {dest.relative_to(ROOT)} ({w}x{h}, {len(data) // 1024} KB) for {work['title']}")

    ok_accepts = [i for i in accepts if i not in failed]
    for cid in ok_accepts:
        if cid not in state["accepted"]:
            state["accepted"].append(cid)
    for cid in rejects:
        if cid not in state["rejected"]:
            state["rejected"].append(cid)
    for cid in ok_accepts + rejects:
        p, c = by_id[cid]
        c["status"] = "accepted" if cid in ok_accepts else "rejected"
    for p, d in inboxes.items():
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    discover.STATE.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")

    if added:
        header = "".join(l + "\n" for l in works_text.splitlines() if l.startswith("#"))
        WORKS.write_text(header + yaml.safe_dump(sorted(works, key=lambda w: (w["kind"], w["title"].lower())),
                                                 allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
        # register the new files, then attach source and work to their entries
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_catalog.py")], check=True)
        entries = catalog_io.load()
        for e in entries:
            meta = added.get(e["path"])
            if meta:
                e["source"] = meta["source"]
                e["works"] = meta["works"]
                e["reviewed"] = False
        catalog_io.save(entries)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "enrich.py")], check=True)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_site_data.py")], check=True)

    print(f"done: {len(ok_accepts)} accepted, {len(rejects)} rejected, {len(failed)} failed {failed or ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
