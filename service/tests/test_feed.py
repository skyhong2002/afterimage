import asyncio
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest

from afterimage import backup, feed, imaging
from afterimage.db import Database, now
from afterimage.sources.base import SourceError
from conftest import add_image, encode, picture
from test_discover import WH_FULL, WH_THUMB, Web, fast_wallhaven, web  # noqa: F401  (fixtures)

TODAY = datetime(2026, 11, 17, tzinfo=timezone.utc)


def act(kind, title, status=None, at="2025-06-01T00:00:00.000Z", rating=None, source="backloggd"):
    return {"mediaKind": kind, "title": title, "status": status, "occurredAt": at, "rating": rating, "source": source}


def run(coro):
    return asyncio.run(coro)


# ---- targets from the history ------------------------------------------------------------------------

def test_targets_are_one_per_work_and_skip_what_was_only_queued():
    rows = [
        act("game", "Celeste"), act("game", "Celeste!", at="2026-01-02T00:00:00.000Z", rating={"value": 4, "scale": 5}),
        act("movie", "Barbie", rating={"value": 8, "scale": 10}, source="simkl"),
        act("show", "Dark Matter", status="plantowatch", source="simkl"),                    # never seen
        act("manga", "The Drifting Classroom", status="planned", source="kitsu"),
        act("manga", "The Drifting Classroom", status="current", source="kitsu"),             # seen once
        act("anime", "Ajin", at="Sep 29", source="kitsu"),                                    # a label, not a date
        act("music", "Some Song"), act("game", "   "),
    ]
    by_key = {t["key"]: t for t in feed.build_targets(rows)}
    assert sorted(by_key) == ["anime:ajin", "game:celeste", "manga:the drifting classroom", "movie:barbie"]
    celeste = by_key["game:celeste"]
    assert celeste["plays"] == 2 and celeste["rating"] == 0.8 and celeste["last_at"] == "2026-01-02T00:00:00.000Z"
    assert by_key["movie:barbie"]["rating"] == 0.8
    assert by_key["anime:ajin"]["last_at"] is None
    assert by_key["manga:the drifting classroom"]["plays"] == 1, "the queued entry is not counted"


def test_priority_prefers_well_rated_recent_and_often_played_works():
    hi = {"kind": "game", "rating": 0.9, "last_at": "2026-10-01T00:00:00Z", "plays": 4}
    lo = {"kind": "game", "rating": 0.2, "last_at": "2021-01-01T00:00:00Z", "plays": 1}
    assert feed.priority(hi, TODAY) > feed.priority(lo, TODAY)
    assert feed.priority({**hi, "kind": "book"}, TODAY) < feed.priority(hi, TODAY), "books rarely have wallpapers"
    assert feed.priority({**hi, "rating": None}, TODAY) < feed.priority(hi, TODAY)
    assert feed.priority({**hi, "last_at": None}, TODAY) < feed.priority(hi, TODAY), "an undated entry counts as old"


def test_syncing_targets_updates_them_without_losing_what_was_already_scanned(db):
    t = {"key": "game:celeste", "title": "Celeste", "kind": "game", "last_at": None, "rating": 0.5, "plays": 1}
    feed.sync_targets(db, [t], TODAY)
    with db.tx() as c:
        c.execute("UPDATE targets SET scanned_at='2026-11-01T00:00:00+00:00', found=4 WHERE key='game:celeste'")
    feed.sync_targets(db, [{**t, "rating": 1.0, "plays": 9}], TODAY)
    r = db.one("SELECT * FROM targets")
    assert (r["rating"], r["plays"], r["scanned_at"], r["found"]) == (1.0, 9, "2026-11-01T00:00:00+00:00", 4)


def test_query_variants_drop_subtitles_and_edition_words():
    assert feed.query_variants("Persona 5 Royal") == ["Persona 5 Royal", "Persona 5"]
    assert feed.query_variants("Final Fantasy XIV: Endwalker") == ["Final Fantasy XIV Endwalker", "Final Fantasy XIV"]
    assert feed.query_variants("Fate/Hollow Ataraxia Remastered") == ["Fate Hollow Ataraxia Remastered", "Fate Hollow Ataraxia"]
    assert feed.query_variants("Hades") == ["Hades"]


