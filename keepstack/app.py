"""Keepstack HTTP application (FastAPI).

Routes are grouped by concern: auth, assets, collections, tags, search,
sharing, administration, and open-standard endpoints (OAI-PMH, IIIF). The
single-page web client is served from the ``web/`` directory at the root.
"""
from __future__ import annotations

import csv
import io
import ipaddress
import json
from pathlib import Path
from typing import Optional

from fastapi import (Body, Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response)
from fastapi.staticfiles import StaticFiles

from . import __version__, ingest, search, standards, storage, thumbnails
from .audit import log, now_iso
from .auth import (authenticate, hash_password, login_locked, make_token,
                   note_login_failure, note_login_success, role_at_least,
                   verify_password, verify_token)
from .config import config
from .db import get_conn, init_db, transaction

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Keepstack DAM", version="0.1.0")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _startup() -> None:
    init_db()
    conn = get_conn()
    has_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if not has_users:
        conn.execute(
            "INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
            "VALUES (?,?,?,?,?,1,?)",
            (config.admin_user, config.admin_email, "Administrator",
             hash_password(config.admin_password), "admin", now_iso()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------
def optional_user(request: Request) -> Optional[dict]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = verify_token(auth[7:])
    if not payload:
        return None
    row = get_conn().execute(
        "SELECT * FROM users WHERE id = ? AND is_active = 1", (payload["sub"],)
    ).fetchone()
    return dict(row) if row else None


def require_user(request: Request) -> dict:
    user = optional_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def require_role(minimum: str):
    def dep(request: Request) -> dict:
        user = require_user(request)
        if not role_at_least(user["role"], minimum):
            raise HTTPException(403, f"Requires {minimum} role")
        return user
    return dep


def _trusted_proxy_nets():
    nets = []
    for spec in config.trusted_proxies:
        try:
            nets.append(ipaddress.ip_network(spec, strict=False))
        except ValueError:
            continue
    return nets


def _ip_in(ip: str, nets) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def client_ip(request: Request) -> str:
    """Resolve the client IP, trusting X-Forwarded-For only from a configured
    proxy. Without trusted proxies the direct peer address is authoritative, so
    a directly-connected client cannot spoof its IP to dodge rate limiting."""
    peer = request.client.host if request.client else ""
    nets = _trusted_proxy_nets()
    if nets and _ip_in(peer, nets):
        fwd = request.headers.get("X-Forwarded-For", "")
        for hop in reversed([h.strip() for h in fwd.split(",") if h.strip()]):
            if not _ip_in(hop, nets):
                return hop
    return peer


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.post("/api/auth/login")
def login(request: Request, body: dict = Body(...)):
    username = body.get("username", "")
    ip = client_ip(request)
    remaining = login_locked(username, ip)
    if remaining > 0:
        raise HTTPException(
            429, "Too many failed attempts, try again later",
            headers={"Retry-After": str(int(remaining) + 1)},
        )
    user = authenticate(username, body.get("password", ""))
    if not user:
        if note_login_failure(username, ip):
            log("login.lockout", detail=username, ip=ip)
        log("login.fail", detail=username, ip=ip)
        raise HTTPException(401, "Invalid credentials")
    note_login_success(username, ip)
    get_conn().execute("UPDATE users SET last_login = ? WHERE id = ?", (now_iso(), user["id"]))
    get_conn().commit()
    log("login.success", user=user, ip=ip)
    return {"token": make_token(user["id"], user["role"]), "user": _user_public(user)}


@app.get("/api/auth/me")
def me(user: dict = Depends(require_user)):
    return _user_public(user)


def _user_public(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "email": u["email"],
            "full_name": u["full_name"], "role": u["role"]}


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
@app.post("/api/assets")
async def upload_assets(
    request: Request,
    files: list[UploadFile] = File(...),
    run_ai: bool = Form(True),
    user: dict = Depends(require_role("contributor")),
):
    results = []
    for f in files:
        data = await f.read()
        asset = ingest.ingest_stream(io.BytesIO(data), f.filename or "upload.bin",
                                     user=user, run_ai=run_ai)
        if not asset.get("duplicate"):
            log("asset.create", user=user, target_type="asset", target_id=asset["uuid"],
                detail=asset["filename"], ip=client_ip(request))
        results.append({"uuid": asset["uuid"], "title": asset["title"],
                        "duplicate": asset.get("duplicate", False),
                        "media_type": asset["media_type"]})
    return {"created": results}


@app.get("/api/assets")
def list_assets(
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tag: list[str] = Query(default=[]),
    exclude_tag: list[str] = Query(default=[]),
    collection_id: Optional[int] = None,
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc)$"),
    mode: str = "keyword",
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
):
    return search.search(
        q=q,
        media_type=media_type,
        tag=tag,
        include_tags=include_tag,
        exclude_tags=exclude_tag,
        collection_id=collection_id,
        status="active",
        sort=sort,
        mode=mode,
        limit=limit,
        offset=offset,
    )


@app.get("/api/assets/export.csv")
def export_assets_csv(
    q: str = "",
    media_type: Optional[str] = None,
    include_tag: list[str] = Query(default=[]),
    exclude_tag: list[str] = Query(default=[]),
    collection_id: Optional[int] = None,
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc)$"),
    user: dict = Depends(require_user),
):
    """Export every asset matching the current keyword filters and sort."""
    assets = search.all_matching_assets(
        q=q,
        media_type=media_type,
        include_tags=include_tag,
        exclude_tags=exclude_tag,
        collection_id=collection_id,
        sort=sort,
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    fields = [
        "uuid",
        "title",
        "description",
        "filename",
        "media_type",
        "tags",
        "created_at",
    ]
    writer.writerow(fields)
    for asset in assets:
        writer.writerow([asset.get(field) or "" for field in fields])

    # A UTF-8 BOM makes Chinese content open correctly in common spreadsheet
    # programs while remaining valid UTF-8 for standards-compliant readers.
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="keepstack-assets.csv"'},
    )


