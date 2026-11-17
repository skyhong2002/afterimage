import time

import pytest
from PIL import Image

from afterimage import auth, imaging
from conftest import PASSWORD, TOKEN, add_image, encode, picture


# ---- authentication --------------------------------------------------------------

def test_password_hash_roundtrip():
    h = auth.hash_password("a long enough password")
    assert auth.verify_password("a long enough password", h)
    assert not auth.verify_password("a long enough passwore", h)
    assert not auth.verify_password("x", "not-a-hash")
    assert not auth.verify_password("x", "md5:1:2:3:aa:bb")
    assert "$" not in h, "must survive shell and Compose expansion"


def test_session_cookie_is_signed_and_expires():
    secret = "k" * 32
    cookie = auth.make_session(secret, now=1000)
    assert auth.check_session(secret, cookie, now=1001)
    assert not auth.check_session(secret, cookie, now=1000 + auth.SESSION_DAYS * 86400 + 1), "expired"
    assert not auth.check_session("other" * 8, cookie, now=1001), "wrong secret"
    payload, sig = cookie.rsplit(".", 1)
    assert not auth.check_session(secret, payload + "." + sig[:-1] + ("0" if sig[-1] != "0" else "1"), now=1001), "tampered"
    assert not auth.check_session(secret, None) and not auth.check_session(secret, "garbage")
    assert not auth.check_session("", cookie), "no secret configured means nobody is logged in"


def test_login_limiter_blocks_after_repeated_failures():
    lim = auth.LoginLimiter()
    for _ in range(auth.MAX_FAILS):
        assert not lim.blocked("ip", now=100)
        lim.fail("ip", now=100)
    assert lim.blocked("ip", now=101)
    assert not lim.blocked("ip", now=100 + auth.FAIL_WINDOW + 1), "window passes"
    assert not lim.blocked("other", now=101)


# ---- duplicate detection ----------------------------------------------------------

def test_dhash_survives_resizing_and_recompression_but_not_a_different_picture():
    base = picture(1, (800, 500))
    small = Image.open(__import__("io").BytesIO(encode(base.resize((320, 200)), "JPEG", quality=60)))
    other = picture(2, (800, 500))
    h = imaging.dhash(base)
    assert imaging.distance(h, imaging.dhash(small)) <= imaging.SAME_DISTANCE
    assert imaging.distance(h, imaging.dhash(other)) > imaging.SIMILAR_DISTANCE


def test_near_flat_images_get_no_hash_so_black_wallpapers_do_not_all_collide():
    assert imaging.dhash(Image.new("RGB", (400, 300), (3, 3, 3))) is None
    assert imaging.distance(None, "0" * 16) is None


# ---- permissions ---------------------------------------------------------------------

def test_editing_and_discovery_require_login(client):
    add_image(client, "aaa", works=["w1"])
    assert client.get("/api/me").json() == {"admin": False}
    assert client.patch("/api/images/aaa", json={"emotion": "joy"}).status_code == 401
    assert client.post("/api/works", json={"title": "X", "kind": "game"}).status_code == 401
    assert client.get("/api/discover/search?title=x").status_code == 401
    assert client.post("/api/discover/run").status_code == 401
    assert client.post("/api/discover/import", json={"url": "https://w.wallhaven.cc/a.jpg", "source": "wallhaven"}).status_code == 401
    assert client.get("/api/discover/feed").status_code == 401
    assert client.get("/api/discover/progress").status_code == 401
    assert client.get("/api/discover/gaps").status_code == 401


def test_login_rejects_a_wrong_password_and_rate_limits(client):
    for _ in range(auth.MAX_FAILS):
        assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 429, "even the right password waits"