def test_history_is_read_page_by_page_for_every_kind(owner, web):
    web.activities = [act("game", f"G{i}") for i in range(5)] + [act("movie", "M1"), act("movie", "M2"), act("anime", "A1")]
    rows = run(feed.fetch_history(owner.app.state.discover, page_size=2))
    assert len(rows) == 8, "three game pages (2+2+1) and the other kinds"
    web.fail = {"infovore"}
    with pytest.raises(SourceError, match="infovore"):
        run(feed.fetch_history(owner.app.state.discover))


# ---- scanning one work -----------------------------------------------------------------------------------

GAME = {"key": "game:celeste", "title": "Celeste", "kind": "game"}


def candidates(db):
    return {r["id"]: dict(r) for r in db.all("SELECT * FROM candidates")}


def test_a_scan_stores_the_usable_pictures_of_a_game(owner, web, db):
    have = add_image(owner, "mine", seed=11, works=["celeste"])
    web.images[WH_THUMB] = encode(have.resize((200, 125)), "JPEG", quality=60)                 # already in the collection
    web.images["https://th.wallhaven.cc/lg/aa/bbb222.jpg"] = encode(picture(77), "JPEG")        # new
    res = run(feed.scan_target(owner.app.state.discover, GAME))
    got = candidates(db)
    assert set(got) == {"steam:123:background", "steam:123:0", "wallhaven:bbb222", "danbooru:7"}, "the banner, the copy of 'mine' and TMDB are absent"
    assert res["found"] == 4 and all(c["status"] == "new" and c["work_query"] == "Celeste" and c["work_kind"] == "game" for c in got.values())
    assert got["wallhaven:bbb222"]["dhash"] is not None, "the hash is kept so the feed can re-check it later"
    assert got["danbooru:7"]["artist"] == "some_artist"


def test_a_second_scan_adds_nothing_and_decided_pictures_do_not_come_back(owner, web, db):
    run(feed.scan_target(owner.app.state.discover, GAME))
    with db.tx() as c:
        c.execute("UPDATE candidates SET status='rejected' WHERE id='wallhaven:bbb222'")
    again = run(feed.scan_target(owner.app.state.discover, GAME))
    assert again["found"] == 0
    assert db.one("SELECT status FROM candidates WHERE id='wallhaven:bbb222'")["status"] == "rejected"


def test_movies_are_searched_on_wallhaven_only_and_never_on_tmdb(owner, web, db):
    run(feed.scan_target(owner.app.state.discover, {"key": "movie:inception", "title": "Inception", "kind": "movie"}))
    assert {c["source"] for c in candidates(db).values()} == {"wallhaven"}
    assert not any("themoviedb" in c or "tmdb.org" in c for c in web.calls)


def test_a_shorter_title_is_tried_when_the_full_one_finds_nothing(owner, web, db):
    web.wh_empty = {"Persona 5 Royal"}
    run(feed.scan_target(owner.app.state.discover, {"key": "game:persona 5 royal", "title": "Persona 5 Royal", "kind": "game"}))
    assert web.wh_queries == ["Persona 5 Royal", "Persona 5"]
    assert any(c["source"] == "wallhaven" for c in candidates(db).values())


def test_a_source_that_already_answered_is_not_asked_again_for_the_next_title(owner, web):
    web.wh_empty = {"Persona 5 Royal"}
    run(feed.scan_target(owner.app.state.discover, {"key": "k", "title": "Persona 5 Royal", "kind": "game"}))
    steam_searches = [c for c in web.calls if "storesearch" in c]
    assert len(steam_searches) == 2, "steam found nothing for the first title (no match), so it is asked once more"
    web.calls.clear(); web.wh_empty = set()
    # "Celeste Remastered" has two variants; steam answers the first, so it is not asked for the second
    run(feed.scan_target(owner.app.state.discover, {"key": "k2", "title": "Celeste Remastered", "kind": "game"}))
    assert len([c for c in web.calls if "storesearch" in c]) == 1, "steam answered, so one search is enough"
    assert web.wh_queries.count("Celeste") == 0 and web.wh_queries.count("Celeste Remastered") == 1, "wallhaven answered too"


def test_a_scan_where_every_source_fails_raises_and_stores_nothing(owner, web, db):
    web.fail = {"steampowered", "wallhaven", "donmai"}
    with pytest.raises(SourceError, match="every source failed"):
        run(feed.scan_target(owner.app.state.discover, GAME))
    assert candidates(db) == {}


