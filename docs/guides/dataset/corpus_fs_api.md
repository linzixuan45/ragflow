cd 

---
title: Corpus FS API
---
# Corpus FS API

Corpus FS exposes one dataset-scoped, path-only namespace. Dataset content is
rooted directly at `/`; document IDs, file IDs, chunk IDs, and object storage
locations remain server-side details.

All routes use the prefix `/api/v1/datasets/{dataset_id}/fs` and require an
authenticated caller with access to the dataset.

## Read operations

| Method and path                       | Purpose                                                               |
| ------------------------------------- | --------------------------------------------------------------------- |
| `GET /tree?path=/`                  | List one directory with cursor pagination.                            |
| `GET /find`                         | Find files by name below a path.                                      |
| `GET /stat`                         | Read metadata for one path.                                           |
| `GET /usage`                        | Aggregate document, byte, chunk, and token usage.                     |
| `GET /blob?path=...`                | Download the immutable uploaded source; supports one HTTP byte range. |
| `GET /content?path=...`             | Read the parsed/source text projection and opaque image references.   |
| `GET /image?path=...&image_ref=...` | Read one projected image.                                             |
| `POST /grep`                        | Run literal keyword search below a path.                              |
| `POST /search`                      | Run semantic or hybrid retrieval below a path.                        |
| `POST /citations/resolve`           | Resolve a citation to versioned evidence coordinates.                 |
| `POST /citations/preview`           | Read evidence from the source version bound to a citation.            |
| `GET /parse-status?path=...`        | Read detailed parsing status, progress, message, and counts.          |
| `POST /parse`                       | Start, restart, or cancel parsing for up to 100 paths.                |

`GET /content` binds every returned block citation to the same source-projection
snapshot used to render that response. A projection published concurrently with
the read can therefore become the next active revision without invalidating the
citations already returned by the in-flight request.

`/blob` and `/content` are intentionally different. `/blob` returns the exact
uploaded object. `/content` returns readable parsed blocks and is not a raw
DeepDOC Markdown file.

`POST /search` validates that `path` exists before retrieval and never widens an
empty path scope to the whole dataset. `mode: "semantic"` uses pure vector
weight (`1.0`); `mode: "hybrid"` uses the supplied
`vector_similarity_weight`.

Read operations never create, rename, or repair the backing file hierarchy. If
the dataset hierarchy has not been initialized by the dataset/file lifecycle,
the request fails with `NOT_FOUND` instead of mutating state. Repair belongs to
an explicit administrative write path.

### Grep pagination

`POST /grep` returns both mapping and retrieval progress:

```json
{
  "total": 20,
  "returned_count": 4,
  "consumed_count": 5,
  "items": [],
  "next_cursor": "opaque-cursor",
  "truncated": true
}
```

`returned_count` is the number of hits that were authorized and mapped to
public paths. `consumed_count` is the number of search hits consumed from the
current page. The next cursor advances by `consumed_count`, so a stale or
unmappable index hit cannot make a later page repeat. `total` is the backend
keyword-hit count, while `truncated` means another consumed page is available.

Every returned hit carries the public evidence fields `path`, `snippet`,
`citation_ref`, `page`, and `images`. `images` matches the `/search` contract: it
is empty when the matched chunk has no associated figure, and otherwise exposes
an opaque `image_ref` readable through `GET /image`. The reference is derived
from the same chunk as the snippet, so a hit whose snippet is a figure
description also carries that figure's reference. A table stored as an image can
appear here too, exactly as `/search` already reports it.

An `image_ref` is a hint, not a guarantee: the referenced object can be removed
or migrated after indexing. `GET /image` then rejects the read with
`IMAGE_NOT_FOUND` (404), and callers must tolerate that and skip the hit. A
snippet that reads like a figure caption while `images` is empty means the
dataset's parsing pipeline flattened media into text, so that chunk carries no
image reference at all.

## Citation contract

`/content`, `/grep`, and `/search` issue the same encrypted and authenticated
`citation_ref` format. A citation is durable evidence metadata, not a paging
cursor, and does not expire with the short-lived content or listing cursor.
`/content` also places a citation on every returned block; its top-level
`citation_ref` is the first returned block.

Resolve citations with a JSON body containing only `citation_ref`:

