"""Catalog queries and edits. Pure functions over Database; no HTTP in here."""

from __future__ import annotations

import json
from typing import Any

from .db import Database, now

EMOTIONS = {"joy", "sadness", "anger", "fear", "disgust", "anxiety", "envy", "ennui", "embarrassment"}
KINDS = {"game", "anime", "movie", "show", "book", "music"}


def _image_rows(db: Database, include_personal: bool):
    where = "" if include_personal else "WHERE personal=0"
    return db.all(f"SELECT * FROM images {where} ORDER BY lower(caption), id")


def _maps(db: Database):
    works_of: dict[str, list[str]] = {}
    for r in db.all("SELECT image_id, work_id FROM image_works ORDER BY position, work_id"):
        works_of.setdefault(r["image_id"], []).append(r["work_id"])
    similar_of: dict[str, list[str]] = {}
    for r in db.all("SELECT image_id, other_id FROM similar ORDER BY image_id, rank"):
        similar_of.setdefault(r["image_id"], []).append(r["other_id"])
    return works_of, similar_of


def catalog(db: Database, include_personal: bool) -> dict[str, Any]:
    """The payload the gallery loads: every image plus the works that have a use."""
    works_of, similar_of = _maps(db)
    rows = _image_rows(db, include_personal)
    visible = {r["id"] for r in rows}
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "caption": r["caption"],
            "device": r["device"],
            "w": r["width"],
            "h": r["height"],
            "mb": round(r["bytes"] / 1048576, 1),
            "palette": json.loads(r["palette"]),
            "similar": [i for i in similar_of.get(r["id"], []) if i in visible],
            "works": works_of.get(r["id"], []),
            "emotion": r["emotion"] or "",
            "reviewed": bool(r["reviewed"]),
            "personal": bool(r["personal"]),
            "confidence": r["confidence"] or "",
            "note": r["note"] or "",
            "source": r["source"] or "",
            "sourceUrl": r["source_url"] or "",
            "artist": r["artist"] or "",
            "origin": r["origin"],
            "src": f"/view/{r['id']}.avif",
            "raw": f"/original/{r['id']}",
        })
    counts: dict[str, int] = {}
    for it in items:
        for w in it["works"]:
            counts[w] = counts.get(w, 0) + 1
    works = [
        {"id": w["id"], "title": w["title"], "kind": w["kind"], "links": json.loads(w["links"]), "count": counts.get(w["id"], 0)}
        for w in db.all("SELECT * FROM works ORDER BY lower(title)")
    ]
    return {"items": items, "works": works}


def public_wallpapers(db: Database) -> dict[str, Any]:
    """The compact contract infovore reads (kept identical to the old static file)."""
    works_of, _ = _maps(db)
    rows = _image_rows(db, include_personal=False)
    used: dict[str, int] = {}
    wallpapers = []
    for r in rows:
        ws = works_of.get(r["id"], [])
        for w in ws:
            used[w] = used.get(w, 0) + 1
        wallpapers.append({
            "id": r["id"], "caption": r["caption"], "device": r["device"],
            "width": r["width"], "height": r["height"], "works": ws,
            "emotion": r["emotion"], "palette": json.loads(r["palette"]), "reviewed": bool(r["reviewed"]),
        })
    works = [
        {"id": w["id"], "title": w["title"], "kind": w["kind"], "count": used[w["id"]]}
        for w in db.all("SELECT * FROM works ORDER BY id") if w["id"] in used
    ]
    return {"version": 1, "works": works, "wallpapers": wallpapers}


class EditError(ValueError):
    pass


def update_image(db: Database, image_id: str, changes: dict[str, Any], actor: str) -> dict[str, Any]:
    """Apply a human edit. Any edit counts as a review unless `reviewed` says otherwise."""
    row = db.one("SELECT * FROM images WHERE id=?", (image_id,))
    if not row:
        raise EditError("no such image")
    if not changes:
        raise EditError("nothing to change")
    unknown = set(changes) - {"works", "emotion", "personal", "reviewed", "caption"}
    if unknown:
        raise EditError(f"cannot edit: {sorted(unknown)}")
    if "emotion" in changes and changes["emotion"] not in EMOTIONS | {None, ""}:
        raise EditError(f"unknown emotion {changes['emotion']!r}")
    if "works" in changes:
        ws = changes["works"]
        if not isinstance(ws, list) or not all(isinstance(w, str) for w in ws):
            raise EditError("works must be a list of work ids")
        missing = [w for w in ws if not db.one("SELECT 1 FROM works WHERE id=?", (w,))]
        if missing:
            raise EditError(f"unknown works: {missing}")
    if "caption" in changes and not str(changes["caption"]).strip():
        raise EditError("caption cannot be empty")

    before = _editable(db, image_id)
    with db.tx() as c:
        if "works" in changes:
            c.execute("DELETE FROM image_works WHERE image_id=?", (image_id,))
            c.executemany("INSERT INTO image_works(image_id, work_id, position) VALUES (?,?,?)",
                          [(image_id, w, i) for i, w in enumerate(dict.fromkeys(changes["works"]))])
        if "emotion" in changes:
            c.execute("UPDATE images SET emotion=? WHERE id=?", (changes["emotion"] or None, image_id))
        if "personal" in changes:
            c.execute("UPDATE images SET personal=? WHERE id=?", (int(bool(changes["personal"])), image_id))
        if "caption" in changes:
            c.execute("UPDATE images SET caption=? WHERE id=?", (str(changes["caption"]).strip(), image_id))
        reviewed = changes.get("reviewed", True)
        c.execute("UPDATE images SET reviewed=?, updated_at=? WHERE id=?", (int(bool(reviewed)), now(), image_id))
        if reviewed:
            c.execute("UPDATE images SET confidence=NULL, note=NULL WHERE id=?", (image_id,))
        after = _editable(db, image_id)
        c.execute("INSERT INTO edits(image_id, at, actor, before, after) VALUES (?,?,?,?,?)",
                  (image_id, now(), actor, json.dumps(before), json.dumps(after)))
    return after


def _editable(db: Database, image_id: str) -> dict[str, Any]:
    # Safe inside db.tx(): the connection is shared and the lock is re-entrant.
    row = db.one("SELECT * FROM images WHERE id=?", (image_id,))
    ws = [r["work_id"] for r in db.all("SELECT work_id FROM image_works WHERE image_id=? ORDER BY position, work_id", (image_id,))]
    return {"works": ws, "emotion": row["emotion"] or "", "personal": bool(row["personal"]),
            "reviewed": bool(row["reviewed"]), "caption": row["caption"]}


def add_work(db: Database, title: str, kind: str, links: dict[str, Any] | None = None) -> dict[str, Any]:
    title = title.strip()
    if not title:
        raise EditError("title is empty")
    if kind not in KINDS:
        raise EditError(f"kind must be one of {sorted(KINDS)}")
    existing = db.find_work_by_title(title)
    if existing:
        return {"id": existing["id"], "title": existing["title"], "kind": existing["kind"], "created": False}
    wid = db.add_work(title, kind, links)
    return {"id": wid, "title": title, "kind": kind, "created": True}