# ---- the background scanner ------------------------------------------------------------------------------------

def scanner(owner, **kw):
    return feed.Scanner(owner.app.state.discover, pause=0, **kw)


def test_the_scanner_reads_the_history_and_scans_the_most_wanted_work_first(owner, web, db):
    web.activities = [act("game", "Old Thing Here", at="2021-02-01T00:00:00.000Z", rating={"value": 2, "scale": 10}),
                      act("game", "Celeste", at="2026-11-01T00:00:00.000Z", rating={"value": 9, "scale": 10}),
                      act("movie", "Barbie", at="2023-08-05T00:00:00.000Z")]
    sc = scanner(owner)

    async def go():
        order = []
        assert await sc.step() is True
        order.append([r["title"] for r in db.all("SELECT title FROM targets WHERE scanned_at IS NOT NULL")])
        assert await sc.step() is True and await sc.step() is True
        return order, await sc.step()
    order, more = run(go())
    assert order == [["Celeste"]], "the well rated, recent work is scanned first"
    assert more is False, "nothing left to do once every work is scanned"
    assert db.one("SELECT COUNT(*) n FROM targets WHERE scanned_at IS NOT NULL")["n"] == 3
    celeste = db.one("SELECT * FROM targets WHERE title='Celeste'")
    assert celeste["found"] == 5 and "steam:ok" in celeste["note"]
    p = sc.progress()
    assert (p["works_total"], p["works_scanned"], p["candidates"]) == (3, 3, db.one("SELECT COUNT(*) n FROM candidates")["n"])
    assert p["eta_minutes"] is None and p["last_refresh"] is not None


def test_a_work_that_keeps_failing_is_given_up_on_so_it_cannot_block_the_queue(owner, web, db):
    web.activities = [act("game", "Celeste", rating={"value": 9, "scale": 10}), act("game", "Hades", rating={"value": 3, "scale": 10})]
    sc = scanner(owner)
    sc.max_fails = 3
    web.fail = {"steampowered", "wallhaven", "donmai"}

    async def go():
        raised = 0
        for _ in range(2):
            try:
                await sc.step()
            except SourceError:
                raised += 1
        gave_up = await sc.step()          # third failure for the same work
        return raised, gave_up
    raised, gave_up = run(go())
    assert raised == 2 and gave_up is True
    r = db.one("SELECT * FROM targets WHERE title='Celeste'")
    assert r["scanned_at"] and r["found"] == 0 and "gave up after 3 failures" in r["note"]
    assert db.one("SELECT scanned_at FROM targets WHERE title='Hades'")["scanned_at"] is None, "the next work is next in line"


def test_a_generic_title_is_marked_scanned_without_searching(owner, web, db):
    web.activities = [act("game", "Dark")]
    run(scanner(owner).step())
    assert db.one("SELECT note FROM targets")["note"] == "too generic to search"
    assert not any("wallhaven" in c or "steampowered" in c for c in web.calls)


def test_when_infovore_is_down_the_scanner_keeps_working_from_the_works_it_knows(owner, web, db):
    web.activities = [act("game", "Celeste"), act("game", "Hades")]
    sc = scanner(owner)
    run(sc.step())
    web.fail = {"infovore"}
    sc.kick(refresh=True)

    async def go():
        return await sc.step()
    assert run(go()) is True, "it carries on with the next unscanned work"
    assert sc.last_error and "infovore" in sc.last_error
    assert db.one("SELECT COUNT(*) n FROM targets WHERE scanned_at IS NOT NULL")["n"] == 2


def test_without_any_known_works_and_no_infovore_there_is_nothing_to_do(owner, web):
    web.fail = {"infovore"}
    sc = scanner(owner)
    assert run(sc.step()) is False and "infovore" in sc.last_error


def test_works_are_rescanned_after_the_interval_but_not_before(db):
    feed.sync_targets(db, [{"key": "game:a", "title": "A", "kind": "game", "last_at": None, "rating": None, "plays": 1}], TODAY)
    with db.tx() as c:
        c.execute("UPDATE targets SET scanned_at=?", (now(),))
    assert feed.next_target(db, rescan_days=21) is None
    with db.tx() as c:
        c.execute("UPDATE targets SET scanned_at='2020-01-01T00:00:00+00:00'")
    assert feed.next_target(db, rescan_days=21)["title"] == "A"


