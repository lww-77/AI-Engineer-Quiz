"""Search and discovery over the asset catalog.

Keyword search, structured filters, pagination, and export all share the same
SQL builder. Keeping that logic in one place prevents the list count, page
contents, and CSV export from drifting apart.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from . import ai
from .db import get_conn


SORTS = {
    "created_desc": "a.created_at DESC, a.id DESC",
    "created_asc": "a.created_at ASC, a.id ASC",
    # Backwards-compatible names used by the original client.
    "newest": "a.created_at DESC, a.id DESC",
    "oldest": "a.created_at ASC, a.id ASC",
    "name": "a.title ASC, a.id ASC",
    "largest": "a.size DESC, a.id DESC",
}


def _fts_query(q: str) -> str:
    """Turn user text into a safe FTS5 prefix query."""
    terms = [
        term
        for term in "".join(
            char if char.isalnum() or char.isspace() else " " for char in q
        ).split()
        if term
    ]
    return " ".join(f'"{term}"*' for term in terms)


def _normalise_tags(values: Sequence[str] | None) -> list[str]:
    """Apply the catalog's case handling and remove blanks and duplicates."""
    return sorted({value.strip().lower() for value in values or [] if value.strip()})


def _filters(
    *,
    q: str,
    media_type: Optional[str],
    include_tags: Sequence[str] | None,
    exclude_tags: Sequence[str] | None,
    collection_id: Optional[int],
    status: str,
) -> tuple[str, str, list, bool]:
    """Return joins, WHERE SQL, parameters, and whether filters conflict."""
    included = _normalise_tags(include_tags)
    excluded = _normalise_tags(exclude_tags)
    if set(included) & set(excluded):
        return "", "0", [], True

    joins: list[str] = []
    where = ["a.status = ?"]
    params: list = [status]

    # Punctuation-only input is unrestricted instead of becoming invalid FTS.
    fts_query = _fts_query(q)
    if fts_query:
        joins.append("JOIN asset_fts ON asset_fts.rowid = a.id")
        where.append("asset_fts MATCH ?")
        params.append(fts_query)
    if media_type:
        where.append("a.media_type = ?")
        params.append(media_type)
    if collection_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM collection_assets ca "
            "WHERE ca.asset_id = a.id AND ca.collection_id = ?)"
        )
        params.append(collection_id)

    # EXISTS avoids multiplying rows when many-to-many filters are combined.
    for tag_name in included:
        where.append(
            "EXISTS (SELECT 1 FROM asset_tags at "
            "JOIN tags t ON t.id = at.tag_id "
            "WHERE at.asset_id = a.id AND t.name = ?)"
        )
        params.append(tag_name)
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        where.append(
            "NOT EXISTS (SELECT 1 FROM asset_tags at "
            "JOIN tags t ON t.id = at.tag_id "
            f"WHERE at.asset_id = a.id AND t.name IN ({placeholders}))"
        )
        params.extend(excluded)

    return " ".join(joins), " AND ".join(where), params, False


def _matching_rows(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    include_tags: Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "created_desc",
    limit: int | None = None,
    offset: int = 0,
):
    """Return matching rows using the canonical filter definition."""
    if sort not in SORTS:
        raise ValueError(f"Unsupported sort: {sort}")
    joins, where_sql, params, conflict = _filters(
        q=q,
        media_type=media_type,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        status=status,
    )
    if conflict:
        return []

    sql = f"SELECT a.* FROM assets a {joins} WHERE {where_sql} ORDER BY {SORTS[sort]}"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = [*params, limit, offset]
    return get_conn().execute(sql, params).fetchall()


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags: Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "created_desc",
    mode: str = "keyword",
    limit: int = 60,
    offset: int = 0,
) -> dict:
    """Search assets and return one stable page plus its total."""
    effective_includes = list(include_tags or [])
    if tag:
        effective_includes.append(tag)

    # Preserve semantic search for existing API consumers. The assignment UI
    # intentionally uses keyword mode only.
    if mode == "semantic" and q.strip():
        return _semantic_search(q, media_type, status, limit, offset)

    joins, where_sql, params, conflict = _filters(
        q=q,
        media_type=media_type,
        include_tags=effective_includes,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        status=status,
    )
    if conflict:
        return {"total": 0, "items": [], "facets": _facets(status), "mode": mode}

    conn = get_conn()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM assets a {joins} WHERE {where_sql}", params
    ).fetchone()["c"]
    rows = _matching_rows(
        q=q,
        media_type=media_type,
        include_tags=effective_includes,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        status=status,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "items": [_card(dict(row)) for row in rows],
        "facets": _facets(status),
        "mode": mode,
    }


def all_matching_assets(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    include_tags: Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    collection_id: Optional[int] = None,
    sort: str = "created_desc",
) -> list[dict]:
    """Return all active matches with deterministic pipe-delimited tags."""
    rows = _matching_rows(
        q=q,
        media_type=media_type,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        status="active",
        sort=sort,
    )
    conn = get_conn()
    results = []
    for row in rows:
        asset = dict(row)
        tags = conn.execute(
            "SELECT t.name FROM tags t JOIN asset_tags at ON at.tag_id = t.id "
            "WHERE at.asset_id = ? ORDER BY t.name",
            (asset["id"],),
        ).fetchall()
        asset["tags"] = "|".join(tag["name"] for tag in tags)
        results.append(asset)
    return results


def _semantic_search(q, media_type, status, limit, offset) -> dict:
    conn = get_conn()
    qvec = ai.embed(q)
    where = ["status = ?", "embedding IS NOT NULL"]
    params: list = [status]
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(where)}", params
    ).fetchall()
    scored = []
    for row in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(row["embedding"]))
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    page = scored[offset : offset + limit]
    items = []
    for score, row in page:
        card = _card(dict(row))
        card["score"] = round(score, 4)
        items.append(card)
    return {
        "total": len(scored),
        "items": items,
        "facets": _facets(status),
        "mode": "semantic",
    }


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    base_vector = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [
        (ai.cosine(base_vector, ai.unpack_embedding(row["embedding"])), row)
        for row in rows
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    output = []
    for score, row in scored[:limit]:
        card = _card(dict(row))
        card["score"] = round(score, 4)
        output.append(card)
    return output


def _facets(status: str) -> dict:
    conn = get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = ? GROUP BY media_type ORDER BY c DESC",
        (status,),
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = ?
           GROUP BY t.name ORDER BY c DESC LIMIT 25""",
        (status,),
    ).fetchall()
    return {
        "media_types": [{"value": row["media_type"], "count": row["c"]} for row in media],
        "tags": [{"value": row["name"], "count": row["c"]} for row in tags],
    }


def _card(asset: dict) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": asset["id"],
        "uuid": asset["uuid"],
        "title": asset["title"],
        "filename": asset["filename"],
        "media_type": asset["media_type"],
        "mime_type": asset["mime_type"],
        "ext": asset["ext"],
        "size": asset["size"],
        "width": asset["width"],
        "height": asset["height"],
        "alt_text": asset["alt_text"],
        "status": asset["status"],
        "created_at": asset["created_at"],
    }
