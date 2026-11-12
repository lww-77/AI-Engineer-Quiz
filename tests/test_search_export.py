"""Acceptance tests for combined search, stable paging, and CSV export."""
from __future__ import annotations

import csv
import io
import uuid

import pytest
from fastapi import HTTPException, Request

from keepstack import ingest, search
from keepstack.app import (add_tag, app, export_assets_csv, list_assets,
                           require_user, update_asset)
from keepstack.auth import hash_password, make_token
from keepstack.db import get_conn, init_db


def _request(headers: list[tuple[bytes, bytes]] | None = None):
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers or [],
        "client": ("127.0.0.1", 1234),
    })


def _insert_asset(*, title: str, description: str, created_at: str, status: str = "active"):
    conn = get_conn()
    asset_uuid = f"quiz-{uuid.uuid4()}"
    cursor = conn.execute(
        """INSERT INTO assets
           (uuid, sha256, filename, mime_type, media_type, ext, size, title,
            description, status, storage_key, created_at, updated_at)
           VALUES (?, ?, ?, 'image/jpeg', 'image', 'jpg', 100, ?, ?, ?, ?, ?, ?)""",
        (
            asset_uuid,
            uuid.uuid4().hex,
            f"{asset_uuid}.jpg",
            title,
            description,
            status,
            f"quiz/{asset_uuid}.jpg",
            created_at,
            created_at,
        ),
    )
    conn.commit()
    ingest.reindex_fts(cursor.lastrowid)
    return cursor.lastrowid, asset_uuid


def _add_tags(asset_id: int, *names: str):
    conn = get_conn()
    for name in names:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?, ?, 'manual')",
            (asset_id, tag_id),
        )
    conn.commit()
    ingest.reindex_fts(asset_id)


