"""Off-site backup: a readable snapshot of the database plus the original images,
pushed to the `backup` branch of the project repository.

    python -m afterimage.backup run     --data DATA --repo CLONE --remote URL [--ssh-key KEY]
    python -m afterimage.backup restore --repo CLONE --data DATA
    python -m afterimage.backup snapshot --data DATA            # print the snapshot

What is and is not in it
- snapshot.json  works, images, image_works (ordered), similar, candidates, edit log.
                 Plain JSON with sorted keys and no timestamps, so a run with no changes makes no commit.
- originals/     every original that is not marked personal.
- NOT included:  images marked `personal`. The repository is public, so those stay on the server only;
                 the snapshot keeps just {"id", "personal": true} for them. Thumbnails and previews
                 are rebuilt on restore.
Marking an image personal later removes its file from the branch tip, but it stays in git history.

Only the standard library is needed to back up (restore also needs Pillow), so this runs on the host.
Objects the repository already holds (the original 282 images live in master) are not uploaded again:
git sends only what the remote does not have.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
BRANCH = "backup"
SNAPSHOT = "snapshot.json"
STATUS = "last-backup.json"
WARN_MB = 1000   # GitHub recommends repositories under 1 GB
STOP_MB = 2000   # refuse to grow the repository past this


# ---- snapshot ---------------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    return [dict(r) for r in conn.execute(sql).fetchall()]


def snapshot(db_path: Path) -> dict:
    """A consistent copy of the database as plain data. Safe while the service is writing."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    mem = sqlite3.connect(":memory:")
    try:
        src.backup(mem)
    finally:
        src.close()
    mem.row_factory = sqlite3.Row
    personal = {r["id"] for r in mem.execute("SELECT id FROM images WHERE personal=1")}
    keep = lambda i: i not in personal  # noqa: E731
    images = [{"id": r["id"], "personal": True} if r["id"] in personal else r
              for r in _rows(mem, "SELECT * FROM images ORDER BY id")]
    snap = {
        "version": VERSION,
        "works": _rows(mem, "SELECT * FROM works ORDER BY id"),
        "images": images,
        "image_works": [r for r in _rows(mem, "SELECT * FROM image_works ORDER BY image_id, position, work_id") if keep(r["image_id"])],
        "similar": [r for r in _rows(mem, "SELECT * FROM similar ORDER BY image_id, rank") if keep(r["image_id"]) and keep(r["other_id"])],
        "candidates": _rows(mem, "SELECT * FROM candidates ORDER BY id"),
        "edits": [r for r in _rows(mem, "SELECT * FROM edits ORDER BY id") if keep(r["image_id"])],
    }
    mem.close()
    return snap


def pushable(snap: dict) -> dict[str, str]:
    """original file name -> image id, for every image that may leave the server."""
    return {i["original"]: i["id"] for i in snap["images"] if not i.get("personal")}


def dumps(snap: dict) -> str:
    return json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


# ---- writing the working tree -------------------------------------------------------------

def sync(snap: dict, data_dir: Path, repo_dir: Path) -> dict[str, int]:
    """Make the repo working tree match the snapshot: snapshot.json plus originals/."""
    out = repo_dir / "originals"
    out.mkdir(parents=True, exist_ok=True)
    (repo_dir / SNAPSHOT).write_text(dumps(snap), encoding="utf-8")
    want = pushable(snap)
    copied = removed = missing = 0
    for name in want:
        src, dst = data_dir / "originals" / name, out / name
        if not src.is_file():
            missing += 1
        elif not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
            copied += 1
    for f in out.iterdir():
        if f.name not in want:
            f.unlink()
            removed += 1
    return {"copied": copied, "removed": removed, "missing": missing, "images": len(want)}


# ---- restoring ----------------------------------------------------------------------------

def restore(repo_dir: Path, data_dir: Path, log=print) -> dict[str, int]:
    """Rebuild a data directory (database, originals, thumbnails, previews) from a backup checkout."""
    from PIL import Image

    from . import imaging
    from .db import Database

    snap = json.loads((repo_dir / SNAPSHOT).read_text(encoding="utf-8"))
    if snap.get("version") != VERSION:
        raise SystemExit(f"unsupported snapshot version {snap.get('version')!r}")
    for sub in ("originals", "thumbs", "view", "backups"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "afterimage.sqlite"
    if db_path.exists():
        raise SystemExit(f"{db_path} already exists; restore into an empty data directory")
    db = Database(db_path)

    def insert(c: sqlite3.Connection, table: str, rows: list[dict]) -> None:
        for r in rows:
            cols = list(r)
            c.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [r[k] for k in cols])

    counts = {"images": 0, "personal_skipped": 0, "files_missing": 0, "derivatives_built": 0}
    with db.tx() as c:
        insert(c, "works", snap["works"])
        full = [i for i in snap["images"] if not i.get("personal")]
        have = {i["id"] for i in full if (repo_dir / "originals" / i["original"]).is_file()}
        counts["files_missing"] = len(full) - len(have)
        counts["personal_skipped"] = len(snap["images"]) - len(full)
        insert(c, "images", [i for i in full if i["id"] in have])
        insert(c, "image_works", [r for r in snap["image_works"] if r["image_id"] in have])
        insert(c, "similar", [r for r in snap["similar"] if r["image_id"] in have and r["other_id"] in have])
        insert(c, "candidates", snap["candidates"])
        insert(c, "edits", [r for r in snap["edits"] if r["image_id"] in have])
        counts["images"] = len(have)
    for i in full:
        if i["id"] not in have:
            continue
        shutil.copy2(repo_dir / "originals" / i["original"], data_dir / "originals" / i["original"])
        if not ((data_dir / "thumbs" / f"{i['id']}.avif").is_file() and (data_dir / "view" / f"{i['id']}.avif").is_file()):
            with Image.open(data_dir / "originals" / i["original"]) as im:
                im.load()
                imaging.write_derivatives(im, i["id"], data_dir / "thumbs", data_dir / "view")
            counts["derivatives_built"] += 1
    db.close()
    return counts


