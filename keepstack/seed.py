"""Generate a synthetic demo catalog so a fresh install has content.

All sample media is generated on the fly (gradients, shapes, labelled
cards) so there is no bundled binary and nothing proprietary. Run with::

    python -m keepstack seed
"""
from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw, ImageFont

from . import ingest
from .auth import hash_password
from .audit import now_iso
from .db import get_conn, init_db, transaction

random.seed(7)  # deterministic demo set

_PALETTES = [
    ((94, 132, 247), (24, 28, 38)), ((70, 192, 138), (18, 30, 26)),
    ((242, 96, 122), (34, 18, 24)), ((220, 160, 60), (32, 26, 14)),
    ((150, 110, 220), (24, 18, 34)), ((60, 180, 200), (14, 28, 30)),
]
_SUBJECTS = [
    ("City skyline at dusk", ["architecture", "city", "skyline"]),
    ("Mountain trail map", ["map", "outdoors", "terrain"]),
    ("Quarterly report cover", ["document", "report", "finance"]),
    ("Public transit poster", ["poster", "transit", "civic"]),
    ("Coastal survey photo", ["coast", "survey", "environment"]),
    ("Heritage building facade", ["heritage", "architecture", "history"]),
    ("Park event banner", ["event", "park", "community"]),
    ("Census infographic", ["infographic", "data", "census"]),
    ("River watershed diagram", ["diagram", "water", "environment"]),
    ("Municipal logo mark", ["logo", "brand", "civic"]),
    ("Road maintenance notice", ["notice", "roads", "public-works"]),
    ("Library reading room", ["library", "interior", "culture"]),
]


def _make_image(idx: int, label: str) -> bytes:
    fg, bg = _PALETTES[idx % len(_PALETTES)]
    w, h = random.choice([(1200, 800), (800, 1200), (1000, 1000)])
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    # soft geometric composition
    for _ in range(6):
        x0, y0 = random.randint(-100, w), random.randint(-100, h)
        r = random.randint(120, 480)
        shade = tuple(min(255, c + random.randint(-30, 40)) for c in fg)
        draw.ellipse([x0, y0, x0 + r, y0 + r], fill=shade)
    draw.rectangle([0, h - 150, w, h], fill=(0, 0, 0))
    try:
        font = ImageFont.truetype("arial.ttf", 44)
    except Exception:
        font = ImageFont.load_default()
    # Centered so the caption survives the square centre-crop used by thumbnails.
    draw.text((w / 2, h - 75), label, font=font, fill=(255, 255, 255), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def run_seed() -> None:
    init_db()
    conn = get_conn()
    # ensure admin exists (startup hook normally does this, but seed runs standalone)
    if not conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        conn.execute(
            "INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
            "VALUES ('admin','admin@example.org','Administrator',?, 'admin', 1, ?)",
            (hash_password("admin"), now_iso()))
        conn.commit()
    admin = dict(conn.execute("SELECT * FROM users WHERE username='admin'").fetchone())

    # demo collaborators
    for uname, role in [("curator", "editor"), ("contributor", "contributor"), ("viewer", "viewer")]:
        if not conn.execute("SELECT 1 FROM users WHERE username=?", (uname,)).fetchone():
            conn.execute(
                "INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (?,?,?,1,?)",
                (uname, hash_password("demo1234"), role, now_iso()))
    conn.commit()

    created = []
    print("Generating synthetic assets...")
    for i, (label, tags) in enumerate(_SUBJECTS):
        for variant in range(2):  # two variants each -> 24 assets
            data = _make_image(i + variant, label)
            asset = ingest.ingest_stream(io.BytesIO(data), f"{label.lower().replace(' ', '_')}_{variant+1}.jpg",
                                         user=admin, run_ai=True, title=label if variant == 0 else f"{label} (alt)")
            for t in tags:
                conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
                tid = conn.execute("SELECT id FROM tags WHERE name=?", (t,)).fetchone()["id"]
                conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?, 'manual')",
                             (asset["id"], tid))
            conn.commit()
            ingest.reindex_fts(asset["id"])
            ingest.reindex_embedding(asset["id"])
            created.append(asset["id"])

    # collections
    import uuid as uuidlib
    cols = {}
    for name, desc in [("Civic Communications", "Posters, banners, notices for public outreach"),
                       ("Environment & Survey", "Coastal, watershed, and terrain imagery"),
                       ("Heritage & Culture", "Buildings, libraries, and cultural assets")]:
        cur = conn.execute(
            "INSERT INTO collections (uuid, name, description, kind, created_by, created_at) VALUES (?,?,?, 'folder', ?, ?)",
            (str(uuidlib.uuid4()), name, desc, admin["id"], now_iso()))
        cols[name] = cur.lastrowid
    conn.commit()
    # distribute assets across collections by tag affinity
    for aid in created:
        row = conn.execute(
            "SELECT GROUP_CONCAT(t.name) g FROM asset_tags at JOIN tags t ON t.id=at.tag_id WHERE at.asset_id=?",
            (aid,)).fetchone()
        g = (row["g"] or "")
        target = None
        if any(k in g for k in ("civic", "poster", "notice", "event", "logo", "transit", "roads")):
            target = cols["Civic Communications"]
        elif any(k in g for k in ("environment", "coast", "water", "survey", "terrain", "outdoors")):
            target = cols["Environment & Survey"]
        elif any(k in g for k in ("heritage", "history", "library", "culture")):
            target = cols["Heritage & Culture"]
        if target:
            conn.execute("INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) VALUES (?,?,?)",
                         (target, aid, now_iso()))
    conn.commit()

    # Make the assignment's acceptance scenario reproducible in a fresh seed:
    # both harbor assets are approved campaign material in Environment &
    # Survey, while one is internal and must be excluded by the sample query.
    harbor_assets = conn.execute(
        """SELECT a.id FROM assets a
           JOIN asset_tags at ON at.asset_id = a.id
           JOIN tags t ON t.id = at.tag_id
           WHERE t.name = 'coast' ORDER BY a.id LIMIT 2"""
    ).fetchall()
    for index, row in enumerate(harbor_assets):
        conn.execute(
            "UPDATE assets SET title = ?, description = ?, updated_at = ? WHERE id = ?",
            (
                f"Harbor campaign photo {index + 1}",
                "Approved harbor campaign material for the coastal survey.",
                now_iso(),
                row["id"],
            ),
        )
        scenario_tags = ["approved", "campaign"] + (["internal"] if index == 1 else [])
        for tag_name in scenario_tags:
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
            tag_id = conn.execute(
                "SELECT id FROM tags WHERE name = ?", (tag_name,)
            ).fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?, ?, 'manual')",
                (row["id"], tag_id),
            )
        conn.commit()
        ingest.reindex_fts(row["id"])
        ingest.reindex_embedding(row["id"])

    print(f"Seeded {len(created)} assets, {len(cols)} collections, and demo users.")
    print("Sign in as admin/admin (also: curator, contributor, viewer / demo1234).")


if __name__ == "__main__":
    run_seed()
