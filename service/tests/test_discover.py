import httpx
import pytest

from afterimage import discover
from afterimage.sources import wallhaven
from afterimage.sources.base import host_allowed
from conftest import add_image, encode, picture

WH_THUMB = "https://th.wallhaven.cc/lg/aa/aaa111.jpg"
WH_FULL = "https://w.wallhaven.cc/full/aa/wallhaven-aaa111.jpg"
STEAM_SHOT = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/123/ss_0.1920x1080.jpg"
STEAM_THUMB = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/123/ss_0.600x338.jpg"


@pytest.fixture(autouse=True)
def fast_wallhaven(monkeypatch):
    monkeypatch.setattr(wallhaven, "MIN_INTERVAL", 0)


class Web:
    """A fake internet: routes by URL, serves images from a dict, records every request."""

    def __init__(self):
        self.images: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.redirects: dict[str, str] = {}
        self.big: set[str] = set()
        self.gaps = {"covered": [], "gaps": []}
        self.activities: list[dict] = []      # infovore's persisted history
        self.wh_empty: set[str] = set()       # Wallhaven queries that find nothing
        self.wh_queries: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.calls.append(url)
        host = req.url.host
        if any(f in host for f in self.fail):
            return httpx.Response(500, text="boom")
        if url in self.redirects:
            return httpx.Response(302, headers={"location": self.redirects[url]})
        if url in self.big:
            return httpx.Response(200, headers={"content-length": str(99 * 1048576)}, content=b"x")
        if url in self.images:
            return httpx.Response(200, content=self.images[url])
        path = req.url.path
        if host == "infovore.test" and path == "/api/activities.json":
            q = req.url.params
            rows = [r for r in self.activities if r["mediaKind"] == q.get("kind")]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 500))
            return httpx.Response(200, json={"data": rows[off: off + lim]})
        if host == "wallhaven.cc" and path.endswith("/search"):
            query = req.url.params.get("q", "").strip('"')
            self.wh_queries.append(query)
            if query in self.wh_empty:
                return httpx.Response(200, json={"data": []})
            hit = lambda i, res: {"id": i, "url": f"https://wallhaven.cc/w/{i}", "resolution": res, "favorites": 9,
                                  "thumbs": {"large": f"https://th.wallhaven.cc/lg/aa/{i}.jpg", "original": f"https://th.wallhaven.cc/orig/aa/{i}.jpg"},
                                  "path": f"https://w.wallhaven.cc/full/aa/wallhaven-{i}.jpg"}
            return httpx.Response(200, json={"data": [hit("aaa111", "1920x1080"), hit("banner1", "8000x2000"), hit("bbb222", "2560x1440")]})
        if host == "store.steampowered.com" and "storesearch" in path:
            return httpx.Response(200, json={"items": [{"id": 123, "name": "Celeste", "type": "app"}, {"id": 9, "name": "Celeste Demo", "type": "app"}]})
        if host == "store.steampowered.com" and "appdetails" in path:
            return httpx.Response(200, json={"123": {"success": True, "data": {"name": "Celeste", "background_raw": "https://shared.akamai.steamstatic.com/bg.jpg?t=1",
                                                    "screenshots": [{"id": 0, "path_thumbnail": STEAM_THUMB, "path_full": STEAM_SHOT}]}}})
        if host == "danbooru.donmai.us" and path == "/tags.json":
            return httpx.Response(200, json=[{"name": "celeste", "category": 3, "post_count": 5}])
        if host == "danbooru.donmai.us" and path == "/posts.json":
            return httpx.Response(200, json=[
                {"id": 7, "file_url": "https://cdn.donmai.us/original/7.jpg", "large_file_url": "https://cdn.donmai.us/sample/7.jpg",
                 "preview_file_url": "https://cdn.donmai.us/180x180/7.jpg", "image_width": 1600, "image_height": 1000, "fav_count": 30, "tag_string_artist": "some_artist"},
                {"id": 8, "file_url": "https://cdn.donmai.us/original/8.mp4", "image_width": 1600, "image_height": 1000},
                {"id": 9, "file_url": "https://cdn.donmai.us/original/9.jpg", "image_width": 300, "image_height": 200}])
        if host == "graphql.anilist.co":
            return httpx.Response(200, json={"data": {"Page": {"media": [{"id": 1, "siteUrl": "https://anilist.co/anime/1", "title": {"english": "Celeste", "romaji": None, "native": None},
                                                                          "coverImage": {"extraLarge": "https://s4.anilist.co/c.jpg"}, "bannerImage": None}]}}})
        if host == "infovore.test":
            return httpx.Response(200, json={"result": {"structuredContent": self.gaps}})
        return httpx.Response(404)