def _get_asset_or_404(uuid: str) -> dict:
    row = get_conn().execute("SELECT * FROM assets WHERE uuid = ?", (uuid,)).fetchone()
    if not row:
        raise HTTPException(404, "Asset not found")
    return dict(row)


@app.get("/api/assets/{uuid}")
def asset_detail(uuid: str, user: dict = Depends(require_user)):
    a = _get_asset_or_404(uuid)
    conn = get_conn()
    tags = [dict(r) for r in conn.execute(
        "SELECT t.name, at.source, at.confidence FROM tags t "
        "JOIN asset_tags at ON at.tag_id = t.id WHERE at.asset_id = ? ORDER BY t.name",
        (a["id"],))]
    versions = [dict(r) for r in conn.execute(
        "SELECT version_no, sha256, size, note, created_at FROM asset_versions "
        "WHERE asset_id = ? ORDER BY version_no DESC", (a["id"],))]
    collections = [dict(r) for r in conn.execute(
        "SELECT c.id, c.name FROM collections c JOIN collection_assets ca ON ca.collection_id = c.id "
        "WHERE ca.asset_id = ?", (a["id"],))]
    custom = [dict(r) for r in conn.execute(
        "SELECT cf.key, cf.label, acv.value FROM asset_custom_values acv "
        "JOIN custom_fields cf ON cf.id = acv.field_id WHERE acv.asset_id = ?", (a["id"],))]
    a.pop("embedding", None)
    a["dublin_core"] = standards.dublin_core(a)
    return {"asset": a, "tags": tags, "versions": versions,
            "collections": collections, "custom": custom}


