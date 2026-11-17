import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from afterimage import backup
from conftest import add_image


def sh(*args, cwd=None) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def populated(owner, db):
    add_image(owner, "aaa", works=["persona-5", "daft-punk"], emotion="joy", reviewed=True, seed=1)
    add_image(owner, "bbb", works=["persona-5"], seed=2)
    add_image(owner, "mine", personal=True, seed=3)
    with db.tx() as c:
        c.executemany("INSERT INTO similar VALUES (?,?,?)", [("aaa", 0, "bbb"), ("aaa", 1, "mine"), ("mine", 0, "aaa"), ("bbb", 0, "aaa")])
        c.execute("INSERT INTO candidates(id, source, full_url, work_query, work_kind, found_at, status)"
                  " VALUES ('wallhaven:x', 'wallhaven', 'https://w.wallhaven.cc/x.jpg', 'Celeste', 'game', '2026-01-01', 'rejected')")
    owner.patch("/api/images/aaa", json={"emotion": "anger"})   # leaves an edit-log row
    return owner


def data_dir(c) -> Path:
    return c.app.state.settings.data_dir


@pytest.fixture
def remote(tmp_path):
    """A bare repo standing in for GitHub, with a main branch that already holds a file."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "seed"
    sh("git", "init", "--quiet", "--bare", "-b", "main", str(bare))
    sh("git", "init", "--quiet", "-b", "main", str(work))
    (work / "README.md").write_text("code lives here\n")
    sh("git", "-C", str(work), "add", ".")
    sh("git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "init")
    sh("git", "-C", str(work), "push", "--quiet", str(bare), "main")
    return bare


def tree(bare: Path, ref="backup") -> set[str]:
    return set(sh("git", f"--git-dir={bare}", "ls-tree", "-r", "--name-only", ref).splitlines())


# ---- the snapshot ---------------------------------------------------------------------------

def test_snapshot_is_deterministic_and_keeps_nothing_about_personal_images(populated):
    db_path = data_dir(populated) / "afterimage.sqlite"
    a, b = backup.snapshot(db_path), backup.snapshot(db_path)
    assert backup.dumps(a) == backup.dumps(b), "no timestamps or ordering noise, so an unchanged database makes no commit"
    stub = next(i for i in a["images"] if i["id"] == "mine")
    assert stub == {"id": "mine", "personal": True}
    text = backup.dumps(a)
    assert "caption mine" not in text and "mine.png" not in text
    assert not [r for r in a["image_works"] if r["image_id"] == "mine"]
    assert [(r["image_id"], r["other_id"]) for r in a["similar"]] == [("aaa", "bbb"), ("bbb", "aaa")], "pairs with a personal image are dropped"
    assert [r["work_id"] for r in a["image_works"] if r["image_id"] == "aaa"] == ["daft-punk", "persona-5"], "same position, so ordered by work id"
    assert a["candidates"][0]["status"] == "rejected" and len(a["edits"]) == 1


def test_the_work_order_survives_the_snapshot(populated):
    populated.post("/api/works", json={"title": "Zed", "kind": "game"})
    populated.patch("/api/images/bbb", json={"works": ["zed", "persona-5"]})
    snap = backup.snapshot(data_dir(populated) / "afterimage.sqlite")
    assert [r["work_id"] for r in snap["image_works"] if r["image_id"] == "bbb"] == ["zed", "persona-5"]


# ---- the working tree --------------------------------------------------------------------------

def test_sync_copies_only_originals_that_may_leave_and_is_idempotent(populated, tmp_path):
    d, repo = data_dir(populated), tmp_path / "repo"
    snap = backup.snapshot(d / "afterimage.sqlite")
    first = backup.sync(snap, d, repo)
    assert first == {"copied": 2, "removed": 0, "missing": 0, "images": 2}
    assert sorted(p.name for p in (repo / "originals").iterdir()) == ["aaa.png", "bbb.png"]
    assert backup.sync(snap, d, repo)["copied"] == 0
    populated.patch("/api/images/aaa", json={"personal": True})
    again = backup.sync(backup.snapshot(d / "afterimage.sqlite"), d, repo)
    assert again["removed"] == 1 and [p.name for p in (repo / "originals").iterdir()] == ["bbb.png"], "marking an image personal removes it from the tip"


def test_a_missing_original_is_counted_not_fatal(populated, tmp_path):
    d = data_dir(populated)
    (d / "originals" / "bbb.png").unlink()
    r = backup.sync(backup.snapshot(d / "afterimage.sqlite"), d, tmp_path / "repo")
    assert r["missing"] == 1 and r["copied"] == 1


# ---- restoring ------------------------------------------------------------------------------------

def dump_tables(db_file: Path) -> dict:
    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    t = lambda sql: [dict(r) for r in con.execute(sql)]  # noqa: E731
    return {"works": t("SELECT * FROM works ORDER BY id"), "images": t("SELECT * FROM images WHERE personal=0 ORDER BY id"),
            "image_works": t("SELECT * FROM image_works WHERE image_id IN (SELECT id FROM images WHERE personal=0) ORDER BY image_id, position"),
            "similar": t("SELECT * FROM similar WHERE image_id IN (SELECT id FROM images WHERE personal=0) AND other_id IN (SELECT id FROM images WHERE personal=0) ORDER BY image_id, rank"),
            "candidates": t("SELECT * FROM candidates ORDER BY id"),
            "edits": t("SELECT * FROM edits WHERE image_id IN (SELECT id FROM images WHERE personal=0) ORDER BY id")}


def test_restore_rebuilds_the_same_database_and_files(populated, tmp_path):
    d, repo, fresh = data_dir(populated), tmp_path / "repo", tmp_path / "restored"
    backup.sync(backup.snapshot(d / "afterimage.sqlite"), d, repo)
    res = backup.restore(repo, fresh)
    assert res == {"images": 2, "personal_skipped": 1, "files_missing": 0, "derivatives_built": 2}
    assert dump_tables(fresh / "afterimage.sqlite") == dump_tables(d / "afterimage.sqlite")
    for name in ("aaa.png", "bbb.png"):
        assert (fresh / "originals" / name).read_bytes() == (d / "originals" / name).read_bytes()
    for i in ("aaa", "bbb"):
        assert (fresh / "thumbs" / f"{i}.avif").is_file() and (fresh / "view" / f"{i}.avif").is_file()
    assert not (fresh / "originals" / "mine.png").exists()
    with pytest.raises(SystemExit, match="already exists"):
        backup.restore(repo, fresh)


def test_a_restored_service_serves_the_collection(populated, tmp_path, settings):
    import dataclasses

    from fastapi.testclient import TestClient

    from afterimage.main import create_app
    d, repo, fresh = data_dir(populated), tmp_path / "repo", tmp_path / "restored"
    backup.sync(backup.snapshot(d / "afterimage.sqlite"), d, repo)
    backup.restore(repo, fresh)
    with TestClient(create_app(dataclasses.replace(settings, data_dir=fresh)), base_url="https://afterimage.test") as c:
        items = c.get("/api/catalog").json()["items"]
        assert {i["id"] for i in items} == {"aaa", "bbb"}
        assert next(i for i in items if i["id"] == "aaa")["emotion"] == "anger"
        assert c.get("/thumbs/aaa.avif").status_code == 200 and c.get("/original/bbb").status_code == 200


# ---- pushing to a real (local) git remote -----------------------------------------------------------

def run_backup(c, remote, tmp_path, **kw):
    return backup.run(data_dir(c), tmp_path / "clone", str(remote), **kw)


def test_run_pushes_a_branch_that_holds_only_the_backup(populated, remote, tmp_path):
    r = run_backup(populated, remote, tmp_path)
    assert r["ok"] and r["pushed"] and r["images"] == 2, r
    assert tree(remote) == {"snapshot.json", "originals/aaa.png", "originals/bbb.png"}, "no code from main, and nothing personal"
    assert sh("git", f"--git-dir={remote}", "show", "main:README.md") == "code lives here", "main is untouched"
    assert json.loads((data_dir(populated) / "backups" / backup.STATUS).read_text())["ok"] is True


def test_a_run_with_nothing_new_makes_no_commit_and_a_new_image_makes_one(populated, remote, tmp_path):
    first = run_backup(populated, remote, tmp_path)
    second = run_backup(populated, remote, tmp_path)
    assert second["ok"] and second["pushed"] is False and second["commit"] == first["commit"]
    add_image(populated, "ccc", works=["persona-5"], seed=4)
    third = run_backup(populated, remote, tmp_path)
    assert third["pushed"] is True and "originals/ccc.png" in tree(remote)
    assert sh("git", f"--git-dir={remote}", "rev-list", "--count", "backup") == "2"


def test_a_fresh_clone_continues_the_same_branch(populated, remote, tmp_path):
    run_backup(populated, remote, tmp_path)
    other = backup.run(data_dir(populated), tmp_path / "second-clone", str(remote))
    assert other["ok"] and other["pushed"] is False, "a new machine picks up where the branch is"
    assert sh("git", f"--git-dir={remote}", "rev-list", "--count", "backup") == "1"


def test_the_size_limit_stops_a_push_and_says_why(populated, remote, tmp_path):
    r = run_backup(populated, remote, tmp_path, stop_mb=0)
    assert r["ok"] is False and "not pushing" in r["error"]
    assert sh("git", f"--git-dir={remote}", "branch", "--list", "backup") == "", "nothing was pushed"
    assert "not pushing" in json.loads((data_dir(populated) / "backups" / backup.STATUS).read_text())["error"]


def test_a_large_repository_warns_but_still_pushes(populated, remote, tmp_path):
    r = run_backup(populated, remote, tmp_path, warn_mb=0)
    assert r["ok"] and r["pushed"] and "recommends" in r["warning"]


def test_an_unreachable_remote_is_reported_not_raised(populated, tmp_path):
    r = run_backup(populated, tmp_path / "nope.git", tmp_path)
    assert r["ok"] is False and "clone failed" in r["error"]
    assert json.loads((data_dir(populated) / "backups" / backup.STATUS).read_text())["ok"] is False


def test_the_admin_endpoint_reports_the_last_run(populated, remote, tmp_path):
    populated.post("/api/logout")                       # `populated` is the logged-in client; become a visitor first
    assert populated.get("/api/admin/backup").status_code == 401
    from conftest import PASSWORD
    populated.post("/api/login", json={"password": PASSWORD})
    assert populated.get("/api/admin/backup").json()["ok"] is None
    run_backup(populated, remote, tmp_path)
    st = populated.get("/api/admin/backup").json()
    assert st["ok"] is True and st["pushed"] is True and st["images"] == 2


def test_branches_whose_name_merely_ends_in_backup_are_not_mistaken_for_it(populated, remote, tmp_path):
    """`git ls-remote --heads origin backup` matches by suffix, so feat/backup used to count as `backup`."""
    work = tmp_path / "other"
    sh("git", "clone", "--quiet", str(remote), str(work))
    for name in ("feat/backup", "old/2026/backup"):
        sh("git", "-C", str(work), "push", "--quiet", "origin", f"HEAD:refs/heads/{name}")
    r = run_backup(populated, remote, tmp_path)
    assert r["ok"] and r["pushed"], r
    assert sh("git", f"--git-dir={remote}", "rev-parse", "--verify", "refs/heads/backup")
    assert tree(remote) == {"snapshot.json", "originals/aaa.png", "originals/bbb.png"}
