# Submission: asset search and CSV export

## Repository information

- Fork: <https://github.com/lww-77/AI-Engineer-Quiz>
- Branch: `feature/asset-search-export`
- Final commit: supplied in the submission email (verify with `git rev-parse HEAD`)

The work was based on the supplied repository history. No history was
reinitialized, squashed, amended, or rebased.

## Run locally

Python 3.10 or newer is required.

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'

export KEEPSTACK_AI_ENABLED=false
export KEEPSTACK_OCR_ENABLED=false

uv run --no-sync python -m keepstack seed
uv run --no-sync python -m keepstack
```

Open <http://localhost:8000> and sign in with `admin` / `admin`.

The seed data contains a reproducible acceptance scenario. In the search page:

1. select `Environment & Survey`;
2. search for `harbor`;
3. include `approved, campaign`;
4. exclude `internal`.

The expected result is `Harbor campaign photo 1`. The second harbor campaign
asset is deliberately tagged `internal` and is excluded.

## Automated verification

```bash
uv run --no-sync python -m pytest -q
```

Last local result:

```text
30 passed, 2 skipped
```

The two skipped tests are the upstream optional local vision/OCR tests. The new
acceptance tests cover combined filters, duplicate and conflicting tags,
same-timestamp stable pagination, special-character input, authenticated empty
and non-empty CSV output, Unicode/quotes/commas/newlines, API constraints, and
search/export results after metadata and tag edits.

## Implementation choices

- List count, page rows, and CSV export use one canonical SQL filter builder.
- Included tags use one `EXISTS` clause per normalized tag, so every requested
  tag is required without multiplying result rows.
- Excluded tags use `NOT EXISTS`; any matching excluded tag removes the asset.
- Tag input is trimmed, lowercased like existing tag writes, and deduplicated.
  A tag present in both include and exclude sets returns an empty result.
- Creation-time ordering uses asset ID as a deterministic tie-breaker, avoiding
  duplicates or omissions across offset-based pages when timestamps match.
- Punctuation-only keyword input is treated as no keyword restriction rather
  than sending an invalid empty expression to SQLite FTS5.
- The browser cancels the previous list request whenever a newer search starts,
  so a slow stale response cannot overwrite the latest filters.
- CSV is generated with Python's `csv` module and a UTF-8 BOM. The `tags` column
  contains alphabetically sorted tag names separated by `|`.
- CSV download uses authenticated `fetch` followed by a browser Blob download;
  a plain link would not include the Bearer token.
- The semantic-search selector is hidden for this assignment. Existing semantic
  API behavior remains available for compatibility.

No new runtime dependency or frontend build tool was added.

## Scope and limitations

- Export is synchronous and assembled in memory. This is appropriate for the
  requested scope; very large catalogs would benefit from streaming or a job.
- List and export consistency assumes catalog data does not change between
  requests, as allowed by the assignment.
- Facet counts remain global active-asset counts and are not presented as the
  current filtered result count.
- Existing FastAPI startup deprecation warnings remain; they are unrelated to
  the requested functionality.

## Unfinished items

None.