@app.patch("/api/assets/{uuid}")
def update_asset(uuid: str, request: Request, body: dict = Body(...),
                 user: dict = Depends(require_role("contributor"))):
    a = _get_asset_or_404(uuid)
    editable = {"title", "description", "alt_text", "license", "rights_statement",
                "credit", "usage_expiry", "retention_until", "dc_creator",
                "dc_subject", "dc_publisher", "dc_date", "status"}
    sets, vals = [], []
    for k, v in body.items():
        if k in editable:
            sets.append(f"{k} = ?")
            vals.append(v)
    if sets:
        sets.append("updated_at = ?")
        vals.extend([now_iso(), a["id"]])
        with transaction() as conn:
            conn.execute(f"UPDATE assets SET {', '.join(sets)} WHERE id = ?", vals)
        ingest.reindex_fts(a["id"])
        ingest.reindex_embedding(a["id"])
        log("asset.update", user=user, target_type="asset", target_id=uuid,
            detail=",".join(body.keys()), ip=client_ip(request))
    return asset_detail(uuid, user)


@app.delete("/api/assets/{uuid}")
def delete_asset(uuid: str, request: Request, user: dict = Depends(require_role("editor"))):
    a = _get_asset_or_404(uuid)
    with transaction() as conn:
        conn.execute("UPDATE assets SET status = 'deleted', updated_at = ? WHERE id = ?",
                     (now_iso(), a["id"]))
    log("asset.delete", user=user, target_type="asset", target_id=uuid, ip=client_ip(request))
    return {"ok": True}


@app.get("/api/assets/{uuid}/file")
def asset_file(uuid: str, user: dict = Depends(require_user)):
    a = _get_asset_or_404(uuid)
    path = storage.blob_path(a["storage_key"])
    if not path.exists():
        raise HTTPException(410, "Blob missing")
    return FileResponse(path, media_type=a["mime_type"], filename=a["filename"])


@app.get("/api/assets/{uuid}/thumb")
def asset_thumb(uuid: str):
    a = _get_asset_or_404(uuid)
    key = a["thumb_key"] or thumbnails.ensure_thumbnail(
        a["sha256"], a["storage_key"], a["media_type"], a["ext"])
    path = storage.thumb_path(key)
    if not path.exists():
        thumbnails.ensure_thumbnail(a["sha256"], a["storage_key"], a["media_type"], a["ext"])
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/assets/{uuid}/preview")
def asset_preview(uuid: str, size: int = Query(1600, le=4000)):
    a = _get_asset_or_404(uuid)
    data = thumbnails.rendition(a["sha256"], a["storage_key"], a["media_type"], a["ext"], size)
    if data is None:
        return asset_thumb(uuid)
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/assets/{uuid}/similar")
def asset_similar(uuid: str, user: dict = Depends(require_user)):
    a = _get_asset_or_404(uuid)
    return {"items": search.more_like(a["id"])}


@app.post("/api/assets/{uuid}/tags")
def add_tag(uuid: str, request: Request, body: dict = Body(...),
            user: dict = Depends(require_role("contributor"))):
    a = _get_asset_or_404(uuid)
    name = (body.get("name") or "").strip().lower()
    if not name:
        raise HTTPException(400, "Tag name required")
    with transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tid = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?,?)",
                     (a["id"], tid, "manual"))
    ingest.reindex_fts(a["id"])
    ingest.reindex_embedding(a["id"])
    log("tag.add", user=user, target_type="asset", target_id=uuid, detail=name)
    return {"ok": True}


@app.delete("/api/assets/{uuid}/tags/{name}")
def remove_tag(uuid: str, name: str, user: dict = Depends(require_role("contributor"))):
    a = _get_asset_or_404(uuid)
    with transaction() as conn:
        conn.execute(
            "DELETE FROM asset_tags WHERE asset_id = ? AND tag_id = "
            "(SELECT id FROM tags WHERE name = ?)", (a["id"], name.lower()))
    ingest.reindex_fts(a["id"])
    ingest.reindex_embedding(a["id"])
    return {"ok": True}