@pytest.fixture
def web(owner):
    w = Web()
    owner.app.state.discover.http = httpx.AsyncClient(transport=httpx.MockTransport(w), follow_redirects=False)
    return w


def search(owner, **q):
    params = {"title": "Celeste", "kind": "game", **q}
    r = owner.get("/api/discover/search", params=params)
    assert r.status_code == 200, r.text
    return r.json()["sources"]


# ---- the host allowlist --------------------------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("https://w.wallhaven.cc/full/a.jpg", True), ("https://shared.akamai.steamstatic.com/a.jpg", True),
    ("https://image.tmdb.org/t/p/original/a.jpg", True), ("https://cdn.donmai.us/original/a.jpg", True),
    ("http://w.wallhaven.cc/a.jpg", False), ("https://evil.com/wallhaven.cc/a.jpg", False),
    ("https://wallhaven.cc.evil.com/a.jpg", False), ("https://notwallhaven.cc/a.jpg", False),
    ("https://127.0.0.1/a.jpg", False), ("file:///etc/passwd", False), ("", False)])
def test_only_known_image_hosts_are_allowed(url, ok):
    assert host_allowed(url) is ok


# ---- searching ----------------------------------------------------------------------------

def test_search_queries_the_sources_for_the_kind_and_filters_bad_shapes(owner, web):
    src = search(owner)
    assert set(src) == {"steam", "wallhaven", "danbooru"}, "a game uses steam, wallhaven and danbooru"
    wh = [i["source_id"] for i in src["wallhaven"]["items"]]
    assert wh == ["aaa111", "bbb222"], "the 8000x2000 banner is dropped"
    assert [i["source_id"] for i in src["steam"]["items"]] == ["123:background", "123:0"]
    assert [i["source_id"] for i in src["danbooru"]["items"]] == ["7"], "video and tiny images are dropped"
    assert src["danbooru"]["items"][0]["artist"] == "some_artist"


def test_the_source_list_can_be_chosen_and_anime_uses_anilist(owner, web):
    assert set(search(owner, kind="anime")) == {"wallhaven", "danbooru", "anilist"}
    assert set(search(owner, sources="wallhaven")) == {"wallhaven"}
    assert set(search(owner, sources="nonsense")) == set(), "unknown source names are ignored"


def test_one_failing_source_is_reported_and_does_not_hide_the_others(owner, web):
    web.fail = {"steampowered"}
    src = search(owner)
    assert src["steam"]["ok"] is False and "500" in src["steam"]["error"] and src["steam"]["items"] == []
    assert src["wallhaven"]["ok"] is True and len(src["wallhaven"]["items"]) == 2


def test_tmdb_without_a_token_says_so_instead_of_failing_the_search(owner, web):
    src = search(owner, title="Hello Ghost", kind="movie")
    assert src["tmdb"]["ok"] is False and "TMDB_TOKEN" in src["tmdb"]["error"]
    assert src["wallhaven"]["ok"] is True