# ---- git ------------------------------------------------------------------------------------

class BackupError(Exception):
    pass


def _env(ssh_key: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if ssh_key:
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    return env


def _git(repo: Path, *args: str, env: dict[str, str], check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise BackupError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout.strip()


def repo_size_mb(repo: Path, env: dict[str, str]) -> float:
    kv = dict(line.split(": ", 1) for line in _git(repo, "count-objects", "-v", env=env).splitlines() if ": " in line)
    return (int(kv.get("size", 0)) + int(kv.get("size-pack", 0))) / 1024


def prepare_repo(repo: Path, remote: str, env: dict[str, str]) -> None:
    """Clone if needed and put the working tree on the backup branch (created empty if new)."""
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "--quiet", remote, str(repo)], capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise BackupError(f"clone failed: {r.stderr.strip()[:400]}")
    _git(repo, "config", "user.name", "afterimage backup", env=env)
    _git(repo, "config", "user.email", "backup@afterimage.skyhong.tw", env=env)
    _git(repo, "fetch", "--quiet", "origin", env=env)
    if _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}", env=env):   # full name: a bare name matches by suffix
        _git(repo, "checkout", "--quiet", "-B", BRANCH, f"origin/{BRANCH}", env=env)
    elif _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}", env=env, check=False):
        _git(repo, "checkout", "--quiet", BRANCH, env=env)
    else:
        _git(repo, "checkout", "--quiet", "--orphan", BRANCH, env=env)
        _git(repo, "rm", "-rf", "--quiet", "--ignore-unmatch", ".", env=env, check=False)


def run(data_dir: Path, repo: Path, remote: str, ssh_key: str | None = None, warn_mb: int = WARN_MB, stop_mb: int = STOP_MB) -> dict:
    env = _env(ssh_key)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result: dict = {"at": stamp, "ok": False}
    try:
        prepare_repo(repo, remote, env)
        snap = snapshot(data_dir / "afterimage.sqlite")
        result.update(sync(snap, data_dir, repo))
        _git(repo, "add", "-A", env=env)
        changed = bool(_git(repo, "status", "--porcelain", env=env))
        size = repo_size_mb(repo, env)
        result["repo_mb"] = round(size, 1)
        if changed:
            if size >= stop_mb:
                raise BackupError(f"repository is {size:.0f} MB (limit {stop_mb} MB); not pushing")
            n = len(pushable(snap))
            _git(repo, "commit", "--quiet", "-m", f"backup {stamp}: {n} images", env=env)
            _git(repo, "push", "--quiet", "origin", f"{BRANCH}:{BRANCH}", env=env)
        result.update(ok=True, pushed=changed, commit=_git(repo, "rev-parse", "--short", "HEAD", env=env))
        if size >= warn_mb:
            result["warning"] = f"repository is {size:.0f} MB; GitHub recommends staying under {warn_mb} MB"
    except BackupError as e:
        result["error"] = str(e)
    except Exception as e:  # never leave without a status the service can show
        result["error"] = f"{type(e).__name__}: {e}"
    status_dir = data_dir / "backups"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / STATUS).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(prog="afterimage.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data", required=True, type=Path)
    r.add_argument("--repo", required=True, type=Path)
    r.add_argument("--remote", required=True)
    r.add_argument("--ssh-key")
    q = sub.add_parser("restore")
    q.add_argument("--repo", required=True, type=Path)
    q.add_argument("--data", required=True, type=Path)
    s = sub.add_parser("snapshot")
    s.add_argument("--data", required=True, type=Path)
    a = ap.parse_args()
    if a.cmd == "run":
        res = run(a.data, a.repo, a.remote, a.ssh_key)
        print(json.dumps(res, indent=1))
        sys.exit(0 if res["ok"] else 1)
    if a.cmd == "restore":
        print(json.dumps(restore(a.repo, a.data), indent=1))
    else:
        sys.stdout.write(dumps(snapshot(a.data / "afterimage.sqlite")))


if __name__ == "__main__":
    main()