@app.post("/api/assets/{uuid}/versions")
async def add_version(uuid: str, request: Request, file: UploadFile = File(...),
                      note: str = Form(""), user: dict = Depends(require_role("editor"))):
    a = _get_asset_or_404(uuid)
    data = await file.read()
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else a["ext"]
    sha, key, size = storage.store_bytes(data, ext)
    conn = get_conn()
    last = conn.execute("SELECT COALESCE(MAX(version_no),0) m FROM asset_versions WHERE asset_id = ?",
                        (a["id"],)).fetchone()["m"]
    with transaction() as conn:
        # snapshot the current version, then promote the new bytes to current
        conn.execute(
            "INSERT INTO asset_versions (asset_id, version_no, sha256, storage_key, size, note, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (a["id"], last + 1, a["sha256"], a["storage_key"], a["size"],
             "previous current", user["id"], now_iso()))
        conn.execute(
            "INSERT INTO asset_versions (asset_id, version_no, sha256, storage_key, size, note, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (a["id"], last + 2, sha, key, size, note or "new version", user["id"], now_iso()))
        conn.execute("UPDATE assets SET sha256 = ?, storage_key = ?, size = ?, updated_at = ? WHERE id = ?",
                     (sha, key, size, now_iso(), a["id"]))
    thumbnails.ensure_thumbnail(sha, key, a["media_type"], ext)
    log("asset.version", user=user, target_type="asset", target_id=uuid, detail=note)
    return {"ok": True, "version": last + 2}


@app.get("/api/assets/{uuid}/metadata")
def asset_metadata(uuid: str, user: dict = Depends(require_user)):
    a = _get_asset_or_404(uuid)
    row = get_conn().execute("SELECT * FROM asset_metadata WHERE asset_id = ?", (a["id"],)).fetchone()
    if not row:
        return {"exif": {}, "iptc": {}, "xmp": {}}
    return {"exif": json.loads(row["exif_json"] or "{}"),
            "iptc": json.loads(row["iptc_json"] or "{}"),
            "xmp": json.loads(row["xmp_json"] or "{}")}


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
@app.get("/api/tags")
def list_tags(user: dict = Depends(require_user)):
    rows = get_conn().execute(
        "SELECT t.name, COUNT(at.asset_id) c FROM tags t "
        "LEFT JOIN asset_tags at ON at.tag_id = t.id GROUP BY t.name ORDER BY c DESC, t.name").fetchall()
    return {"tags": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------
@app.get("/api/collections")
def list_collections(user: dict = Depends(require_user)):
    rows = get_conn().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM collection_assets ca WHERE ca.collection_id = c.id) AS asset_count "
        "FROM collections c ORDER BY c.name").fetchall()
    return {"collections": [dict(r) for r in rows]}


@app.post("/api/collections")
def create_collection(request: Request, body: dict = Body(...),
                      user: dict = Depends(require_role("contributor"))):
    import uuid as uuidlib
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    cid = str(uuidlib.uuid4())
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO collections (uuid, name, description, parent_id, kind, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, name, body.get("description"), body.get("parent_id"),
             body.get("kind", "folder"), user["id"], now_iso()))
    log("collection.create", user=user, target_type="collection", target_id=cid, detail=name)
    return {"id": cur.lastrowid, "uuid": cid, "name": name}


@app.delete("/api/collections/{cid}")
def delete_collection(cid: int, user: dict = Depends(require_role("editor"))):
    with transaction() as conn:
        conn.execute("DELETE FROM collections WHERE id = ?", (cid,))
    log("collection.delete", user=user, target_type="collection", target_id=cid)
    return {"ok": True}