def test_results_are_marked_when_the_collection_already_has_the_picture(owner, web):
    have = add_image(owner, "mine", seed=11, works=["celeste"])
    web.images[WH_THUMB] = encode(have.resize((200, 125)), "JPEG", quality=60)           # same picture, smaller
    web.images["https://th.wallhaven.cc/lg/aa/bbb222.jpg"] = encode(picture(99), "JPEG")  # unrelated
    items = {i["source_id"]: i for i in search(owner, sources="wallhaven")["wallhaven"]["items"]}
    assert items["aaa111"]["have"]["id"] == "mine" and items["aaa111"]["have"]["distance"] <= 3
    assert items["bbb222"]["have"] is None and items["bbb222"]["similar"] is None


def test_a_missing_thumbnail_only_means_it_cannot_be_compared(owner, web):
    items = search(owner, sources="wallhaven")["wallhaven"]["items"]
    assert [i["have"] for i in items] == [None, None]


# ---- importing --------------------------------------------------------------------------------

def do_import(owner, url=WH_FULL, **extra):
    body = {"url": url, "source": "wallhaven", "source_url": "https://wallhaven.cc/w/aaa111", "source_ref": "wallhaven:aaa111",
            "work_title": "Celeste", "work_kind": "game", **extra}
    return owner.post("/api/discover/import", json=body)


def test_importing_stores_the_files_the_row_and_creates_the_work(owner, web, db):
    web.images[WH_FULL] = encode(picture(5, (1920, 1080)), "JPEG", quality=85)
    r = do_import(owner, artist="someone")
    assert r.status_code == 200 and r.json()["status"] == "imported" and r.json()["device"] == "desktop"
    iid = r.json()["id"]
    s = owner.app.state.settings
    assert (s.originals_dir / f"{iid}.jpg").is_file() and (s.thumbs_dir / f"{iid}.avif").is_file() and (s.view_dir / f"{iid}.avif").is_file()
    row = db.one("SELECT * FROM images WHERE id=?", (iid,))
    assert (row["source"], row["source_ref"], row["artist"], row["origin"], row["reviewed"]) == ("wallhaven", "wallhaven:aaa111", "someone", "imported", 0)
    assert [r_["work_id"] for r_ in db.all("SELECT work_id FROM image_works WHERE image_id=?", (iid,))] == ["celeste"]
    item = next(i for i in owner.get("/api/catalog").json()["items"] if i["id"] == iid)
    assert item["source"] == "wallhaven" and item["artist"] == "someone" and item["origin"] == "imported"
    assert owner.get(f"/original/{iid}").status_code == 200 and owner.get(f"/thumbs/{iid}.avif").status_code == 200


def test_the_same_file_twice_is_not_stored_twice(owner, web, db):
    web.images[WH_FULL] = encode(picture(5, (800, 500)), "PNG")
    assert do_import(owner).json()["status"] == "imported"
    assert do_import(owner).json()["status"] == "exists"
    assert db.one("SELECT COUNT(*) n FROM images")["n"] == 1


def test_a_lookalike_of_something_in_the_collection_is_refused_unless_forced(owner, web, db):
    base = add_image(owner, "mine", seed=21)
    web.images[WH_FULL] = encode(base.resize((300, 188)), "JPEG", quality=50)
    r = do_import(owner).json()
    assert r["status"] == "duplicate" and r["nearest"]["id"] == "mine"
    assert db.one("SELECT COUNT(*) n FROM images")["n"] == 1
    assert do_import(owner, force=True).json()["status"] == "imported"


def test_bad_downloads_are_refused_with_a_clear_reason(owner, web, db):
    web.images[WH_FULL] = b"<html>not an image</html>"
    r = do_import(owner)
    assert r.status_code == 422 and "not a usable image" in r.json()["detail"]
    import io
    from PIL import Image
    gif = io.BytesIO(); Image.new("RGB", (50, 50)).save(gif, "GIF")
    web.images[WH_FULL] = gif.getvalue()
    assert "unsupported format" in do_import(owner).json()["detail"]
    web.big.add(WH_FULL)
    assert "larger than" in do_import(owner).json()["detail"]
    assert db.one("SELECT COUNT(*) n FROM images")["n"] == 0


