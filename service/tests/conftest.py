import io
import random
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afterimage import auth, imaging  # noqa: E402
from afterimage.config import Settings  # noqa: E402
from afterimage.main import create_app  # noqa: E402

PASSWORD = "correct horse battery"
TOKEN = "t" * 40


def picture(seed: int, size=(480, 300)) -> Image.Image:
    """A structured random picture: different seeds look different to a difference hash."""
    rnd = random.Random(seed)
    im = Image.new("RGB", size, tuple(rnd.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        r = rnd.randrange(15, 90)
        d.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rnd.randrange(256) for _ in range(3)))
    return im


def encode(im: Image.Image, fmt="PNG", **kw) -> bytes:
    b = io.BytesIO()
    im.save(b, fmt, **kw)
    return b.getvalue()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data", frontend_dir=None, public_url="https://afterimage.test",
        admin_password_hash=auth.hash_password(PASSWORD), session_secret="s" * 40, api_token=TOKEN,
        infovore_mcp="https://infovore.test/mcp", tmdb_token="")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="https://afterimage.test") as c:
        yield c


@pytest.fixture
def db(client):
    return client.app.state.db


@pytest.fixture
def owner(client):
    r = client.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200
    return client


def add_image(client, image_id, *, personal=False, seed=None, works=(), emotion=None, reviewed=False, confidence=None):
    """Insert a real image (files + row) straight into the service's data directory."""
    s = client.app.state.settings
    db = client.app.state.db
    s.ensure_dirs()
    im = picture(seed if seed is not None else hash(image_id) % 10_000)
    (s.originals_dir / f"{image_id}.png").write_bytes(encode(im))
    imaging.write_derivatives(im, image_id, s.thumbs_dir, s.view_dir)
    info = imaging.analyse(im)
    import json
    with db.tx() as c:
        for w in works:
            c.execute("INSERT OR IGNORE INTO works(id, title, kind, created_at) VALUES (?,?,?, '2026-01-01')", (w, w.title(), "game"))
        c.execute(
            "INSERT INTO images(id, original, caption, device, width, height, bytes, palette, dhash, emotion, reviewed, personal,"
            " confidence, origin, added_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'collection','2026-01-01','2026-01-01')",
            (image_id, f"{image_id}.png", f"caption {image_id}", info["device"], info["width"], info["height"], 1000,
             json.dumps(info["palette"]), info["dhash"], emotion, int(reviewed), int(personal), confidence))
        c.executemany("INSERT INTO image_works(image_id, work_id) VALUES (?,?)", [(image_id, w) for w in works])
    return im