@app.post("/api/collections/{cid}/assets")
def add_to_collection(cid: int, body: dict = Body(...),
                      user: dict = Depends(require_role("contributor"))):
    uuids = body.get("uuids", [])
    conn = get_conn()
    added = 0
    with transaction() as conn:
        for u in uuids:
            row = conn.execute("SELECT id FROM assets WHERE uuid = ?", (u,)).fetchone()
            if row:
                conn.execute(
                    "INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) VALUES (?,?,?)",
                    (cid, row["id"], now_iso()))
                added += 1
    log("collection.add", user=user, target_type="collection", target_id=cid, detail=f"{added} assets")
    return {"ok": True, "added": added}


@app.delete("/api/collections/{cid}/assets/{uuid}")
def remove_from_collection(cid: int, uuid: str, user: dict = Depends(require_role("contributor"))):
    with transaction() as conn:
        conn.execute(
            "DELETE FROM collection_assets WHERE collection_id = ? AND asset_id = "
            "(SELECT id FROM assets WHERE uuid = ?)", (cid, uuid))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sharing (public, tokenised, expiring links)
# ---------------------------------------------------------------------------
@app.post("/api/shares")
def create_share(request: Request, body: dict = Body(...),
                 user: dict = Depends(require_role("contributor"))):
    import secrets
    token = secrets.token_urlsafe(16)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO share_links (token, target_type, target_id, permission, expires_at, "
            "max_downloads, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (token, body.get("target_type", "asset"), body["target_id"],
             body.get("permission", "view"), body.get("expires_at"),
             body.get("max_downloads"), user["id"], now_iso()))
    log("share.create", user=user, target_type=body.get("target_type"), target_id=body["target_id"])
    return {"token": token, "url": f"{config.base_url}/s/{token}"}


@app.get("/api/shares")
def list_shares(user: dict = Depends(require_role("contributor"))):
    rows = get_conn().execute(
        "SELECT id, token, target_type, target_id, permission, expires_at, "
        "download_count, max_downloads, revoked, created_at FROM share_links "
        "WHERE revoked = 0 ORDER BY created_at DESC").fetchall()
    return {"shares": [dict(r) for r in rows]}


@app.delete("/api/shares/{sid}")
def revoke_share(sid: int, user: dict = Depends(require_role("contributor"))):
    with transaction() as conn:
        conn.execute("UPDATE share_links SET revoked = 1 WHERE id = ?", (sid,))
    log("share.revoke", user=user, target_type="share", target_id=sid)
    return {"ok": True}


def _valid_share(token: str) -> dict:
    row = get_conn().execute("SELECT * FROM share_links WHERE token = ? AND revoked = 0",
                             (token,)).fetchone()
    if not row:
        raise HTTPException(404, "Invalid or revoked link")
    s = dict(row)
    if s["expires_at"] and s["expires_at"] < now_iso():
        raise HTTPException(410, "Link expired")
    if s["max_downloads"] and s["download_count"] >= s["max_downloads"]:
        raise HTTPException(410, "Download limit reached")
    return s