# ---- the feed -------------------------------------------------------------------------------------------------------

def target(db, title, kind="game", priority=0.5):
    with db.tx() as c:
        c.execute("INSERT INTO targets(key, title, kind, priority, updated_at) VALUES (?,?,?,?,?)", (f"{kind}:{title.lower()}", title, kind, priority, now()))


_n = [0]


def cand(db, work, favorites, *, source="wallhaven", kind="game", w=1920, h=1080, dhash=None, status="new", cid=None):
    _n[0] += 1
    cid = cid or f"{source}:{_n[0]}"
    with db.tx() as c:
        c.execute("INSERT INTO candidates(id, source, source_url, thumb_url, preview_url, full_url, width, height, favorites, dhash,"
                  " work_query, work_kind, reason, status, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (cid, source, f"https://x/{cid}", f"https://t/{cid}", f"https://p/{cid}", f"https://f/{cid}", w, h, favorites, dhash,
                   work, kind, "label", status, now()))
    return cid


def works_of(res):
    return [i["work"] for i in res["items"]]


def test_the_feed_mixes_works_so_it_never_reads_work_after_work(db):
    for t, p in (("A", 0.9), ("B", 0.8), ("C", 0.7)):
        target(db, t, priority=p)
        for f in (100, 90, 80, 70, 60):
            cand(db, t, f)
    order = works_of(feed.feed(db, limit=15))
    assert order[:3] == ["A", "B", "C"], "the best of each work comes first"
    assert all(a != b for a, b in zip(order[:9], order[1:10])), "no work twice in a row early on"
    assert order.count("A") == order.count("B") == order.count("C") == 5


def test_a_more_wanted_work_comes_first_and_owning_many_of_it_lowers_it(owner, db):
    for i in range(6):
        add_image(owner, f"h{i}", works=["hades"], seed=100 + i)
    target(db, "Hades", priority=0.8)
    target(db, "Celeste", priority=0.8)
    cand(db, "Hades", 100)
    cand(db, "Celeste", 100)
    res = feed.feed(db)
    assert works_of(res) == ["Celeste", "Hades"] and res["items"][1]["have_wallpapers"] == 6
    assert works_of(feed.feed(db, uncovered=True)) == ["Celeste"]


def test_filters_by_kind_source_device_and_work(db):
    target(db, "G"); target(db, "M", "movie")
    cand(db, "G", 5, source="steam"); cand(db, "G", 5, source="danbooru", w=1000, h=1600); cand(db, "M", 5, kind="movie")
    assert {i["work"] for i in feed.feed(db, kind="movie")["items"]} == {"M"}
    assert {i["source"] for i in feed.feed(db, source="steam")["items"]} == {"steam"}
    phone = feed.feed(db, device="phone")["items"]
    assert [i["source"] for i in phone] == ["danbooru"] and phone[0]["device"] == "phone"
    assert {i["work"] for i in feed.feed(db, work="G")["items"]} == {"G"}


def test_pages_do_not_overlap_and_together_cover_everything(db):
    target(db, "A"); target(db, "B")
    ids = {cand(db, w, f) for w in ("A", "B") for f in range(1, 13)}
    seen, offset = [], 0
    while True:
        page = feed.feed(db, limit=10, offset=offset)
        seen += [i["id"] for i in page["items"]]
        offset += 10
        if offset >= page["total"]:
            break
    assert len(seen) == len(set(seen)) == 24 and set(seen) == ids and page["total"] == 24


def test_a_picture_that_has_since_entered_the_collection_leaves_the_feed(owner, db):
    mine = add_image(owner, "mine", seed=21)
    target(db, "A")
    dup = cand(db, "A", 50, dhash=imaging.dhash(mine))
    other = cand(db, "A", 40, dhash=imaging.dhash(picture(999)))
    nohash = cand(db, "A", 30)
    assert {i["id"] for i in feed.feed(db)["items"]} == {other, nohash}
    assert dup not in {i["id"] for i in feed.feed(db)["items"]}


def test_decided_candidates_are_not_in_the_feed(db):
    target(db, "A")
    keep = cand(db, "A", 5)
    cand(db, "A", 5, status="rejected"); cand(db, "A", 5, status="accepted")
    assert [i["id"] for i in feed.feed(db)["items"]] == [keep]