def test_urls_outside_the_allowed_hosts_are_never_fetched(owner, web):
    for url in ("https://evil.example/a.jpg", "http://w.wallhaven.cc/a.jpg", "https://127.0.0.1:8000/a.jpg"):
        r = do_import(owner, url=url)
        assert r.status_code == 422 and "not allowed" in r.json()["detail"], url
    assert web.calls == [], "nothing was requested"


def test_a_redirect_to_a_forbidden_host_is_refused_before_following_it(owner, web):
    web.redirects[WH_FULL] = "https://internal.example/secret.jpg"
    r = do_import(owner)
    assert r.status_code == 422 and "not allowed" in r.json()["detail"]
    assert not any("internal.example" in c for c in web.calls)


def test_a_redirect_to_another_allowed_host_is_followed(owner, web):
    web.redirects[WH_FULL] = "https://w.wallhaven.cc/full/aa/final.jpg"
    web.images["https://w.wallhaven.cc/full/aa/final.jpg"] = encode(picture(8), "PNG")
    assert do_import(owner).json()["status"] == "imported"


def test_importing_into_an_unknown_work_id_is_rejected(owner, web):
    web.images[WH_FULL] = encode(picture(5), "PNG")
    assert do_import(owner, work_title=None, work_id="no-such").status_code == 422


# ---- the inbox and the batch run -------------------------------------------------------------

GAPS = {"covered": [], "gaps": [
    {"title": "Celeste", "kind": "game", "source": "backloggd", "status": "playing", "lastActivityAt": "2026-11-10T00:00:00Z", "rating": None},
    {"title": "Escape", "kind": "movie", "source": "simkl", "status": "completed", "lastActivityAt": "2026-11-09T00:00:00Z", "rating": None},
    {"title": "Some Song", "kind": "music", "source": "statsfm", "status": "listened", "lastActivityAt": "2026-11-08T00:00:00Z", "rating": None},
]}


def test_gaps_endpoint_reports_infovore_being_down_as_502(owner, web):
    web.fail = {"infovore"}
    assert owner.get("/api/discover/gaps").status_code == 502


def test_the_work_links_are_looked_up_and_saved(owner, web):
    wid = owner.post("/api/works", json={"title": "Celeste", "kind": "game"}).json()["id"]
    r = owner.post(f"/api/works/{wid}/resolve-links").json()
    assert r["links"] == {"steam": "https://store.steampowered.com/app/123"}
    owner.post("/api/works", json={"title": "Ajin", "kind": "anime"})
    assert owner.post("/api/works/ajin/resolve-links").status_code == 200
    assert owner.post("/api/works/none/resolve-links").status_code == 404
    assert any(w["id"] == wid and w["links"] for w in owner.get("/api/catalog").json()["works"])


def test_generic_titles():
    from afterimage import feed
    assert feed.too_generic("Escape", "movie") and feed.too_generic("Dark", "show")
    assert not feed.too_generic("Celeste", "game") and not feed.too_generic("Dark Matter", "show")


# ---- TMDB: preview only ----------------------------------------------------------------------