def test_login_sets_a_strict_httponly_cookie_and_logout_clears_it(client):
    r = client.post("/api/login", json={"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert client.get("/api/me").json() == {"admin": True}
    client.post("/api/logout")
    assert client.get("/api/me").json() == {"admin": False}


def test_bearer_token_works_for_scripts_and_a_wrong_token_does_not(client):
    add_image(client, "aaa", works=["w1"])
    assert client.patch("/api/images/aaa", json={"emotion": "joy"}, headers={"authorization": "Bearer nope"}).status_code == 401
    assert client.patch("/api/images/aaa", json={"emotion": "joy"}, headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_personal_photos_are_invisible_to_visitors_in_every_endpoint(client):
    add_image(client, "pub", works=["w1"])
    add_image(client, "mine", personal=True)
    anon = client.get("/api/catalog").json()
    assert [i["id"] for i in anon["items"]] == ["pub"]
    assert "mine" not in str(client.get("/api/wallpapers.json").json())
    for path in ("/thumbs/mine.avif", "/view/mine.avif", "/original/mine"):
        assert client.get(path).status_code == 404, path
        assert client.get(path.replace("mine", "pub")).status_code == 200
    client.post("/api/login", json={"password": PASSWORD})
    assert {i["id"] for i in client.get("/api/catalog").json()["items"]} == {"pub", "mine"}
    assert client.get("/thumbs/mine.avif").status_code == 200
    assert "no-store" in client.get("/thumbs/mine.avif").headers["cache-control"]


# ---- the contract infovore reads ----------------------------------------------------------

def test_public_wallpapers_json_keeps_the_shape_infovore_validates(client):
    add_image(client, "aaa", works=["persona-5"], emotion="anger", reviewed=True)
    add_image(client, "bbb", works=["persona-5", "daft-punk"])
    add_image(client, "ccc", works=["unused-work-only-on-personal"], personal=True)
    doc = client.get("/api/wallpapers.json").json()
    assert doc["version"] == 1
    assert {w["id"]: w["count"] for w in doc["works"]} == {"persona-5": 2, "daft-punk": 1}, "only works with a public image"
    for w in doc["works"]:
        assert set(w) == {"id", "title", "kind", "count"}
    assert {i["id"] for i in doc["wallpapers"]} == {"aaa", "bbb"}
    a = next(i for i in doc["wallpapers"] if i["id"] == "aaa")
    assert set(a) == {"id", "caption", "device", "width", "height", "works", "emotion", "palette", "reviewed"}
    assert a["device"] in {"desktop", "phone"} and a["emotion"] == "anger" and a["reviewed"] is True
    assert "public" in client.get("/api/wallpapers.json").headers["cache-control"]


# ---- editing --------------------------------------------------------------------------------

def test_a_review_edit_updates_the_image_clears_the_ai_note_and_is_logged(owner, db):
    add_image(owner, "aaa", works=["w1"], emotion="joy", confidence="l")
    owner.post("/api/works", json={"title": "Persona 5", "kind": "game"})
    r = owner.patch("/api/images/aaa", json={"works": ["persona-5"], "emotion": "anger"})
    assert r.status_code == 200 and r.json()["works"] == ["persona-5"] and r.json()["reviewed"] is True
    row = db.one("SELECT * FROM images WHERE id='aaa'")
    assert row["emotion"] == "anger" and row["confidence"] is None
    log = db.all("SELECT * FROM edits WHERE image_id='aaa'")
    assert len(log) == 1 and '"joy"' in log[0]["before"] and '"anger"' in log[0]["after"]


@pytest.mark.parametrize("body,status", [
    ({"emotion": "rage"}, 422), ({"works": ["no-such-work"]}, 422), ({"works": "w1"}, 422),
    ({"caption": "  "}, 422), ({"width": 5}, 422), ({}, 422)])
def test_bad_edits_are_rejected_and_change_nothing(owner, db, body, status):
    add_image(owner, "aaa", works=["w1"], emotion="joy")
    assert owner.patch("/api/images/aaa", json=body).status_code == status
    assert db.one("SELECT emotion FROM images WHERE id='aaa'")["emotion"] == "joy"
    assert db.all("SELECT * FROM edits") == []


def test_editing_a_missing_image_is_404(owner):
    assert owner.patch("/api/images/nope", json={"emotion": "joy"}).status_code == 404


def test_creating_a_work_twice_returns_the_same_one(owner):
    first = owner.post("/api/works", json={"title": "Celeste", "kind": "game"}).json()
    second = owner.post("/api/works", json={"title": "  celeste ", "kind": "game"}).json()
    assert first["created"] is True and second["created"] is False and first["id"] == second["id"] == "celeste"
    assert owner.post("/api/works", json={"title": "X", "kind": "podcast"}).status_code == 422
    assert owner.post("/api/works", json={"title": "文字遊戲", "kind": "game"}).json()["id"].startswith("w-")


def test_the_gallery_payload_has_what_the_frontend_reads(owner):
    add_image(owner, "aaa", works=["persona-5"], emotion="anger", reviewed=True)
    add_image(owner, "bbb", works=["persona-5"])
    doc = owner.get("/api/catalog").json()
    it = next(i for i in doc["items"] if i["id"] == "aaa")
    for key in ("caption", "device", "w", "h", "mb", "palette", "similar", "works", "emotion", "reviewed", "personal",
                "confidence", "note", "src", "raw"):
        assert key in it, key
    assert it["src"] == "/view/aaa.avif" and it["raw"] == "/original/aaa" and it["works"] == ["persona-5"]
    assert next(w for w in doc["works"] if w["id"] == "persona-5")["count"] == 2


def test_healthz_counts_images(client):
    add_image(client, "aaa")
    assert client.get("/healthz").json() == {"status": "ok", "images": 1}


def test_database_backup_is_a_usable_copy(client, db, tmp_path):
    add_image(client, "aaa")
    dest = tmp_path / "b.sqlite"
    db.backup(dest)
    import sqlite3
    assert sqlite3.connect(dest).execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1


def test_the_order_of_works_is_kept_because_the_first_one_is_the_main_work(owner, db):
    add_image(owner, "aaa", works=["w1"])
    for t in ("Persona 5", "Persona 4", "Persona 3"):
        owner.post("/api/works", json={"title": t, "kind": "game"})
    owner.patch("/api/images/aaa", json={"works": ["persona-5", "persona-4", "persona-3"]})
    assert next(i for i in owner.get("/api/catalog").json()["items"] if i["id"] == "aaa")["works"] == ["persona-5", "persona-4", "persona-3"]
    assert owner.get("/api/wallpapers.json").json()["wallpapers"][0]["works"] == ["persona-5", "persona-4", "persona-3"]
    owner.patch("/api/images/aaa", json={"works": ["persona-3", "persona-5"]})
    assert next(i for i in owner.get("/api/catalog").json()["items"] if i["id"] == "aaa")["works"] == ["persona-3", "persona-5"]


def test_every_original_extension_is_served_as_an_image(owner, db):
    for ext, mime in ((".jpeg", "image/jpeg"), (".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")):
        iid = "x" + ext.strip(".")
        add_image(owner, iid)
        s = owner.app.state.settings
        (s.originals_dir / f"{iid}.png").rename(s.originals_dir / f"{iid}{ext}")
        with db.tx() as c:
            c.execute("UPDATE images SET original=? WHERE id=?", (f"{iid}{ext}", iid))
        r = owner.get(f"/original/{iid}")
        assert r.status_code == 200 and r.headers["content-type"] == mime, ext


def test_login_rate_limit_cannot_be_dodged_by_faking_forwarded_for(client):
    """The first X-Forwarded-For entry is client-controlled; only the proxy's (last) entry counts."""
    for i in range(auth.MAX_FAILS):
        r = client.post("/api/login", json={"password": "nope"}, headers={"x-forwarded-for": f"10.9.9.{i}, 203.0.113.7"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"password": PASSWORD}, headers={"x-forwarded-for": "10.1.2.3, 203.0.113.7"})
    assert r.status_code == 429, "rotating the spoofed address did not reset the counter"
    other = client.post("/api/login", json={"password": PASSWORD}, headers={"x-forwarded-for": "198.51.100.9"})
    assert other.status_code == 200, "a different real address is unaffected"
