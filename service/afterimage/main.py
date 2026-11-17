"""The afterimage web service: public gallery API, owner-only editing and discovery."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import Response as RawResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import auth, backup, discover, feed, store
from .config import Settings, load
from .db import Database
from .sources.base import SourceError, host_allowed

log = logging.getLogger("afterimage")
IMMUTABLE = "public, max-age=31536000, immutable"
EXT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str


class EditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    works: list[str] | None = None
    emotion: str | None = None
    personal: bool | None = None
    reviewed: bool | None = None
    caption: str | None = None


class WorkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    kind: str
    links: dict[str, str] = Field(default_factory=dict)


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    source: str
    source_url: str | None = None
    source_ref: str | None = None
    artist: str | None = None
    work_id: str | None = None
    work_title: str | None = None
    work_kind: str = "game"
    caption: str | None = None
    force: bool = False


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_id: str | None = None
    force: bool = False


async def _backup_loop(db: Database, settings: Settings) -> None:
    while True:
        try:
            target = settings.backups_dir / f"afterimage-{date.today():%Y%m%d}.sqlite"
            if not target.exists():
                await asyncio.to_thread(db.backup, target)
            for old in sorted(settings.backups_dir.glob("afterimage-*.sqlite"))[:-14]:
                old.unlink()
        except Exception:
            log.exception("backup failed")
        await asyncio.sleep(6 * 3600)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        db = Database(settings.db_path)
        http = discover.make_client()
        app.state.settings = settings
        app.state.db = db
        app.state.discover = discover.State(settings=settings, db=db, http=http)
        app.state.limiter = auth.LoginLimiter()
        scanner = feed.Scanner(app.state.discover)
        app.state.scanner = scanner
        if settings.scanner:
            scanner.start()
        task = asyncio.create_task(_backup_loop(db, settings))
        try:
            yield
        finally:
            task.cancel()
            await scanner.stop()
            await http.aclose()
            db.close()

    app = FastAPI(title="afterimage", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    def db_of(request: Request) -> Database:
        return request.app.state.db

    def admin(request: Request) -> bool:
        return auth.is_admin(request.app.state.settings, request)

    # ---- session ----------------------------------------------------------

    @app.get("/healthz")
    def healthz(request: Request):
        n = db_of(request).one("SELECT COUNT(*) n FROM images")["n"]
        return {"status": "ok", "images": n}

    @app.get("/api/me")
    def me(request: Request):
        return {"admin": admin(request)}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response):
        s: Settings = request.app.state.settings
        limiter: auth.LoginLimiter = request.app.state.limiter
        who = auth.client_id(request)
        if limiter.blocked(who):
            raise HTTPException(429, "too many attempts, try again later")
        if not s.admin_password_hash or not s.session_secret or not auth.verify_password(body.password, s.admin_password_hash):
            limiter.fail(who)
            raise HTTPException(401, "wrong password")
        limiter.clear(who)
        response.set_cookie(auth.COOKIE, auth.make_session(s.session_secret), max_age=auth.SESSION_DAYS * 86400,
                            httponly=True, samesite="strict", secure=s.secure_cookies, path="/")
        return {"admin": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(auth.COOKIE, path="/")
        return {"admin": False}

    # ---- collection ---------------------------------------------------------

    @app.get("/api/catalog")
    def catalog(request: Request):
        return JSONResponse(store.catalog(db_of(request), include_personal=admin(request)),
                            headers={"Cache-Control": "private, no-cache"})

    @app.get("/api/wallpapers.json")
    def wallpapers(request: Request):
        # The contract infovore reads. Never includes personal photos.
        return JSONResponse(store.public_wallpapers(db_of(request)), headers={"Cache-Control": "public, max-age=300"})

    @app.patch("/api/images/{image_id}", dependencies=[Depends(auth.require_admin)])
    def edit_image(image_id: str, body: EditBody, request: Request):
        try:
            return store.update_image(db_of(request), image_id, body.model_dump(exclude_unset=True), actor="owner")
        except store.EditError as e:
            raise HTTPException(404 if str(e) == "no such image" else 422, str(e)) from e

    @app.post("/api/works", dependencies=[Depends(auth.require_admin)])
    def create_work(body: WorkBody, request: Request):
        try:
            return store.add_work(db_of(request), body.title, body.kind, body.links)
        except store.EditError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/api/works/{work_id}/resolve-links", dependencies=[Depends(auth.require_admin)])
    async def resolve_links(work_id: str, request: Request):
        db = db_of(request)
        w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
        if not w:
            raise HTTPException(404, "no such work")
        found = await discover.resolve_links(request.app.state.discover, w["title"], w["kind"])
        links = {**json.loads(w["links"]), **found}
        with db.tx() as c:
            c.execute("UPDATE works SET links=? WHERE id=?", (json.dumps(links, ensure_ascii=False), work_id))
        return {"id": work_id, "links": links}

    # ---- image files --------------------------------------------------------

    def image_row(request: Request, image_id: str):
        row = db_of(request).one("SELECT original, caption, personal FROM images WHERE id=?", (image_id,))
        if not row or (row["personal"] and not admin(request)):
            raise HTTPException(404, "not found")
        return row

    @app.get("/thumbs/{image_id}.avif")
    def thumb(image_id: str, request: Request):
        row = image_row(request, image_id)
        f = request.app.state.settings.thumbs_dir / f"{image_id}.avif"
        if not f.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(f, media_type="image/avif", headers={"Cache-Control": "private, no-store" if row["personal"] else IMMUTABLE})

    @app.get("/view/{image_id}.avif")
    def view(image_id: str, request: Request):
        row = image_row(request, image_id)
        f = request.app.state.settings.view_dir / f"{image_id}.avif"
        if not f.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(f, media_type="image/avif", headers={"Cache-Control": "private, no-store" if row["personal"] else IMMUTABLE})

    @app.get("/original/{image_id}")
    def original(image_id: str, request: Request):
        row = image_row(request, image_id)
        f = request.app.state.settings.originals_dir / row["original"]
        if not f.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(f, media_type=EXT_TYPES.get(f.suffix.lower(), "application/octet-stream"),
                            filename=f"{row['caption']}{f.suffix}", content_disposition_type="inline",
                            headers={"Cache-Control": "private, no-store" if row["personal"] else "public, max-age=86400"})

    # ---- discovery (owner only) ---------------------------------------------

    owner = [Depends(auth.require_admin)]

    @app.get("/api/discover/search", dependencies=owner)
    async def search(request: Request, title: str = Query(min_length=1, max_length=120), kind: str = "game",
                     sources: str = "", device: str = Query("desktop", pattern="^(desktop|phone)$"), limit: int = Query(12, ge=1, le=40)):
        names = [s for s in sources.split(",") if s] or None
        return await discover.search_all(request.app.state.discover, title.strip(), kind, names, limit, device)

    @app.post("/api/discover/import", dependencies=owner)
    async def import_one(body: ImportBody, request: Request):
        try:
            return await discover.import_image(request.app.state.discover, **body.model_dump())
        except discover.ImportError_ as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/discover/gaps", dependencies=owner)
    async def gaps(request: Request, days: int = Query(60, ge=1, le=365)):
        try:
            return await discover.gaps(request.app.state.discover, days)
        except SourceError as e:
            raise HTTPException(502, str(e)) from e

    @app.get("/api/discover/feed", dependencies=owner)
    def discover_feed(request: Request, limit: int = Query(36, ge=1, le=100), offset: int = Query(0, ge=0),
                      kind: str | None = None, source: str | None = None, work: str | None = None,
                      device: str | None = Query(None, pattern="^(desktop|phone)$"), uncovered: bool = False):
        """Pictures from every work in the owner's history, best first, never one work in a row."""
        return feed.feed(db_of(request), limit=limit, offset=offset, kind=kind, source=source, device=device, work=work, uncovered=uncovered)

    SNIFF = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"RIFF", "image/webp"))

    @app.get("/api/discover/img", dependencies=owner)
    async def discover_img(request: Request, u: str = Query(min_length=8, max_length=700)):
        """Fetch a candidate's picture for the browser. Some image hosts (Danbooru's CDN) refuse to
        serve browsers hotlinking from another site. Only allowlisted hosts, only real images."""
        if not host_allowed(u):
            raise HTTPException(400, "host not allowed")
        try:
            data = await discover.fetch_bytes(request.app.state.discover, u, 6 * 1048576)
        except discover.ImportError_ as e:
            raise HTTPException(502, str(e)) from e
        kind = next((m for sig, m in SNIFF if data.startswith(sig)), None)
        if not kind or (kind == "image/webp" and data[8:12] != b"WEBP"):
            raise HTTPException(502, "the host did not return an image")   # e.g. a bot-check page
        return RawResponse(data, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/discover/progress", dependencies=owner)
    def discover_progress(request: Request):
        return request.app.state.scanner.progress()

    @app.post("/api/discover/run", dependencies=owner)
    async def run(request: Request, refresh: bool = False):   # async: the event is set from the loop that waits on it
        """Wake the background scan now (refresh=true re-reads the history from infovore first)."""
        sc: feed.Scanner = request.app.state.scanner
        sc.kick(refresh=refresh)
        return sc.progress()

    @app.post("/api/discover/feed/{candidate_id:path}/accept", dependencies=owner)
    async def accept(candidate_id: str, body: AcceptBody, request: Request):
        db = db_of(request)
        c = db.one("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        if not c:
            raise HTTPException(404, "no such candidate")
        try:
            result = await discover.import_image(
                request.app.state.discover, url=c["full_url"], source=c["source"], source_url=c["source_url"], source_ref=c["id"],
                artist=c["artist"], work_id=body.work_id, work_title=None if body.work_id else c["work_query"],
                work_kind=c["work_kind"], force=body.force)
        except discover.ImportError_ as e:
            raise HTTPException(422, str(e)) from e
        if result["status"] in {"imported", "exists"}:
            with db.tx() as tx:
                tx.execute("UPDATE candidates SET status='accepted', decided_at=datetime('now') WHERE id=?", (candidate_id,))
        return result

    @app.post("/api/discover/feed/{candidate_id:path}/reject", dependencies=owner)
    def reject(candidate_id: str, request: Request):
        db = db_of(request)
        with db.tx() as c:
            n = c.execute("UPDATE candidates SET status='rejected', decided_at=datetime('now') WHERE id=?", (candidate_id,)).rowcount
        if not n:
            raise HTTPException(404, "no such candidate")
        return {"status": "rejected"}

    @app.get("/api/admin/backup", dependencies=owner)
    def backup_status(request: Request):
        """What the last off-site backup run reported (written by `python -m afterimage.backup run`)."""
        f = request.app.state.settings.backups_dir / backup.STATUS
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {"ok": None, "note": "no backup has run yet"}

    # ---- frontend -------------------------------------------------------------

    fe = settings.frontend_dir
    if fe and fe.is_dir():
        for route, name in (("/discover", "discover.html"), ("/login", "login.html")):
            if (fe / name).is_file():
                def page(name=name):
                    return FileResponse(fe / name, headers={"Cache-Control": "no-cache"})
                app.add_api_route(route, page, methods=["GET"], include_in_schema=False)
        app.mount("/", StaticFiles(directory=fe, html=True), name="frontend")
    return app