@app.get("/s/{token}", response_class=HTMLResponse)
def public_share(token: str):
    s = _valid_share(token)
    a = get_conn().execute("SELECT * FROM assets WHERE id = ?", (s["target_id"],)).fetchone()
    if not a:
        raise HTTPException(404, "Asset unavailable")
    a = dict(a)
    can_dl = s["permission"] == "download"
    dl = (f'<a class="btn" href="/s/{token}/file">Download original</a>' if can_dl else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{a['title']} - Shared via Keepstack</title>
<style>body{{margin:0;font-family:system-ui,sans-serif;background:#f4f6fa;color:#1d2433;
display:flex;flex-direction:column;align-items:center;gap:24px;padding:48px 16px}}
img{{max-width:min(900px,92vw);border-radius:12px;border:1px solid #dde3ec}}
.btn{{background:#4d68d6;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none}}
.muted{{color:#64708a}}</style></head>
<body><h2>{a['title']}</h2>
<img src="/s/{token}/preview" alt="{a.get('alt_text') or a['title']}">
<p class="muted">{a.get('description') or ''}</p>{dl}
<p class="muted">Shared securely via Keepstack</p></body></html>"""


@app.get("/s/{token}/preview")
def public_share_preview(token: str):
    s = _valid_share(token)
    a = dict(get_conn().execute("SELECT * FROM assets WHERE id = ?", (s["target_id"],)).fetchone())
    data = thumbnails.rendition(a["sha256"], a["storage_key"], a["media_type"], a["ext"], 1600)
    if data is None:
        key = a["thumb_key"]
        return FileResponse(storage.thumb_path(key), media_type="image/jpeg")
    return Response(content=data, media_type="image/jpeg")


@app.get("/s/{token}/file")
def public_share_file(token: str, request: Request):
    s = _valid_share(token)
    if s["permission"] != "download":
        raise HTTPException(403, "Download not permitted")
    a = dict(get_conn().execute("SELECT * FROM assets WHERE id = ?", (s["target_id"],)).fetchone())
    with transaction() as conn:
        conn.execute("UPDATE share_links SET download_count = download_count + 1 WHERE id = ?", (s["id"],))
    log("share.download", target_type="asset", target_id=a["uuid"], detail=token, ip=client_ip(request))
    return FileResponse(storage.blob_path(a["storage_key"]), media_type=a["mime_type"],
                        filename=a["filename"])


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------
@app.get("/api/stats")
def stats(user: dict = Depends(require_user)):
    conn = get_conn()
    by_type = {r["media_type"]: r["c"] for r in conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status='active' GROUP BY media_type")}
    total = conn.execute("SELECT COUNT(*) c FROM assets WHERE status='active'").fetchone()["c"]
    size = conn.execute("SELECT COALESCE(SUM(size),0) s FROM assets WHERE status='active'").fetchone()["s"]
    tags = conn.execute("SELECT COUNT(*) c FROM tags").fetchone()["c"]
    cols = conn.execute("SELECT COUNT(*) c FROM collections").fetchone()["c"]
    # storage saved through dedup: bytes that would exist without content-addressing
    dup = conn.execute(
        "SELECT COALESCE(SUM(size),0) s FROM (SELECT sha256, size, COUNT(*) n "
        "FROM assets GROUP BY sha256 HAVING n > 1)").fetchone()["s"]
    return {"total_assets": total, "total_bytes": size, "by_type": by_type,
            "tags": tags, "collections": cols, "dedup_saved_bytes": dup}


@app.get("/api/users")
def list_users(user: dict = Depends(require_role("admin"))):
    rows = get_conn().execute(
        "SELECT id, username, email, full_name, role, is_active, created_at, last_login FROM users "
        "ORDER BY username").fetchall()
    return {"users": [dict(r) for r in rows]}


@app.post("/api/users")
def create_user(request: Request, body: dict = Body(...), user: dict = Depends(require_role("admin"))):
    try:
        with transaction() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
                "VALUES (?,?,?,?,?,1,?)",
                (body["username"], body.get("email"), body.get("full_name"),
                 hash_password(body["password"]), body.get("role", "viewer"), now_iso()))
    except Exception:
        raise HTTPException(409, "Username already exists")
    log("user.create", user=user, target_type="user", target_id=body["username"])
    return {"id": cur.lastrowid}


@app.patch("/api/users/{uid}")
def update_user(uid: int, body: dict = Body(...), user: dict = Depends(require_role("admin"))):
    sets, vals = [], []
    for k in ("email", "full_name", "role", "is_active"):
        if k in body:
            sets.append(f"{k} = ?"); vals.append(body[k])
    if body.get("password"):
        sets.append("password_hash = ?"); vals.append(hash_password(body["password"]))
    if sets:
        vals.append(uid)
        with transaction() as conn:
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", vals)
    log("user.update", user=user, target_type="user", target_id=uid)
    return {"ok": True}


@app.get("/api/audit")
def audit_log(limit: int = Query(100, le=1000), user: dict = Depends(require_role("admin"))):
    rows = get_conn().execute(
        "SELECT ts, username, action, target_type, target_id, detail, ip FROM audit_log "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"entries": [dict(r) for r in rows]}


@app.get("/api/custom-fields")
def list_custom_fields(user: dict = Depends(require_user)):
    rows = get_conn().execute("SELECT * FROM custom_fields ORDER BY sort_order, label").fetchall()
    return {"fields": [dict(r) for r in rows]}


@app.post("/api/custom-fields")
def create_custom_field(body: dict = Body(...), user: dict = Depends(require_role("admin"))):
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO custom_fields (key, label, field_type, options_json, required, sort_order) "
            "VALUES (?,?,?,?,?,?)",
            (body["key"], body["label"], body.get("field_type", "text"),
             json.dumps(body.get("options")) if body.get("options") else None,
             1 if body.get("required") else 0, body.get("sort_order", 0)))
    return {"id": cur.lastrowid}


@app.put("/api/assets/{uuid}/custom/{field_id}")
def set_custom_value(uuid: str, field_id: int, body: dict = Body(...),
                     user: dict = Depends(require_role("contributor"))):
    a = _get_asset_or_404(uuid)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO asset_custom_values (asset_id, field_id, value) VALUES (?,?,?) "
            "ON CONFLICT(asset_id, field_id) DO UPDATE SET value = excluded.value",
            (a["id"], field_id, body.get("value")))
    ingest.reindex_fts(a["id"])
    return {"ok": True}


@app.post("/api/admin/fixity")
def fixity_check(user: dict = Depends(require_role("admin"))):
    """Re-hash every blob and report integrity failures (digital preservation)."""
    conn = get_conn()
    rows = conn.execute("SELECT uuid, sha256, storage_key FROM assets WHERE status != 'deleted'").fetchall()
    failures = []
    for r in rows:
        if not storage.verify_fixity(r["storage_key"], r["sha256"]):
            failures.append(r["uuid"])
    log("admin.fixity", user=user, detail=f"{len(failures)} failures of {len(rows)}")
    return {"checked": len(rows), "failures": failures, "ok": not failures}


# ---------------------------------------------------------------------------
# Open standards: OAI-PMH + IIIF
# ---------------------------------------------------------------------------
@app.get("/oai")
def oai(request: Request):
    body, status = standards.oai_response(dict(request.query_params))
    return Response(content=body, media_type="application/xml", status_code=status)


@app.get("/iiif/3/{uuid}/info.json")
def iiif_info(uuid: str):
    a = _get_asset_or_404(uuid)
    return JSONResponse(standards.iiif_info(a))


@app.get("/iiif/3/{uuid}/manifest.json")
def iiif_manifest(uuid: str):
    a = _get_asset_or_404(uuid)
    if a["media_type"] != "image":
        raise HTTPException(415, "IIIF manifests are available for images only")
    return JSONResponse(standards.iiif_manifest(a))


@app.get("/iiif/3/{uuid}/{region}/{size}/{rotation}/{quality}.{fmt}")
def iiif_image(uuid: str, region: str, size: str, rotation: str, quality: str, fmt: str):
    a = _get_asset_or_404(uuid)
    if fmt.lower() not in ("jpg", "jpeg"):
        raise HTTPException(400, "Only JPEG IIIF output is supported")
    try:
        data = thumbnails.iiif_rendition(
            a["sha256"], a["storage_key"], a["media_type"], region, size, rotation
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if data is None:
        raise HTTPException(415, "IIIF available for images only")
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/version")
def version():
    """Report the running version (unauthenticated, for monitoring)."""
    return {"name": "keepstack", "version": __version__}


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# ---------------------------------------------------------------------------
# Static SPA (registered last so API routes win)
# ---------------------------------------------------------------------------
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