```http
POST /api/v1/datasets/{dataset_id}/fs/citations/resolve
Content-Type: application/json

{"citation_ref":"cit1...."}
```

The resolved `data` contains this public contract:

```json
{
  "dataset_id": "dataset-id-from-the-route",
  "path": "/manual.pdf",
  "content_revision": 42,
  "projection_revision": 7,
  "page": 12,
  "block_ref": "41",
  "range": {"start": 0, "end": 128},
  "source_hash": "sha256-hex"
}
```

`range` uses UTF-8 byte offsets within `block_ref`. Page, block, range, and
projection revision can be `null` when the parser cannot provide that
coordinate. Non-projection chunk references are opaque and never expose an
index row ID.

`POST /citations/preview` accepts the same body and returns both `citation` and
`preview`. For PDF source projections it reads the exact cited revision,
including a retired revision after a later successful reparse. The encrypted
citation carries a private stable document locator, so preview still works
after a path rename and reports the new path as `preview.current_path`.

Both routes require normal dataset authentication and authorization. Tokens
are bound to the tenant and dataset; tampered or cross-dataset tokens are
rejected. A citation does not grant access, and deleted evidence is not
retained solely to satisfy a citation. Set a stable `RAGFLOW_CORPUS_SECRET`
with at least 32 bytes when citations must
survive application-secret rotation or Redis replacement. Corpus FS derives
separate keys for citations, cursors, delete confirmations, and opaque media
references. Rotating the root intentionally invalidates previously issued
references.

## Conditional writes

Every mutating request requires the current dataset content revision:

```http
If-Match: "42"
```

JSON read and successful write responses include the current value in both
`data.revision.content_revision` and the HTTP `ETag` header. A missing
precondition returns `428 PRECONDITION_REQUIRED`; a stale revision returns
`409 REVISION_CONFLICT`. Mutations are serialized by locking the dataset row;
successful mutations increment the revision before that lock is released.
Object storage and parse-queue side effects are not database transactions, so
uploads report per-file errors while preserving and revising successful files.
`GET /blob` instead uses the source content hash as its `ETag`.

| Method and path                  | Purpose                                                                       |
| -------------------------------- | ----------------------------------------------------------------------------- |
| `POST /entries/folders`        | Create a directory.                                                           |
| `POST /entries/upload`         | Upload files and enqueue parsing.                                             |
| `POST /entries/move`           | Move entries or rename one entry.                                             |
| `POST /entries/delete-preview` | Preview recursive delete counts and receive a confirmation token.             |
| `DELETE /entries`              | Delete the confirmed snapshot.                                                |
| `POST /parse`                  | Apply`retry` or `cancel` to up to 100 paths and return per-path outcomes. |

The request body is `{"action":"retry|cancel","paths":["/a.pdf"]}`.
The response reports `success_count`, `skipped_count`, `failed_count`, and an
`items` result for every unique normalized path. The whole request uses one
`If-Match` content revision.

Delete preview is read-only and does not require `If-Match`; the subsequent
delete does.

## Versioned Markdown derivative

An uploaded source and an edited Markdown derivative have separate lifecycles.
Saving Markdown never overwrites a PDF or another uploaded source.

| Method and path                            | Purpose                                    |
| ------------------------------------------ | ------------------------------------------ |
| `GET /derived-content?path=...`          | Read the current Markdown derivative.      |
| `PUT /derived-content`                   | Save a new immutable version.              |
| `GET /derived-content/versions?path=...` | List versions newest first.                |
| `POST /derived-content/restore`          | Restore an older version as a new version. |

`PUT /derived-content` accepts `path`, UTF-8 `content`, and
`expected_version`. The first save uses `expected_version: 0`. Restore accepts
`path`, `version`, and the current `expected_version`. Both operations also
require the dataset `If-Match` revision.

Versions contain a SHA-256 digest and timestamp. Their storage object names and
internal document identifiers are never returned. Deleting the source document
also removes its Markdown version objects and database records.

## Parse states

Corpus FS reports `UNSTARTED`, `SCHEDULED`, `RUNNING`, `DONE`, `FAILED`,
`CANCELLED`, or `UNKNOWN`. Clients should use the status and progress fields,
not fixed delays, to decide when parsed content is available.