def test_popularity_is_compared_within_each_source(db):
    target(db, "A")
    for f in (1, 2, 3, 4):
        cand(db, "A", f, source="wallhaven")
    for f in (5000, 6000, 7000, 8000):
        cand(db, "A", f, source="danbooru")
    top_two = {i["favorites"] for i in feed.feed(db)["items"][:2]}
    assert top_two == {4, 8000}, "the best of each source competes on equal terms; raw counts would put 8000 and 7000 first"


# ---- the endpoints ------------------------------------------------------------------------------------------------------

def test_feed_progress_and_scan_need_a_login(owner):
    owner.post("/api/logout")
    for method, path in (("GET", "/api/discover/feed"), ("GET", "/api/discover/progress"), ("POST", "/api/discover/run"),
                         ("POST", "/api/discover/feed/wallhaven:x/accept"), ("POST", "/api/discover/feed/wallhaven:x/reject")):
        assert owner.request(method, path, json={} if method == "POST" else None).status_code == 401, path


def test_the_feed_endpoint_returns_pictures_and_progress_reports_the_scan(owner, db):
    target(db, "Celeste")
    cid = cand(db, "Celeste", 10, source="steam", cid="steam:123:0")
    body = owner.get("/api/discover/feed?limit=5").json()
    assert body["total"] == 1 and body["items"][0]["id"] == cid
    for key in ("source", "source_url", "thumb_url", "preview_url", "width", "favorites", "work", "kind", "have_wallpapers", "label"):
        assert key in body["items"][0], key
    assert body["items"][0]["full_url"] == f"https://f/{cid}", "the big viewer swaps the small preview for the real picture"
    p = owner.get("/api/discover/progress").json()
    assert p["running"] is False, "the scanner is off in tests"
    assert (p["works_total"], p["works_scanned"], p["candidates"]) == (1, 0, 1)
    assert owner.post("/api/discover/run?refresh=true").json()["works_total"] == 1
    assert owner.get("/api/discover/feed?device=tablet").status_code == 422


def test_rejecting_many_touches_only_the_listed_undecided_pictures(owner, db):
    target(db, "Celeste")
    seen = [cand(db, "Celeste", 10 + i) for i in range(3)]
    later = cand(db, "Celeste", 1)                      # found by the scanner, not on the owner's screen
    taken = cand(db, "Celeste", 50, status="accepted")
    r = owner.post("/api/discover/reject", json={"ids": seen + [taken, "steam:nope", seen[0]]})
    assert r.status_code == 200 and r.json() == {"rejected": 3}, "accepted, unknown and repeated ids do not count"
    status = {c["id"]: c["status"] for c in db.all("SELECT id, status FROM candidates")}
    assert [status[i] for i in seen] == ["rejected"] * 3
    assert status[later] == "new" and status[taken] == "accepted"
    assert db.one("SELECT decided_at FROM candidates WHERE id=?", (seen[0],))["decided_at"]
    assert [i["id"] for i in owner.get("/api/discover/feed").json()["items"]] == [later]


@pytest.mark.parametrize("body", [{}, {"ids": []}, {"ids": ["a"] * 501}, {"ids": ["a"], "all": True}, {"ids": "a"}])
def test_rejecting_many_refuses_an_empty_oversized_or_open_ended_request(owner, db, body):
    cid = cand(db, "Celeste", 10)
    assert owner.post("/api/discover/reject", json=body).status_code == 422
    assert db.one("SELECT status FROM candidates WHERE id=?", (cid,))["status"] == "new"


def test_rejecting_many_needs_a_login(client, db):
    target(db, "Celeste")
    cid = cand(db, "Celeste", 10)
    assert client.post("/api/discover/reject", json={"ids": [cid]}).status_code == 401
    assert db.one("SELECT status FROM candidates WHERE id=?", (cid,))["status"] == "new"