def with_tmdb(owner, web):
    import dataclasses
    st = owner.app.state.discover
    st.settings = dataclasses.replace(st.settings, tmdb_token="tmdb-test-token")
    orig = web.__call__

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.themoviedb.org":
            assert req.headers["authorization"] == "Bearer tmdb-test-token"
            if req.url.path.endswith("/search/multi"):
                return httpx.Response(200, json={"results": [
                    {"media_type": "movie", "id": 5, "title": "Hello Ghost", "release_date": "2010-05-01"},
                    {"media_type": "movie", "id": 6, "title": "Hello Ghost", "release_date": "2023-01-01"},
                    {"media_type": "person", "id": 9, "name": "Hello Ghost"},
                    {"media_type": "movie", "id": 7, "title": "Hello Ghostbusters", "release_date": "1999-01-01"}]})
            if "/movie/5/" in req.url.path:
                return httpx.Response(200, json={"backdrops": [
                    {"file_path": "/a1.jpg", "width": 3840, "height": 2160, "vote_average": 5.1, "iso_639_1": None},
                    {"file_path": "/text.jpg", "width": 3840, "height": 2160, "vote_average": 9, "iso_639_1": "en"},
                    {"file_path": "/small.jpg", "width": 800, "height": 450, "vote_average": 9, "iso_639_1": None}]})
            if "/movie/6/" in req.url.path:   # only a backdrop with text on it
                return httpx.Response(200, json={"backdrops": [{"file_path": "/b1.jpg", "width": 2248, "height": 1264, "iso_639_1": "ko"}]})
            return httpx.Response(404)
        return orig(req)
    st.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_tmdb_results_are_preview_only_and_cover_every_same_named_work(owner, web):
    with_tmdb(owner, web)
    items = search(owner, title="Hello Ghost", kind="movie", sources="tmdb")["tmdb"]["items"]
    assert [(i["source_id"], i["label"]) for i in items] == [
        ("5:a1", "Hello Ghost (2010) backdrop"),                    # clean backdrop preferred, small and texty ones dropped
        ("6:b1", "Hello Ghost (2023) backdrop, has text")], "both films, the person and the 'Ghostbusters' near-miss excluded"
    assert all(i["importable"] is False for i in items)
    assert [i["source_url"] for i in items] == ["https://www.themoviedb.org/movie/5", "https://www.themoviedb.org/movie/6"]
    assert search(owner, sources="wallhaven")["wallhaven"]["items"][0]["importable"] is True


def test_tmdb_images_cannot_be_imported_even_by_calling_the_api_directly(owner, web, db):
    web.images["https://image.tmdb.org/t/p/original/a1.jpg"] = encode(picture(5, (1920, 1080)), "JPEG")
    for body in ({"url": "https://image.tmdb.org/t/p/original/a1.jpg", "source": "tmdb"},
                 {"url": "https://image.tmdb.org/t/p/original/a1.jpg", "source": "wallhaven"},   # lying about the source
                 {"url": "https://w.wallhaven.cc/x.jpg", "source": "tmdb"}):
        r = owner.post("/api/discover/import", json={**body, "work_title": "Hello Ghost", "work_kind": "movie"})
        assert r.status_code == 422 and "TMDB" in r.json()["detail"], body
    assert not any("image.tmdb.org" in c for c in web.calls), "nothing was even downloaded"
    assert db.one("SELECT COUNT(*) n FROM images")["n"] == 0


def test_same_named_works_share_the_slots_instead_of_the_first_taking_them_all(owner, web):
    import dataclasses
    st = owner.app.state.discover
    st.settings = dataclasses.replace(st.settings, tmdb_token="t")
    many = lambda tag: [{"file_path": f"/{tag}{i}.jpg", "width": 3840, "height": 2160, "vote_average": 9 - i / 10, "iso_639_1": None} for i in range(10)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/search/multi"):
            return httpx.Response(200, json={"results": [{"media_type": "movie", "id": i, "title": "Same Name", "release_date": f"{2000 + i}-01-01"} for i in (1, 2, 3)]})
        mid = req.url.path.split("/")[-2]
        return httpx.Response(200, json={"backdrops": many(f"m{mid}_")})
    st.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    items = search(owner, title="Same Name", kind="movie", sources="tmdb", limit=12)["tmdb"]["items"]
    by_film = {}
    for i in items:
        by_film[i["source_url"]] = by_film.get(i["source_url"], 0) + 1
    assert by_film == {f"https://www.themoviedb.org/movie/{i}": 4 for i in (1, 2, 3)}, by_film