@pytest.fixture
def catalog():
    init_db()
    conn = get_conn()
    conn.execute("DELETE FROM asset_fts WHERE rowid IN (SELECT id FROM assets WHERE uuid LIKE 'quiz-%')")
    conn.execute("DELETE FROM assets WHERE uuid LIKE 'quiz-%'")
    conn.execute("DELETE FROM collections WHERE uuid LIKE 'quiz-%'")
    conn.commit()

    collection_uuid = f"quiz-{uuid.uuid4()}"
    collection_id = conn.execute(
        "INSERT INTO collections (uuid, name, kind, created_at) VALUES (?, 'Quiz campaign', 'folder', ?)",
        (collection_uuid, "2026-01-01T00:00:00+00:00"),
    ).lastrowid

    alpha_id, alpha_uuid = _insert_asset(
        title='Harbor "launch", 中文',
        description="First line, with comma\nSecond line with 中文",
        created_at="2026-01-01T10:00:00+00:00",
    )
    beta_id, beta_uuid = _insert_asset(
        title="Harbor internal draft",
        description="Must be excluded",
        created_at="2026-01-02T10:00:00+00:00",
    )
    gamma_id, gamma_uuid = _insert_asset(
        title="Harbor awaiting campaign",
        description="Missing one required tag",
        created_at="2026-01-03T10:00:00+00:00",
    )
    outside_id, outside_uuid = _insert_asset(
        title="Harbor outside collection",
        description="Correct tags but wrong collection",
        created_at="2026-01-04T10:00:00+00:00",
    )
    deleted_id, _ = _insert_asset(
        title="Harbor deleted",
        description="Never returned",
        created_at="2026-01-05T10:00:00+00:00",
        status="deleted",
    )

    _add_tags(alpha_id, "approved", "campaign")
    _add_tags(beta_id, "approved", "campaign", "internal")
    _add_tags(gamma_id, "approved")
    _add_tags(outside_id, "approved", "campaign")
    _add_tags(deleted_id, "approved", "campaign")
    for asset_id in (alpha_id, beta_id, gamma_id, deleted_id):
        conn.execute(
            "INSERT INTO collection_assets (collection_id, asset_id, added_at) VALUES (?, ?, ?)",
            (collection_id, asset_id, "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()

    user = conn.execute("SELECT * FROM users WHERE username = 'quiz-reviewer'").fetchone()
    if not user:
        conn.execute(
            """INSERT INTO users
               (username, password_hash, role, is_active, created_at)
               VALUES ('quiz-reviewer', ?, 'admin', 1, ?)""",
            (hash_password("test-only"), "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE username = 'quiz-reviewer'").fetchone()

    yield {
        "collection_id": collection_id,
        "alpha_id": alpha_id,
        "alpha_uuid": alpha_uuid,
        "beta_uuid": beta_uuid,
        "gamma_uuid": gamma_uuid,
        "outside_uuid": outside_uuid,
        "headers": {"Authorization": f"Bearer {make_token(user['id'], user['role'])}"},
    }


def _business_filters(catalog):
    return {
        "q": "harbor",
        "collection_id": catalog["collection_id"],
        "include_tags": ["approved", "campaign"],
        "exclude_tags": ["internal"],
    }


def test_combined_filters_require_all_include_tags_and_exclude_any(catalog):
    result = search.search(**_business_filters(catalog))
    assert result["total"] == 1
    assert [item["uuid"] for item in result["items"]] == [catalog["alpha_uuid"]]


def test_duplicate_tags_and_include_exclude_conflict(catalog):
    filters = _business_filters(catalog)
    filters["include_tags"] = ["approved", "campaign", "APPROVED"]
    assert search.search(**filters)["total"] == 1
    filters["exclude_tags"] = ["internal", "approved"]
    assert search.search(**filters)["total"] == 0


def test_stable_multi_page_results_when_created_at_is_equal(catalog):
    created = "2026-02-01T10:00:00+00:00"
    expected = []
    for number in range(5):
        _, asset_uuid = _insert_asset(
            title=f"Paging marker harbor {number}", description="", created_at=created
        )
        expected.append(asset_uuid)

    seen = []
    for offset in (0, 2, 4):
        page = search.search(q="paging marker", sort="created_desc", limit=2, offset=offset)
        seen.extend(item["uuid"] for item in page["items"])
    assert len(seen) == len(set(seen)) == 5
    assert seen == list(reversed(expected))

    ascending = search.search(q="paging marker", sort="created_asc", limit=10)["items"]
    assert [item["uuid"] for item in ascending] == expected


def test_special_character_keyword_does_not_fail(catalog):
    assert search.search(q='*** "" (((', limit=5)["total"] >= 4


def test_csv_export_matches_full_search_and_quotes_fields(catalog):
    user = {"id": 1, "role": "admin"}
    listing = list_assets(
        q="harbor", media_type=None, tag=None,
        include_tag=["approved", "campaign"], exclude_tag=["internal"],
        collection_id=catalog["collection_id"], sort="created_desc",
        mode="keyword", limit=1, offset=0, user=user,
    )
    exported = export_assets_csv(
        q="harbor", media_type=None, include_tag=["approved", "campaign"],
        exclude_tag=["internal"], collection_id=catalog["collection_id"],
        sort="created_desc", user=user,
    )
    text = exported.body.decode("utf-8").lstrip("\ufeff")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [row["uuid"] for row in rows] == [item["uuid"] for item in listing["items"]]
    assert rows[0]["title"] == 'Harbor "launch", 中文'
    assert rows[0]["description"] == "First line, with comma\nSecond line with 中文"
    assert rows[0]["tags"] == "approved|campaign"


def test_empty_export_has_header_and_export_requires_login(catalog):
    with pytest.raises(HTTPException) as unauthorised:
        require_user(_request())
    assert unauthorised.value.status_code == 401
    response = export_assets_csv(
        q="", media_type=None, include_tag=["does-not-exist"], exclude_tag=[],
        collection_id=None, sort="created_desc", user={"id": 1, "role": "viewer"},
    )
    text = response.body.decode("utf-8").lstrip("\ufeff")
    assert list(csv.reader(io.StringIO(text))) == [[
        "uuid", "title", "description", "filename", "media_type", "tags", "created_at"
    ]]


def test_pagination_and_sort_constraints_are_published(catalog):
    parameters = {
        parameter["name"]: parameter["schema"]
        for parameter in app.openapi()["paths"]["/api/assets"]["get"]["parameters"]
    }
    assert parameters["limit"]["minimum"] == 1
    assert parameters["limit"]["maximum"] == 100
    assert parameters["offset"]["minimum"] == 0
    assert parameters["sort"]["pattern"] == "^(created_desc|created_asc)$"
    with pytest.raises(ValueError):
        search.search(sort="drop table assets")


def test_edits_are_visible_to_search_and_export(catalog):
    asset_uuid = catalog["gamma_uuid"]
    user = {"id": 1, "username": "tester", "role": "admin"}
    request = _request()
    updated = update_asset(
        asset_uuid, request,
        {"title": "Revised needleword", "description": "更新后的描述"}, user,
    )
    tagged = add_tag(asset_uuid, request, {"name": "campaign"}, user)
    listing = list_assets(
        q="needleword", media_type=None, tag=None,
        include_tag=["approved", "campaign"], exclude_tag=[], collection_id=None,
        sort="created_desc", mode="keyword", limit=24, offset=0, user=user,
    )
    exported = export_assets_csv(
        q="needleword", media_type=None, include_tag=["approved", "campaign"],
        exclude_tag=[], collection_id=None, sort="created_desc", user=user,
    )
    assert updated["asset"]["title"] == "Revised needleword"
    assert tagged == {"ok": True}
    assert [item["uuid"] for item in listing["items"]] == [asset_uuid]
    text = exported.body.decode("utf-8").lstrip("\ufeff")
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["title"] == "Revised needleword"
    assert row["description"] == "更新后的描述"
    assert row["tags"] == "approved|campaign"