def test_accepting_from_the_feed_imports_the_picture_and_rejecting_is_remembered(owner, web, db):
    web.images[WH_FULL] = encode(picture(5, (1920, 1080)), "JPEG", quality=85)
    target(db, "Celeste")
    a = cand(db, "Celeste", 10, cid="wallhaven:aaa111")
    b = cand(db, "Celeste", 9, cid="wallhaven:zzz")
    with db.tx() as c:
        c.execute("UPDATE candidates SET full_url=? WHERE id=?", (WH_FULL, a))
    ok = owner.post(f"/api/discover/feed/{a}/accept", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "imported"
    assert db.one("SELECT status FROM candidates WHERE id=?", (a,))["status"] == "accepted"
    assert db.one("SELECT source_ref FROM images WHERE id=?", (ok.json()["id"],))["source_ref"] == a
    assert owner.post(f"/api/discover/feed/{b}/reject").json() == {"status": "rejected"}
    assert owner.get("/api/discover/feed").json()["total"] == 0
    assert owner.post("/api/discover/feed/wallhaven:nope/reject").status_code == 404


# ---- storage -----------------------------------------------------------------------------------------------------------------

def test_a_database_from_before_the_feed_gets_the_new_column_and_keeps_its_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY, source TEXT NOT NULL, source_url TEXT, thumb_url TEXT, preview_url TEXT,"
                " full_url TEXT NOT NULL, width INTEGER, height INTEGER, favorites INTEGER, artist TEXT, work_query TEXT NOT NULL,"
                " work_kind TEXT NOT NULL, reason TEXT, nearest_id TEXT, distance INTEGER, status TEXT NOT NULL DEFAULT 'new',"
                " found_at TEXT NOT NULL, decided_at TEXT)")
    con.execute("INSERT INTO candidates(id, source, full_url, work_query, work_kind, found_at) VALUES ('wallhaven:old','wallhaven','https://x','Old','game','2026-01-01')")
    con.commit(); con.close()
    db = Database(path)
    assert "dhash" in {r["name"] for r in db.all("PRAGMA table_info(candidates)")}
    assert db.one("SELECT id, dhash FROM candidates")["id"] == "wallhaven:old"
    Database(path).close()      # opening it again is harmless
    assert db.all("SELECT name FROM sqlite_master WHERE name='targets'")


def test_the_backup_keeps_decisions_but_not_the_candidate_pool(owner, db, tmp_path):
    target(db, "A")
    cand(db, "A", 1, cid="wallhaven:new1")
    cand(db, "A", 1, cid="wallhaven:rej1", status="rejected")
    cand(db, "A", 1, cid="wallhaven:acc1", status="accepted")
    snap = backup.snapshot(owner.app.state.settings.data_dir / "afterimage.sqlite")
    assert [c["id"] for c in snap["candidates"]] == ["wallhaven:acc1", "wallhaven:rej1"], "undecided ones are found again by the scan"


# ---- the image proxy (some hosts refuse browsers hotlinking from another site) -------------------------------

DANBOORU_IMG = "https://cdn.donmai.us/sample/7.jpg"


def test_the_image_proxy_serves_allowlisted_images_to_the_owner_only(owner, web):
    web.images[DANBOORU_IMG] = encode(picture(5), "JPEG")
    r = owner.get("/api/discover/img", params={"u": DANBOORU_IMG})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:3] == b"\xff\xd8\xff"
    assert "private" in r.headers["cache-control"] and r.headers["x-content-type-options"] == "nosniff"
    owner.post("/api/logout")
    assert owner.get("/api/discover/img", params={"u": DANBOORU_IMG}).status_code == 401


@pytest.mark.parametrize("url", ["https://evil.example/a.jpg", "http://cdn.donmai.us/a.jpg", "https://127.0.0.1/a.jpg",
                                 "https://cdn.donmai.us.evil.example/a.jpg", "file:///etc/passwd"])
def test_the_image_proxy_refuses_anything_outside_the_allowed_hosts(owner, web, url):
    assert owner.get("/api/discover/img", params={"u": url}).status_code == 400
    assert web.calls == [], "nothing was requested"


def test_the_image_proxy_never_passes_a_bot_check_page_on_as_an_image(owner, web):
    web.images[DANBOORU_IMG] = b"<!DOCTYPE html><title>Just a moment...</title>"
    r = owner.get("/api/discover/img", params={"u": DANBOORU_IMG})
    assert r.status_code == 502 and "not return an image" in r.json()["detail"]


def test_the_image_proxy_reports_a_missing_or_oversized_file(owner, web):
    assert owner.get("/api/discover/img", params={"u": "https://cdn.donmai.us/none.jpg"}).status_code == 502
    web.big.add(DANBOORU_IMG)
    assert owner.get("/api/discover/img", params={"u": DANBOORU_IMG}).status_code == 502
