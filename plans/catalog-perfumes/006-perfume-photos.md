---
status: done
module: catalog
min_implementer: mid
depends_on: ["003", "004"]
---

# 006 — Perfume photos on S3

## Context

### What exists

**Perfumes.** A `Perfume` and its presentations form one aggregate
(`apps/api/src/fragancia_api/modules/catalog/domain/perfume.py`). Commands load it with its row
locked, mutate it and save it (`application/commands/perfumes.py`, `presentations.py`;
`infrastructure/sql_perfume_repository.py:141-222`). An archived perfume is read-only (README
decision 39). The read side is `SqlPerfumeQueries`, whose `get_admin`, `list_public` and
`get_public` build `AdminPerfume`, `PublicPerfumeCard` and `PublicPerfume`
(`contracts.py`). Routes are in `http/perfume_router.py`, and wiring is in
`module.py:60-125`.

**Configuration.** Settings live in `apps/api/src/fragancia_api/config.py:51-84`. Each one is a
field with a default for local development, plus a `model_validator` when a bad value must
stop startup (`_check_admin_web_url`, `config.py:73-78`). A module reads its own settings
through `Platform.settings` (`apps/api/src/fragancia_api/shared/module.py:17-28`).

**Blocking work off the event loop.** `Argon2PasswordHasher` uses `asyncio.to_thread`
(`apps/api/src/fragancia_api/modules/identity/infrastructure/argon2_password_hasher.py:10-20`).

**S3.** Local development runs RustFS (`infra/docker/compose.yaml:52-64`, S3 API on port 9100).
`apps/api/.env.example:33-37` already declares `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`,
`S3_SECRET_KEY` and `S3_BUCKET` (`fragancia-media`). The bucket is created by
`scripts/bootstrap.py:86-93` through `scripts/infra.py:86-94` (`ensure_bucket`), which uses
`boto3`, a root dev dependency (`pyproject.toml:10-12`). The **API itself has no S3 settings,
no S3 client and none of these packages**: `apps/api/pyproject.toml:7-18` lacks `boto3`,
`pillow` and `python-multipart`, and FastAPI needs `python-multipart` for `UploadFile`.

**Migrations.** The last one is `0008_catalog_perfume_slug_history_and_.py`.

### What we need

README decisions 6, 47–52 and 39. The back office uploads up to 3 photos per perfume,
removes them and reorders them. The storefront card shows the main photo, and the detail
page shows all of them. Plan 007 (the import) will reuse the same processing and storage.

### Approach

**Two application ports**, both in `application/ports.py`:
- `ImageProcessor`: validates the bytes and returns two WebP renditions.
- `MediaStorage`: stores and deletes objects.

Two adapters implement them in `infrastructure/`:
- `PillowImageProcessor`, which works in `asyncio.to_thread` because image work is CPU-bound,
  as with argon2.
- `S3MediaStorage`, which runs boto3 calls in `asyncio.to_thread`.

**`Photo` is a child entity of the `Perfume` aggregate**, like presentations, so the limit of
3 and the positions are enforced under the perfume's row lock.

**Upload order.**
1. Process the image outside the transaction.
2. Upload both objects.
3. Run the transaction that adds the photo.
4. If the transaction refuses (limit, archived, not found), delete the uploaded objects
   (compensation).

**Removal order.** The transaction removes the row, and the objects are deleted after the
commit. A failed delete is logged and leaves an orphan object, never a broken photo row.

The bucket's `perfumes/*` prefix is public-read (decision 49). Bootstrap sets that policy
locally; production sets it by configuration.

**Discarded:**
- Presigned uploads (decision 47).
- Storing the original file (decision 48).
- Photos per presentation (decision 6).
- Processing in the arq worker: the admin waits a second at most, and the worker would add a
  pending state to the photo.

### Imitated files

- Ports and an off-loop adapter: `application/ports.py` and `argon2_password_hasher.py`.
- Child entities and their persistence: presentations in `perfume.py` and
  `sql_perfume_repository.py` (upsert by id).
- Routes: `http/perfume_router.py`.
- Settings with a validator: `config.py:73-78`.

### Rules fixed here

These are derived from the decisions above.

- **The file:**
  - It must have at most 10,485,760 bytes (10 MB), or the upload gets a 422
    `CATALOG_PHOTO_TOO_LARGE`.
  - Pillow must recognize it as JPEG, PNG or WebP, whatever its name or declared content type.
    Otherwise it gets a 422 `CATALOG_PHOTO_FORMAT_INVALID`.
  - Its shorter side must be at least 600 px after applying the EXIF orientation, or it gets
    a 422 `CATALOG_PHOTO_TOO_SMALL`.
  - Pillow's decompression-bomb guard stays on; a triggered guard means
    `FORMAT_INVALID`.
- **Renditions:**
  - Each is scaled so its longest side is 400 px (card) or 1200 px (detail), never upscaled.
  - Each is WebP at quality 82, with no EXIF, ICC or other metadata.
  - Alpha is kept if present, otherwise RGB.
- **Object keys:** `perfumes/<perfume_id>/<photo_id>-card.webp` and
  `perfumes/<perfume_id>/<photo_id>-detail.webp`.
- **Count and positions:**
  - A perfume has at most 3 photos, so a 4th gets a 409 `CATALOG_PHOTO_LIMIT_REACHED`.
  - A new photo takes the next position.
  - Removing a photo renumbers the rest to 1..n, keeping their order.
- **Reorder:** the body is the full list of the perfume's photo ids in the new order. Anything
  else (missing, extra, duplicated, or a foreign id) gets a 422 `CATALOG_PHOTO_ORDER_INVALID`.
- **Archived perfume:** adding, removing and reordering photos all get a 422
  `CATALOG_PERFUME_ARCHIVED` (decision 39).
- **Publishing** still does not require a photo (decision 24).

## Out of scope

- The Excel import and its URL downloads (plan 007; it will reuse these ports).
- Alt text, captions, photos per presentation, more than two renditions, AVIF.
- Image CDN setup, and setting the production bucket policy. The production policy is a
  rollout step, written in "Risks" when the PR is opened.
- Cleaning up orphan objects. A failed post-commit delete is only logged.
- Virus scanning beyond Pillow's decoding.
- The web app.

## Dependencies

- **003** (`done`) provides the `Perfume` aggregate, `SqlPerfumeRepository`,
  `get_for_update`/`save`, the archived-is-read-only rule and the admin perfume routes.
- **004** (`done`) provides the `PublicPerfumeCard`/`PublicPerfume` contracts and
  `list_public`/`get_public`.

## Steps

1. **Dependencies and settings**
   - Files: `apps/api/pyproject.toml` (modify), `apps/api/src/fragancia_api/config.py` (modify), `apps/api/.env.example` (modify)
   - Do:
     1. Run `uv add --package fragancia-api boto3 pillow python-multipart`. This also updates
        `uv.lock`.
     2. Before using an API you are not sure of, read the installed Pillow and boto3 docs:
        `ImageOps.exif_transpose`, `Image.thumbnail`, WebP `save` options, `put_object` and
        `delete_objects`.
     3. In `Settings`, add a commented block "Media storage (catalog photos)":

        | Field | Type and default |
        | ----- | ---------------- |
        | `s3_endpoint_url` | `str = "http://127.0.0.1:9100"` |
        | `s3_access_key` | `str \| None = None` |
        | `s3_secret_key` | `SecretStr \| None = None` |
        | `s3_bucket` | `str = "fragancia-media"` |
        | `s3_region` | `str = "us-east-1"` |
        | `media_public_base_url` | `str = "http://127.0.0.1:9100/fragancia-media"` |
        | `photo_max_bytes` | `int = Field(default=10_485_760, ge=1)` |

        Add a validator for `media_public_base_url`, the same as `_check_admin_web_url`:
        `http(s)://` and no trailing `/`.
     4. In `.env.example`, add `MEDIA_PUBLIC_BASE_URL=http://127.0.0.1:9100/fragancia-media`
        under the S3 lines, with a comment: the public prefix the storefront loads photos from
        (a CDN in production).
   - Observable result: `uv run just check` is green, and `Settings()` loads with the new
     defaults.

2. **Contracts**
   - Files: `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify)
   - Do:
     - Add `AdminPhoto` (`id`, `position`, `card_url`, `detail_url`, `width`, `height`,
       `created_at`) and `ReorderPhotosRequest` (`photo_ids: list[UUID] =
       Field(max_length=3)`).
     - Add `photos: list[AdminPhoto]` to `AdminPerfume`, ordered by position.
     - Add `PublicPhoto` (`card_url`, `detail_url`).
     - Add `photo_url: str | None` to `PublicPerfumeCard`: the card URL of position 1, or
       `None` without photos.
     - Add `photos: list[PublicPhoto]` to `PublicPerfume`, ordered by position.
   - Observable result: `uv run just typecheck` passes.

3. **Domain**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/perfume.py` (modify), `apps/api/src/fragancia_api/modules/catalog/domain/errors.py` (modify)
   - Do:
     - **`errors.py`:**

       | Error | Category | Code |
       | ----- | -------- | ---- |
       | `PhotoTooLarge` | `InvalidValueError` | `CATALOG_PHOTO_TOO_LARGE`, details `{"max_bytes": …}` |
       | `PhotoFormatInvalid` | `InvalidValueError` | `CATALOG_PHOTO_FORMAT_INVALID`, details `{"formats": ["jpeg","png","webp"]}` |
       | `PhotoTooSmall` | `InvalidValueError` | `CATALOG_PHOTO_TOO_SMALL`, details `{"min_side": 600}` |
       | `PhotoOrderInvalid` | `InvalidValueError` | `CATALOG_PHOTO_ORDER_INVALID` |
       | `PhotoLimitReached` | `ConflictError` | `CATALOG_PHOTO_LIMIT_REACHED`, details `{"max": 3}` |
       | `PhotoNotFound` | `NotFoundError` | `CATALOG_PHOTO_NOT_FOUND` |

     - **`perfume.py`:**
       - Constants: `PHOTOS_MAX = 3`, `PHOTO_MIN_SIDE = 600`, `CARD_SIDE = 400` and
         `DETAIL_SIDE = 1200`.
       - `class Photo`, a plain entity with `id`, `position`, `card_key`, `detail_key`,
         `width`, `height` and `created_at`. Width and height are those of the detail
         rendition.
       - `photo_keys(perfume_id, photo_id) -> tuple[str, str]` returns the two object keys
         from "Rules fixed here".
       - `Perfume` gains `photos: list[Photo]` (constructor and `create`) and
         `removed_photo_ids: list[UUID]`, which starts empty.
       - `Perfume` gains three methods. Each first returns Err `PerfumeArchived` when the
         perfume is archived:

         | Method | Returns | Behavior |
         | ------ | ------- | -------- |
         | `add_photo(photo_id, card_key, detail_key, width, height, *, created_at)` | `Result[None, PerfumeArchived \| PhotoLimitReached]` | appends at position `len+1` |
         | `remove_photo(photo_id)` | `Result[Photo, PerfumeArchived \| PhotoNotFound]` | removes it, renumbers 1..n, appends to `removed_photo_ids`, returns the removed photo |
         | `reorder_photos(photo_ids)` | `Result[None, PerfumeArchived \| PhotoOrderInvalid]` | the ids must be exactly the current set, with no duplicates |

       - Extend the module docstring with the photo rules.
   - Observable result: `uv run just arch` is green.

4. **Application: ports and commands**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/catalog/application/commands/photos.py` (create)
   - Do:
     - **`ports.py`:**
       - A frozen dataclass `ProcessedImage` with `card: bytes`, `detail: bytes`, `width:
         int` and `height: int`.
       - `ImageProcessor(Protocol)` with `async def process(self, data: bytes) ->
         Result[ProcessedImage, PhotoFormatInvalid | PhotoTooSmall]`.
       - `MediaStorage(Protocol)` with two methods:
         - `async def put(self, key: str, data: bytes, content_type: str) -> None`.
         - `async def delete(self, keys: Sequence[str]) -> None`, which never raises for a
           missing key.
     - **`commands/photos.py`:**
       - **`AddPhoto(perfumes, images, storage, transactions, clock)`**,
         `execute(perfume_id, data: bytes) -> Result[UUID, DomainError]`:
         1. `len(data) > settings max` is checked in the router, not here.
         2. `images.process(data)`, returning Err as is.
         3. `photo_id = new_id()`, then the keys from `photo_keys`.
         4. `storage.put` for both, with `content_type="image/webp"`.
         5. `transactions.run(work)`, where `work` runs `get_for_update` (Err
            `PerfumeNotFound` if missing), `perfume.add_photo(...)`, then `save`.
         6. If the result is Err, `await storage.delete(keys)` (compensation, with any error
            logged and suppressed), then return the Err.
       - **`RemovePhoto(perfumes, storage, transactions)`**,
         `execute(perfume_id, photo_id)`: inside the transaction, `get_for_update`, then
         `remove_photo`, then `save`. On Ok, delete the removed photo's two keys after the
         commit, logging and suppressing any error.
       - **`ReorderPhotos(perfumes, transactions)`**, `execute(perfume_id, photo_ids)`:
         `get_for_update`, then `reorder_photos`, then `save`.

       Use the module logger the same way other adapters do (structlog
       `get_logger(__name__)`). If the application layer may not import structlog (check
       `apps/api/.importlinter`), report the delete failure through the `MediaStorage` port
       instead, and record that as a deviation.
   - Observable result: `uv run just typecheck` and `uv run just arch` pass.

5. **Infrastructure**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_repository.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_queries.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/pillow_image_processor.py` (create), `apps/api/src/fragancia_api/modules/catalog/infrastructure/s3_media_storage.py` (create), `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_perfumes.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_media.py` (create)
   - Do:
     - **`tables.py`:** add `perfume_photos` with these columns:

       | Column | Type | Constraints |
       | ------ | ---- | ----------- |
       | `id` | `Uuid` | primary key |
       | `perfume_id` | `Uuid` | FK `catalog.perfumes.id`, not null, indexed |
       | `position` | `Integer` | |
       | `card_key`, `detail_key` | `String(200)` | |
       | `width`, `height` | `Integer` | |
       | `created_at` | `DateTime(timezone=True)` | |

       Every column is not null. Add `UniqueConstraint("perfume_id", "position",
       name="uq_perfume_photos_position", deferrable=True, initially="DEFERRED")`, because a
       reorder rewrites several positions in one transaction.
     - **`SqlPerfumeRepository`:**
       - `get_for_update` also loads the photos ordered by position.
       - `save`, in the same savepoint:
         - Delete the rows whose ids are in `removed_photo_ids`.
         - Upsert every photo by id. Only `position` changes, so `set_` is `{"position": …}`.
         - Clear `removed_photo_ids`.
     - **`SqlPerfumeQueries`:** takes `media_base_url: str` in its constructor. The URL of an
       object is `f"{media_base_url}/{key}"`.
       - `get_admin` adds the photos.
       - `list_public` adds `photo_url`, from a correlated subquery of the `card_key` at
         position 1.
       - `get_public` adds the photos.
     - **`PillowImageProcessor.process`** runs `_process` in `asyncio.to_thread`:
       1. Open the bytes with `Image.open(BytesIO(data))`.
       2. If `img.format` is not `JPEG`, `PNG` or `WEBP` → `PhotoFormatInvalid`. Also catch
          `UnidentifiedImageError`, `Image.DecompressionBombError` and `OSError` →
          `PhotoFormatInvalid`.
       3. Apply `ImageOps.exif_transpose`. If `min(size) < 600` → `PhotoTooSmall`.
       4. Convert to `RGBA` if the image has alpha, otherwise to `RGB`.
       5. For 400 and 1200, make a copy, call `thumbnail((side, side), Image.Resampling.LANCZOS)`
          and save it as `WEBP` with `quality=82` and `method=6`. Pass no `exif` or
          `icc_profile` arguments.
       6. Return the bytes and the detail size.
     - **`S3MediaStorage(endpoint_url, access_key, secret_key, bucket, region)`** creates one
       `boto3.client("s3", …)` lazily.
       - `put` calls `put_object(Bucket, Key, Body, ContentType,
         CacheControl="public, max-age=31536000, immutable")` in a thread. Keys contain a fresh
         photo id, so the objects never change.
       - `delete` calls `delete_objects` in a thread.
     - **`in_memory_media.py`** holds `InMemoryMediaStorage`, which records puts and deletes
       and can be set to fail. It also holds `FakeImageProcessor`, which returns a fixed
       `ProcessedImage` or a configured Err.
     - **`InMemoryPerfumes`** stores photos, honours `removed_photo_ids`, and builds the photo
       URLs with the same base-URL rule.
   - Observable result: `uv run just typecheck` passes.

6. **Migration**
   - Files: `apps/api/migrations/versions/` (create)
   - Do:
     1. Run `uv run just db-revision "catalog perfume photos"`, then review the file by hand.
     2. It must have revision `0009` and `down_revision` `0008`.
     3. It creates `catalog.perfume_photos` with its FK, index and pk, and
        `uq_perfume_photos_position` as `DEFERRABLE INITIALLY DEFERRED`. Autogenerate may drop
        the deferrable flags; add them by hand if so.
     4. The downgrade drops the table.
     5. Apply it with `uv run just db-migrate` and `uv run just db-migrate --test`.
   - Observable result: `uv run just psql -c '\d catalog.perfume_photos'` shows the
     deferrable unique constraint. Downgrading to 0008 and upgrading again works on the test
     database.

7. **HTTP and wiring**
   - Files: `apps/api/src/fragancia_api/modules/catalog/http/perfume_router.py` (modify), `apps/api/src/fragancia_api/modules/catalog/module.py` (modify), `apps/api/openapi.json` (modify)
   - Do:
     - **`perfume_router.py`**, on `admin_perfumes`. Each docstring lists its error codes.

       | Route | Answers | Operation id |
       | ----- | ------- | ------------ |
       | `POST "/{perfume_id}/photos"` | 201 `CreatedResponse`; 404, 409, 422 | `add_photo` |
       | `DELETE "/{perfume_id}/photos/{photo_id}"` | 204; 404, 422 | `remove_photo` |
       | `PUT "/{perfume_id}/photos/order"`, body `ReorderPhotosRequest` | 204; 404, 422 | `reorder_photos` |

       The upload route takes `file: UploadFile`, reads at most `settings.photo_max_bytes +
       1` bytes, and raises `PhotoTooLarge` through `unwrap(Err(...))` when it gets more. It
       then passes the bytes to `AddPhoto`. Get the limit through a small injected service,
       not by reading settings in the router: register a `PhotoUploadLimit(max_bytes)`
       dataclass in `module.py`.
     - **`module.py`:**
       - Build `S3MediaStorage` from `platform.settings.s3_*`, unwrapping the secret with
         `.get_secret_value()` when set.
       - Build `PillowImageProcessor()`.
       - Pass `media_public_base_url` to `SqlPerfumeQueries`.
       - Register `AddPhoto`, `RemovePhoto`, `ReorderPhotos` and `PhotoUploadLimit`.
     - Run `uv run just openapi`.
   - Observable result: `uv run just check` is green and `openapi.json` has the 3 new
     operations plus the new photo fields.

8. **Bootstrap: public-read photos locally**
   - Files: `scripts/infra.py` (modify), `scripts/bootstrap.py` (modify), `scripts/test_bootstrap.py` (modify)
   - Do:
     - **`infra.py`:** add `ensure_public_read(s3, bucket: str, prefix: str) -> bool`. It
       applies a bucket policy that allows `s3:GetObject` on `arn:aws:s3:::<bucket>/<prefix>*`
       for `"Principal": "*"`. It returns True when it changed the policy and False when the
       policy was already in place. Read it first with `get_bucket_policy`; a missing policy is
       an empty one.
     - **`bootstrap.py`:** call it right after `ensure_bucket` with `prefix="perfumes/"`, and
       `report(...)` it.
     - **`test_bootstrap.py`:** extend `FakeS3` with `get_bucket_policy`/`put_bucket_policy`
       and test both the first call and an idempotent second call.
   - Observable result: `uv run just bootstrap` reports the policy, and `curl` on a stored
     object's public URL returns 200 without credentials.

9. **Docs**
   - Files: `docs/architecture.md` (modify)
   - Do: the `catalog` row (`docs/architecture.md:55`) adds "perfume photos (S3, WebP)".
     Next: "Excel import".
   - Observable result: the row matches what was built.

10. **Tests (tester phase)**
    - Files: `apps/api/tests/unit/catalog/` (create), `apps/api/tests/integration/catalog/` (create), `apps/api/tests/unit/test_settings.py` (modify)
    - Do: the layers in "Test layers required". The integration tests that hit S3 use the
      local RustFS with the bucket from bootstrap. They skip with a clear reason if it is not
      reachable, as other integration tests do for their services.
    - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

Checked against `uv run just api`, with RustFS up and the bucket bootstrapped.

- [x] Uploading a 2000×1500 JPEG (`POST /api/v1/admin/perfumes/{id}/photos`, multipart
      `file`) → 201 `{id}`:
      - `GET /admin/perfumes/{id}` lists the photo at position 1 with width 1200 and height
        900.
      - Both URLs return 200 `image/webp` without credentials.
      - The card rendition's longest side is 400 px.
      - Neither file has EXIF.
- [x] A PNG with transparency is accepted and stays transparent. A GIF, a text file renamed
      `.jpg` and a 500×800 image → 422 with `CATALOG_PHOTO_FORMAT_INVALID`,
      `CATALOG_PHOTO_FORMAT_INVALID` and `CATALOG_PHOTO_TOO_SMALL`. A file over 10 MB → 422
      `CATALOG_PHOTO_TOO_LARGE`.
- [x] A 4th photo → 409 `CATALOG_PHOTO_LIMIT_REACHED`, and **no new object is left in the
      bucket** (compensation).
- [x] `PUT …/photos/order` with the three ids reversed → 204, and the positions follow. A list
      missing an id, or with a foreign id → 422 `CATALOG_PHOTO_ORDER_INVALID`.
- [x] `DELETE …/photos/{id}` → 204: the remaining photos are renumbered 1..n and both objects
      are gone from the bucket. An unknown photo → 404 `CATALOG_PHOTO_NOT_FOUND`.
- [x] While the perfume is archived, upload, delete and reorder → 422 `CATALOG_PERFUME_ARCHIVED`.
- [x] The storefront list shows `photo_url` = position 1's card URL, or `null` without photos.
      The public detail lists `photos` in order.
- [x] Every new route returns 401 without a session.
- [x] Migration 0009 applies. `openapi.json` is regenerated and the drift check passes.
      `uv run just bootstrap` is idempotent with the new policy step.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `add_photo` limit and positions; `remove_photo` renumbers and records the id; `reorder_photos` set equality (missing, extra, duplicate, foreign); archived refusals; `photo_keys` |
| application | yes     | `AddPhoto`: processor Err passes through without upload; compensation deletes both keys on not-found, limit and archived; a failing compensation delete does not mask the Err. `RemovePhoto` deletes the keys only after Ok, and a failing delete is still Ok. `ReorderPhotos` |
| http        | yes     | Multipart upload (field name, empty file, too large → 422 without calling the use case); delete and reorder codes; 401/403; new contract fields in admin detail, public card and detail |
| integration | yes     | `PillowImageProcessor` on real JPEG/PNG(alpha)/WebP/GIF/garbage/tiny/EXIF-rotated inputs (sizes, format, no EXIF); `S3MediaStorage` put/delete round trip on local RustFS and public GET; repository photos load/save, deferrable reorder, removal; queries' URLs and `photo_url` at position 1; settings validator |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

All cosmetic, fixed forward:

- Step 1: `uv add` only added `pillow` and `python-multipart`; `boto3` was already a dependency of
  `apps/api`.
- Step 4: `PhotoUploadLimit` is defined in `application/commands/photos.py` (the router and
  `module.py` import it from there). structlog is allowed in the application layer, so the
  delete failures are logged there and no port change was needed.
- Step 5: `S3MediaStorage` imports `boto3`/`botocore` with `# type: ignore[import-untyped]`
  (no stubs; mypy strict). `SqlPerfumeQueries` takes `media_base_url` keyword-only;
  `InMemoryPerfumes` takes it as an optional keyword (default `http://media.test`).
- Step 7: the upload route takes a named `AddPhotoForm` (pydantic model with `file: UploadFile`,
  `Form()`), defined in `perfume_router.py`, so the OpenAPI schema is `AddPhotoForm` and not
  `Body_add_photo` (rejected by `test_schema_names_are_readable`).
- Existing tests touched to keep `check` green (outside the file list; additive contract fields and
  the new constructor argument): `tests/integration/catalog/test_sql_public_perfumes.py` (passes
  `media_base_url`), `tests/unit/catalog/test_perfume_http.py` and `test_public_perfume_http.py`
  (exact key-set assertions gained `photos` / `photo_url`). `plans-scope` reports clean.
- Step 8: `ensure_public_read` keeps other statements of an existing policy and only manages its
  own `Sid`.

### Repair round 1 (2026-10-11, main session: review → implementing)

**Why.** The review found H1, M1–M3 and L1–L4. The user decided:
- H1 → the upload becomes a raw binary body (README decision 54).
- Every Medium and Low finding is fixed in this round (decision 55).

The earlier test and review evidence stays valid for everything else. The tester adapts the
upload tests and adds regressions afterwards.

**Repair spec:**

1. **H1, the upload route** (`http/perfume_router.py`): `POST
   /admin/perfumes/{perfume_id}/photos` takes **no form or file parameters**, only
   `request: Request`, so the admin dependencies run before any body is read.
   1. Read the `Content-Type` header. Anything other than `image/jpeg`, `image/png` or
      `image/webp` → 422 `CATALOG_PHOTO_FORMAT_INVALID`. The bytes are still sniffed by
      Pillow afterwards.
   2. Read the `Content-Length` header. If present and > `PhotoUploadLimit.max_bytes` → 422
      `CATALOG_PHOTO_TOO_LARGE` immediately, before reading the body.
   3. Accumulate `request.stream()` chunks. Stop and answer `CATALOG_PHOTO_TOO_LARGE` as soon
      as the total passes the limit.
   4. An empty body → `CATALOG_PHOTO_FORMAT_INVALID`.

   Declare the request body in OpenAPI with `openapi_extra`: three `image/*` content types,
   `format: binary`. Remove `AddPhotoForm`. Regenerate `openapi.json`.
2. **M1:** accept Pillow format `MPO` as JPEG, and use its first frame.
3. **M2:** add a `PHOTO_MAX_PIXELS = 40_000_000` constant. After `Image.open` and **before**
   `load`/`exif_transpose`, a width × height above it → `PhotoTooLarge` with details
   `{"max_bytes": …, "max_pixels": 40000000}`.
4. **M3** (`s3_media_storage.py`): `delete` reads the `Errors` list of each `delete_objects`
   response. It raises, or returns the failed keys, so that `_delete_quietly` logs them with
   their codes. Never silent.
5. **L1:** convert 16-bit and other high bit-depth modes (`I;16*`, `I`, `F`) to 8-bit `L`
   correctly, by scaling and not clipping, before RGB.
6. **L2:** when the image has an ICC profile, convert it to sRGB with `ImageCms`
   (`profileToProfile`, perceptual intent), then drop the profile. On a profile error, fall
   back to the plain conversion.
7. **L3:** `Image.open(..., formats=["JPEG", "PNG", "WEBP", "MPO"])`.
8. **L4:** the boto3 client uses
   `botocore.config.Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3,
   "mode": "standard"})`.
9. **Correct the step 1 deviation.** `boto3` was **not** already an `apps/api` dependency: this
   diff adds it to `apps/api/pyproject.toml`.

Update the plan's "Rules fixed here", or record the differences under this subsection. The
differences are: raw body instead of multipart, MPO, the pixel cap, and sRGB conversion
instead of only dropping the ICC profile.

**Repair round 1 implemented (2026-10-11, implementer).** All 9 items done, product code only.

- H1: `add_photo` takes `request: Request` and no form parameters; `AddPhotoForm`, `Form` and
  `UploadFile` are gone; `openapi_extra` declares the three `image/*` binary bodies;
  `apps/api/openapi.json` regenerated. Order: content type (422 `CATALOG_PHOTO_FORMAT_INVALID`),
  declared `Content-Length` (422 `CATALOG_PHOTO_TOO_LARGE`), streamed accumulation cut off at the
  limit, empty body (`FORMAT_INVALID`). Probe against the in-process app (scratchpad, not
  committed): an unauthenticated POST with a 1000 x 1 KiB streamed body got 401 with 0 chunks
  pulled; an admin POST with the same body got 422 `CATALOG_PHOTO_TOO_LARGE` after 1 chunk.
- M1/L3: `Image.open(..., formats=["JPEG", "PNG", "WEBP"])`; `MPO` is accepted in the format
  check. Pillow has no MPO opener of its own (the JPEG one returns `MPO` files), so `"MPO"` in
  `formats=` raises `KeyError`; it is only in the accepted set. Frame 0 is used. Probe: a
  two-frame MPO is processed.
- M2: `PHOTO_MAX_PIXELS = 40_000_000`, checked from the header before `load`; `PhotoTooLarge`
  details `{"max_bytes", "max_pixels"}`. Because the processor needs `max_bytes`,
  `PillowImageProcessor` now takes a required keyword `max_bytes` (wired in `module.py` from
  `photo_max_bytes`). The `ImageProcessor` port and `FakeImageProcessor` error unions gained
  `PhotoTooLarge`.
- M3: `S3MediaStorage.delete` collects `Errors` of every `delete_objects` response and raises
  `RuntimeError` listing `key: code`; `_delete_quietly` already logs exceptions with the keys.
- L1: `_to_8bit` scales `I;16*` (full range to 0..255) and `I`/`F` (by extrema) to `L`.
- L2: `_to_srgb` converts an ICC profile to sRGB (`profileToProfile`, perceptual) on the RGB/RGBA
  image and drops the profile; any `PyCMSError`/`OSError`/`ValueError` falls back to the plain
  conversion.
- L4: boto3 `Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3, "mode":
  "standard"})` besides path addressing.
- Item 9: the Step 1 deviation above is wrong. `boto3>=1.43.107` was **not** already a dependency
  of `apps/api`; the original diff added it to `apps/api/pyproject.toml` (together with `pillow`
  and `python-multipart`). `python-multipart` is no longer needed by this route; I left the
  dependency untouched (no dependency change was in the 9 items).
- Existing tests touched only as the contract change forces (the tester adapts and extends them):
  `unit/catalog/test_photo_http.py` (upload helper and route kwargs send a raw body with
  `Content-Type`; the three multipart-only tests became "JSON body is 422 FORMAT_INVALID", "empty
  body is 422 FORMAT_INVALID without calling the processor", "a non-image content type is 422
  FORMAT_INVALID"), `unit/catalog/test_s3_media_storage.py` (the fake `delete_objects` returns
  `{}` like boto3), `integration/catalog/test_pillow_image_processor.py` and
  `test_sql_perfume_photos.py` (`PillowImageProcessor(max_bytes=5_000_000)`).
- Results: `uv run just check` green (1243 unit passed, 3 skipped; harness 684 passed);
  `uv run just test-integration` green (318 passed, 2 skipped). No server was left running.

## Test coverage

Tester phase, 2026-10-11. Source lines are in `apps/api/src/fragancia_api/modules/catalog/`.

**Runs.**
- Baseline, before any test: `uv run just check` green (1136 passed, 3 skipped; harness 684 passed),
  `uv run just test-integration` green (236 passed, 2 skipped).
- Closing: `uv run just check` green (ruff, mypy 221 files, arch, 1244 unit passed, 3 skipped, harness
  684 passed, plans-lint, harness-check); `uv run just test-integration` green (318 passed, 2 skipped,
  both skips pre-existing identity migration round trips). Difference: +108 unit, +82 integration, 0
  failures, 0 new skips, 0 xfail.
- Local RustFS (`127.0.0.1:9100`, bucket `fragancia-media`, public-read `perfumes/*`) was reachable, so
  no S3 test was skipped. If it is not, the `bucket` fixture
  (`tests/integration/catalog/conftest.py`) skips with `NOT CONFIRMED: ...`; a bad credential is a
  failure, not a skip.

**Migration 0009 round trip, test database only** (`fragancia_test`, by hand; not automated for the
same race reason as the identity ones):
1. `alembic -x test=true current` -> `0009 (head)`.
2. `alembic -x test=true downgrade 0008` -> `Running downgrade 0009 -> 0008`; `current` -> `0008`;
   `\d catalog.perfume_photos` -> `Did not find any relation`.
3. `uv run just db-migrate --test` -> `Running upgrade 0008 -> 0009, catalog perfume photos`.
4. `\d catalog.perfume_photos` shows the 8 columns, all not null, `pk_perfume_photos`,
   `ix_perfume_photos_perfume_id`, `fk_perfume_photos_perfume_id_perfumes` and
   `uq_perfume_photos_position UNIQUE ... DEFERRABLE INITIALLY DEFERRED`.
   The development database was not touched.

**Tests added.**

| Layer | Count | Files (`apps/api/tests/`) |
| ----- | ----- | ------------------------- |
| domain | 25 | `unit/catalog/test_perfume_photos_domain.py` |
| application | 22 | `unit/catalog/test_photo_commands.py` |
| http | 40 | `unit/catalog/test_photo_http.py` |
| adapter (unit, fake boto3) | 7 | `unit/catalog/test_s3_media_storage.py` |
| settings | 14 | `unit/test_settings.py` (appended) |
| integration | 82 | `integration/catalog/test_pillow_image_processor.py` (35), `test_s3_media_storage.py` (7), `test_sql_perfume_photos.py` (40); helpers `images.py`, `conftest.py` |

**Matrix.** Every row is CONFIRMED (code read plus a local run); there are no GAP and no NOT
CONFIRMED rows, except the one marked below.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| Constants 3 / 600 / 400 / 1200; `photo_keys` names | `domain/perfume.py:106-107, 297-300` | domain | `test_perfume_photos_domain.py::test_the_photo_rules_are_the_ones_fixed_by_the_plan`, `::test_photo_keys_live_under_the_perfume_prefix_and_name_both_renditions` | CONFIRMED |
| `add_photo`: next position, 4th refused with `{"max": 3}`, archived refused first | `domain/perfume.py:534-559` | domain | `::test_a_new_photo_takes_the_next_position_and_keeps_its_fields`, `::test_rejects_a_fourth_photo_with_the_limit_in_the_details`, `::test_rejects_adding_a_photo_to_an_archived_perfume`, `::test_an_archived_perfume_at_the_limit_answers_archived_first` | CONFIRMED |
| `remove_photo`: returns the photo, renumbers 1..n, records the id, not found, archived | `domain/perfume.py:561-571` | domain | `::test_removing_*`, `::test_removed_ids_accumulate_across_removals`, `::test_a_removed_slot_can_be_filled_again`, `::test_rejects_removing_*` | CONFIRMED |
| `reorder_photos`: exact set (missing, extra, duplicated, foreign, empty), archived | `domain/perfume.py:573-583` | domain | `::test_reordering_*`, `::test_rejects_an_order_that_is_not_the_current_set[*]`, `::test_rejects_reordering_an_archived_perfume` | CONFIRMED |
| Publishing needs no photo (decision 24) | `domain/perfume.py:434-442` | domain | `::test_publishing_does_not_need_a_photo` | CONFIRMED |
| `AddPhoto`: both renditions uploaded as `image/webp`, saved with the clock, fresh id and next position | `application/commands/photos.py:AddPhoto.execute` | application | `test_photo_commands.py::test_adds_the_photo_uploads_both_renditions_and_saves_it_with_the_clock`, `::test_each_upload_gets_a_fresh_id_and_the_next_position` | CONFIRMED |
| Processor Err passes through, no upload, no change | same | application | `::test_a_processor_error_passes_through_without_uploading_or_touching_the_perfume[*]` | CONFIRMED |
| Compensation deletes both keys on not-found, limit (only its own keys), archived, save conflict, transaction exception, failed second put | same | application | `::test_an_unknown_perfume_is_not_found_and_the_uploaded_objects_are_deleted`, `::test_the_fourth_photo_is_refused_and_only_its_own_objects_are_deleted`, `::test_an_archived_perfume_refuses_the_photo_and_the_objects_are_deleted`, `::test_a_save_conflict_is_returned_and_compensated`, `::test_an_exception_in_the_transaction_deletes_the_objects_and_propagates`, `::test_a_failing_upload_deletes_what_was_put_and_raises` | CONFIRMED |
| A failing compensation delete does not mask the Err and is logged with the keys | same | application | `::test_a_failing_compensation_delete_does_not_mask_the_domain_error`, `::test_a_failed_compensation_delete_is_logged_with_the_keys` | CONFIRMED |
| `RemovePhoto`: deletes keys only after the transaction; failing delete still Ok and logged; not found / archived delete nothing | `application/commands/photos.py:RemovePhoto.execute` | application | `::test_removes_the_photo_*`, `::test_the_objects_are_deleted_only_after_the_transaction_finished`, `::test_a_failing_object_delete_still_removes_the_photo_and_is_logged`, `::test_unknown_perfume_or_photo_is_not_found_and_deletes_nothing[*]`, `::test_an_archived_perfume_refuses_the_removal_and_deletes_nothing` | CONFIRMED |
| `ReorderPhotos`: saves positions; invalid / unknown / archived | `application/commands/photos.py:ReorderPhotos.execute` | application | `::test_reorders_the_photos_and_saves_the_positions`, `::test_rejects_an_order_*`, `::test_reordering_an_*` | CONFIRMED |
| Raw-body upload (superseded multipart, repair round 1): the body is the image, `Content-Type` image/jpeg, png or webp; 201 `{id}`; admin detail lists the photo with URLs | `http/perfume_router.py:add_photo` | http | `test_photo_http.py::test_a_raw_body_upload_is_201_and_the_admin_detail_lists_the_photo` | CONFIRMED |
| Size limit: exactly max accepted; max+1 is 422 `CATALOG_PHOTO_TOO_LARGE` with `max_bytes`, use case not called | `http/perfume_router.py:add_photo` | http | `::test_an_upload_exactly_at_the_limit_is_accepted`, `::test_an_upload_over_the_limit_is_422_too_large_without_calling_the_use_case` | CONFIRMED |
| JSON body, empty body and a non-image content type are `CATALOG_PHOTO_FORMAT_INVALID` without calling the processor; malformed id is `VALIDATION_ERROR` | same | http | `::test_a_json_body_is_422_format_invalid_by_its_content_type`, `::test_an_empty_body_is_422_format_invalid_without_calling_the_processor`, `::test_a_content_type_outside_the_three_image_types_is_422_format_invalid`, `::test_a_malformed_perfume_id_is_a_validation_error` | CONFIRMED |
| Error codes: 422 format / too small, 409 limit, 404, 422 archived (upload, delete, reorder); delete 204 renumbers and deletes objects; reorder 204; order invalid 422; `PUT .../photos/order` not swallowed by `{photo_id}` | `http/perfume_router.py:254-273` | http | `::test_processor_errors_are_422_with_their_codes[*]`, `::test_the_fourth_upload_is_409_and_leaves_no_new_object`, `::test_delete_*`, `::test_deleting_*`, `::test_reorder_is_204_and_the_positions_follow`, `::test_an_order_that_is_not_the_current_set_is_422[*]`, `::test_order_is_a_route_of_its_own_not_a_photo_id` | CONFIRMED |
| More than 3 ids in the reorder body is `VALIDATION_ERROR` from the contract (the plan names `ORDER_INVALID` for "extra"; with 4 ids the contract answers first, still 422; a 3rd extra id with 2 photos is `ORDER_INVALID`) | `contracts.py:211-214` | http | `::test_more_than_three_ids_is_a_validation_error_from_the_contract` | CONFIRMED (see Deviations note below) |
| 401 without session and 403 without `catalog:manage` on the 3 routes; all admin routes protected | `assert_admin_routes_are_protected` | http | `::test_photo_routes_return_401_without_a_session[*]`, `::test_photo_routes_return_403_for_an_admin_without_catalog_manage[*]` | CONFIRMED |
| New contract fields: admin `photos`, public card `photo_url` (null / position 1 / follows reorder), public detail `photos` (order, only two URLs, removed disappears) | `contracts.py:199-333`, `in_memory_perfumes.py` | http | `::test_the_admin_detail_has_an_empty_photo_list_*`, `::test_the_public_card_*`, `::test_the_public_detail_*`, `::test_a_removed_photo_disappears_from_the_public_detail` | CONFIRMED |
| `S3MediaStorage` talks to boto3 correctly: lazy single client, path addressing, `put_object` args incl. immutable cache header, quiet batched `delete_objects` (1000 per call), no call for no keys | `infrastructure/s3_media_storage.py` | adapter (unit) | `test_s3_media_storage.py::*` | CONFIRMED |
| Settings: defaults, `MEDIA_PUBLIC_BASE_URL` scheme and no trailing slash, `photo_max_bytes >= 1`, secret key masked | `config.py:73-90` | settings (unit) | `test_settings.py::test_media_*`, `::test_photo_max_bytes_*`, `::test_the_s3_secret_key_is_a_secret_that_does_not_show_in_the_repr` | CONFIRMED |
| `PillowImageProcessor`: JPEG 2000x1500 gives 1200x900 / 400x300 WebP, portrait, never upscaled, 600 boundary (600 accepted, 599 too small, shorter side only) | `infrastructure/pillow_image_processor.py:32-50` | integration | `test_pillow_image_processor.py::test_a_large_jpeg_*`, `::test_a_portrait_*`, `::test_renditions_are_never_upscaled`, `::test_an_image_of_exactly_the_minimum_side_*`, `::test_the_minimum_applies_*`, `::test_an_image_with_a_shorter_side_under_600_is_too_small[*]` | CONFIRMED |
| No EXIF / ICC in the renditions even when the source has both; colors survive | same | integration | `::test_the_source_has_exif_and_an_icc_profile_but_the_renditions_have_none`, `::test_the_red_and_blue_halves_survive_the_conversion` | CONFIRMED |
| EXIF orientation applied before sizing and to the pixels | same | integration | `::test_exif_orientation_is_applied_before_sizing`, `::test_exif_orientation_rotates_the_pixels_not_just_the_size`, `::test_an_image_without_orientation_keeps_its_pixels` | CONFIRMED |
| Alpha kept for RGBA PNG and palette PNG with transparency, RGB otherwise; L and CMYK JPEG become RGB; JPEG/PNG/WebP all accepted | same | integration | `::test_a_png_with_transparency_stays_transparent`, `::test_an_opaque_png_*`, `::test_a_palette_png_*`, `::test_grayscale_and_cmyk_jpegs_come_out_as_rgb[*]`, `::test_jpeg_png_and_webp_inputs_all_come_out_as_webp[*]` | CONFIRMED |
| GIF / BMP / garbage / empty / truncated / decompression bomb are `FORMAT_INVALID` with `{"formats": [...]}`; a small GIF is a format error, not a size error; the guard is not disabled | same | integration | `::test_a_gif_*`, `::test_a_bmp_*`, `::test_a_small_gif_*`, `::test_garbage_is_a_format_error[*]`, `::test_a_truncated_jpeg_is_a_format_error[*]`, `::test_a_decompression_bomb_is_a_format_error`, `::test_the_decompression_guard_is_not_disabled_for_the_whole_process` | CONFIRMED |
| `S3MediaStorage` on RustFS: put then public GET without credentials (200, `image/webp`, immutable cache), delete removes and the URL 404s, missing key not an error, empty list no-op, overwrite, wrong secret raises, only `perfumes/*` is public | `infrastructure/s3_media_storage.py` | integration | `test_s3_media_storage.py::*` | CONFIRMED |
| Table: unique `(perfume_id, position)` per perfume, constraint deferred (two-statement swap passes, a duplicate left at commit fails and writes nothing), FK, truncate cascade | `infrastructure/tables.py:111-130`, migration 0009 | integration | `test_sql_perfume_photos.py::test_a_position_is_unique_*`, `::test_the_position_constraint_is_deferred_until_commit`, `::test_a_duplicate_position_left_at_commit_*`, `::test_a_photo_needs_an_existing_perfume`, `::test_truncating_the_perfumes_*` | CONFIRMED |
| Repository load: ordered by position, every field, only that perfume's photos, empty list | `sql_perfume_repository.py:224-235` | integration | `::test_get_for_update_loads_*`, `::test_a_perfume_without_photos_loads_an_empty_list` | CONFIRMED |
| Repository save: insert, deferrable reorder (reverse and swap), removal with renumbering and cleared `removed_photo_ids`, remove + add in one save, other perfumes untouched, only `position` is updated for an existing photo | `sql_perfume_repository.py:258-285` | integration | `::test_save_inserts_*`, `::test_reordering_rewrites_*`, `::test_swapping_two_photos_is_saved`, `::test_removing_*`, `::test_save_only_updates_the_position_of_an_existing_photo` | CONFIRMED |
| Row lock serializes concurrent uploads (5 concurrent: 3 Ok, 2 `LIMIT_REACHED`, positions 1..3, refused uploads compensated) | `sql_perfume_repository.py` `get_for_update` + `AddPhoto` | integration | `::test_concurrent_uploads_to_one_perfume_are_serialized_by_the_row_lock` | CONFIRMED |
| Queries: `get_admin` / `get_public` photos by position with URLs `f"{base}/{key}"`, retired slug keeps photos, `photo_url` is position 1's card URL (own perfume per card, follows reorder, none without photos, no duplicated cards), hidden perfume leaks nothing | `sql_perfume_queries.py:156-158, 305-335, 412-453` | integration | `::test_get_admin_*`, `::test_a_trailing_part_of_the_base_url_is_kept_verbatim`, `::test_list_public_*`, `::test_each_card_gets_the_photo_of_its_own_perfume`, `::test_get_public_*`, `::test_a_hidden_perfume_does_not_leak_its_photos_publicly` | CONFIRMED |
| Real stack (container wiring: Pillow + S3 + SQL): upload gives two public WebP objects with the right sizes, no EXIF, URLs under `media_public_base_url`; PNG alpha stays transparent in the stored object | `module.py:149-215` | integration | `::test_an_uploaded_jpeg_is_stored_as_two_public_webp_renditions_and_listed`, `::test_a_png_with_transparency_stays_transparent_in_the_stored_object` | CONFIRMED |
| Real stack: refused file / 4th photo / archived / unknown perfume leave no new object in the bucket and no row; remove deletes both objects, renumbers, keeps the others; unknown photo deletes nothing; objects already gone is still Ok; reorder and invalid order; a storage failure raises and leaves no row | same | integration | `::test_a_refused_file_leaves_no_object_and_no_row[*]`, `::test_the_fourth_photo_is_refused_and_no_new_object_is_left_in_the_bucket`, `::test_an_archived_perfume_refuses_every_photo_operation_and_keeps_its_objects`, `::test_uploading_to_an_unknown_perfume_leaves_no_object`, `::test_removing_*`, `::test_reordering_through_the_container_*`, `::test_a_storage_failure_raises_and_leaves_no_row` | CONFIRMED |
| The 0009 migration round trip | `migrations/versions/0009_catalog_perfume_photos.py` | by hand | see "Migration 0009 round trip" above | NOT CONFIRMED in the suite (run by hand, passed; downgrading `fragancia_test` inside the suite would race the other integration tests) |

**Not covered, said plainly.**
- Offloading to a thread (`asyncio.to_thread`) is not asserted; only behavior is.
- The processor's WebP `quality=82` / `method=6` are not asserted (Pillow does not expose the quality of a
  decoded WebP).
- Large-scale delete batching (more than 1000 keys) is tested only against the fake boto3 client, not
  against RustFS.
- The production bucket policy and CDN are out of scope (plan).
- `plans-scope` was not run by the tester; no product file was touched (only tests and this section).

**Observation (no GAP).** The plan's reorder rule lists "extra" ids as `CATALOG_PHOTO_ORDER_INVALID`;
`ReorderPhotosRequest(max_length=3)` makes a body of 4 ids a `VALIDATION_ERROR` before the use case.
Both are 422. With fewer than 3 photos an extra id reaches the domain and is `ORDER_INVALID`
(tested). Left to the reviewer to decide whether the plan text or the contract should change.

### Test coverage: repair round 1 (2026-10-11, tester)

Regressions for H1, M1-M3 and L1, L2, L4 (L3 is covered by M1: only JPEG/PNG/WebP/MPO inputs are
accepted and GIF/BMP/garbage stay `FORMAT_INVALID`). Product code untouched. Superseded: the three
multipart-only http rows above (replaced by the raw-body rows); the earlier closing counts.

**Runs.**
- Baseline (repair code, before any new test): `uv run just check` green, `uv run just
  test-integration` green (318 passed, 2 skipped).
- Closing: `uv run just check` green (ruff, mypy 221 files, arch, 1253 unit passed, 3 skipped,
  harness 684 passed, plans-lint, harness-check, compose config); `uv run just test-integration`
  green (329 passed, 2 skipped, the pre-existing identity round trips). Difference: +10 unit, +11
  integration, 0 failures, 0 xfail, 0 new skips. RustFS was reachable.

| Finding | Behavior | Layer | Test (`apps/api/tests/`) | State |
| ------- | -------- | ----- | ------------------------ | ----- |
| H1 | A chunked body over the limit is 422 `CATALOG_PHOTO_TOO_LARGE` and the read stops early (fewer chunks pulled than offered); a chunked body exactly at the limit is accepted | http | `unit/catalog/test_photo_http.py::test_a_streamed_body_over_the_limit_is_422_too_large_and_cut_off`, `::test_a_streamed_body_exactly_at_the_limit_is_accepted` | CONFIRMED |
| H1 | A declared `Content-Length` over the limit is 422 `TOO_LARGE` with zero chunks pulled | http | `::test_a_declared_content_length_over_the_limit_is_422_without_reading_the_body` | CONFIRMED |
| H1 | Unauthenticated streamed body (1000 x 1 KiB) is 401 with 0 chunks consumed; admin without `catalog:manage` is 403 with 0 chunks | http | `::test_an_unauthenticated_streamed_body_is_401_and_no_chunk_is_consumed`, `::test_a_streamed_body_without_catalog_manage_is_403_and_no_chunk_is_consumed` | CONFIRMED |
| M1 | A two-frame MPO (a phone JPEG) is accepted, first frame used (size and colors) | integration | `integration/catalog/test_pillow_image_processor.py::test_a_multi_picture_jpeg_is_accepted_and_uses_its_first_frame` | CONFIRMED |
| M2 | A 42 MP 1-bit PNG (a few KB) is `CATALOG_PHOTO_TOO_LARGE` with `{"max_bytes", "max_pixels": 40000000}`; refused before any decode (`ImageFile.load` patched to fail); exactly 40 MP accepted | integration | `::test_more_than_40_megapixels_is_refused_with_the_cap_in_the_details`, `::test_the_pixel_cap_is_checked_before_any_pixel_is_decoded`, `::test_exactly_40_megapixels_is_accepted` | CONFIRMED |
| M3 | `delete_objects` `Errors` are raised naming `key: code`; errors of every batch are collected and no batch is skipped; an empty `Errors` is success | adapter (unit, fake boto3) | `unit/catalog/test_s3_media_storage.py::test_delete_errors_reported_in_the_response_are_raised_with_key_and_code`, `::test_errors_of_every_batch_are_collected_before_raising`, `::test_an_empty_errors_list_is_success` | CONFIRMED |
| M3 | With the real `S3MediaStorage` reporting `AccessDenied`, `RemovePhoto` is still Ok, the row is gone and a warning `catalog_photo_objects_not_deleted` is logged with both keys and `exc_info` | application | `unit/catalog/test_photo_commands.py::test_s3_per_key_delete_errors_are_logged_and_the_removal_still_succeeds` | CONFIRMED |
| L1 | A 16-bit grayscale PNG is scaled over the full range (0 -> 0, 10000 -> 39, 30000 -> 117, 65535 -> 255), not clipped to white | integration | `::test_a_16_bit_grayscale_png_is_scaled_not_clipped[*]` | CONFIRMED |
| L2 | A Display P3 image (profile built by hand in `images.display_p3_profile`) is converted: red up, green and blue down versus the raw values, no ICC profile in either rendition; no profile means no color shift; an unreadable profile falls back to the plain conversion | integration | `::test_a_display_p3_image_is_converted_to_srgb_and_leaves_no_profile`, `::test_an_image_without_a_profile_is_not_color_shifted`, `::test_an_unreadable_profile_falls_back_to_the_plain_conversion` | CONFIRMED |
| L4 | boto3 client is built with `connect_timeout=5`, `read_timeout=30`, `retries={"max_attempts": 3, "mode": "standard"}` | adapter (unit) | `unit/catalog/test_s3_media_storage.py::test_the_client_has_timeouts_and_standard_retries` | CONFIRMED |

**Not covered, said plainly.**
- The logged exception text (the `key: code` list) is asserted on the adapter's exception, not in the
  rendered log line: `structlog.testing.capture_logs` records `exc_info=True`, not the message.
- The P3 profile is a hand-built matrix/TRC profile with the Display P3 primaries, not Apple's file;
  the assertions are directional (red up, green and blue down), not exact values.
- The retries and timeouts are asserted as configuration; no network failure was simulated.
- The MPO test builds the file with Pillow's own MPO writer, not a real phone photo.
- The existing 196 MP bomb test still passes (Pillow refuses it on open, `FORMAT_INVALID`); the 40 MP
  cap is the `TOO_LARGE` path for 40-179 MP.

## Review findings

Reviewer, 2026-10-11, `git diff main...HEAD` on `feat/catalog-photos` (worktree clean). Source
paths below are relative to `apps/api/src/fragancia_api/modules/catalog/` unless they are full.

### Pass 1 — Checklist: 13/15 passed, 1 failed, 1 not applicable

- [x] `plans-scope`: "26 declared, 39 changed … Every change is inside the plan". No hot file
      (`container.py`, `docs/modules.json`, `.importlinter`) changed.
- [x] `uv run just check` green (ruff, mypy, arch, 1244 unit passed / 3 skipped, 684 harness,
      plans-lint, harness-check, compose config).
- [x] `uv run just test-integration` green (318 passed, 2 skipped, the pre-existing identity
      round trips).
- [x] Business rules in `domain/perfume.py` (limit, positions, renumbering, order set). File
      rules live in the image adapter as the plan says; the router only enforces the byte limit.
- [x] CQRS-lite: the three commands go `get_for_update` → aggregate → `save` inside
      `TransactionRunner.run` and return `Result`; reads go through `SqlPerfumeQueries`.
- [x] Contracts in `contracts.py`; `openapi.json` regenerated (drift check green).
      `AddPhotoForm` lives in the router (recorded deviation; it is a transport model).
- [x] Stable codes `CATALOG_PHOTO_*`; expected errors are `Err`.
- [x] UTC datetime from `Clock`; ids from `new_id()`; no money involved.
- [x] Migration 0009 new, `0008` → `0009`, no cross-schema FK, constraint
      `DEFERRABLE INITIALLY DEFERRED`, reversible (round trip run by hand on the test DB by the
      tester).
- [x] Routes on `admin_perfumes` (`admin_router` + `catalog:manage`); 401/403 tested.
- [x] Wiring in `module.py` resolves; adapters created only there.
- [x] No secrets: `.env.example` gets only `MEDIA_PUBLIC_BASE_URL`; the secret stays a
      `SecretStr` until `module.py` unwraps it for boto3.
- [ ] **`## Deviations` honest — FAILED (spot-check).** "Step 1: `boto3` was already a
      dependency of `apps/api`" is false: `git show main:apps/api/pyproject.toml` has no
      `boto3`, and this diff adds `"boto3>=1.43.107"` to `apps/api/pyproject.toml`. Harmless
      (adding it was the plan), but the record is wrong. Second spot-check (structlog allowed in
      `application/`) is true: `.importlinter` `application-is-framework-free` does not forbid
      it.
- [x] Docs: `docs/architecture.md` catalog row updated; `apps/api/README.md` has no per-setting
      list to update.
- [n/a] PR body: no PR exists yet for `feat/catalog-photos`.

### Pass 2 — Findings

**High**

- **H1. The 10 MB limit does not bound what the server receives, and the body is received
  before authentication.** `http/perfume_router.py:248-249` reads at most `max_bytes + 1`
  from `form.file`, but by then Starlette has already spooled the whole multipart part to a
  temporary file (Starlette 1.7 `MultiPartParser.max_part_size` caps only non-file fields).
  FastAPI parses the form before solving dependencies (`.venv/…/fastapi/routing.py:441-443`
  `await request.form()` precedes `solve_dependencies` at line 499), and `require_admin` /
  `require_permission` are dependencies. Scenario: an anonymous client streams a 20 GB
  multipart body to `POST /api/v1/admin/perfumes/<any uuid>/photos`; the API writes 20 GB to
  the temp directory and only then answers 401. A few in parallel fill the disk of the VPS
  (PostgreSQL shares it locally). This route is the first multipart route and
  `python-multipart` is new in this plan, so the vector is new. A FastAPI dependency cannot fix
  it (it runs after the parse): it needs either a size-checking ASGI middleware / raw
  `request.stream()` handling for this route, or an explicit decision to rely on a reverse
  proxy `client_max_body_size` as a rollout step. **Needs a user decision** (fix in this plan
  vs. accept with a rollout step); the general, pre-existing JSON variant is recorded as
  finding `plans/findings/platform-unbounded-request-body.md`.

**Medium**

- **M1. Multi-picture JPEGs (MPO) are rejected as `CATALOG_PHOTO_FORMAT_INVALID`.**
  `infrastructure/pillow_image_processor.py:31` accepts only `{"JPEG","PNG","WEBP"}`, but
  Pillow 12.3 reports any JPEG carrying an MPF index with more than one image as `"MPO"`.
  Reproduced: a two-frame JPEG built with Pillow opens as `format == "MPO"` and the processor
  answers `CATALOG_PHOTO_FORMAT_INVALID`. Scenario: the admin uploads a JPEG straight from a
  phone or camera that embeds a secondary image (iPhone JPEGs with an HDR gain map, many
  Samsung/Fujifilm/Nintendo files) and gets "must be a JPEG, PNG or WebP image" for a `.jpg`
  that is a JPEG. Plan rule: "Pillow must recognize it as JPEG". Fix direction: accept `MPO`
  (only the first frame is used) — uncertain only in how common such files are among this
  business's sources.
- **M2. Pixel-count guard lets a 20 KB file allocate about 1.5 GB.** The plan keeps Pillow's
  default guard, which only *warns* between 89.5 M and 179 M pixels. Reproduced: a 13000×13000
  1-bit PNG of 20,588 bytes is accepted, takes 1.3 s and raises the process max RSS to
  1,542 MB (the `convert("RGB")` plus copies in `_process`, lines 33-42). Scenario: an admin
  (or a stolen admin session, or plan 007's import of URL images) submits a few such files in
  parallel; `asyncio.to_thread` runs them concurrently and the API process is OOM-killed on a
  small VPS. Fix direction: an explicit pixel cap in the processor (e.g. reject above ~40 MP
  as `FORMAT_INVALID`/a size error) checked on `opened.size` before `load()`, and/or JPEG
  `draft()` downscaling. This is within the plan's rule ("guard stays on") but defeats the
  intent of the decompression-bomb rule.
- **M3. Failed object deletes are not logged.** `infrastructure/s3_media_storage.py:55-61`
  calls `delete_objects` with `"Quiet": True` and discards the response. S3 (and RustFS)
  report per-key failures such as `AccessDenied` inside a 200 response (`Errors` list), not as
  an exception, so `_delete_quietly` (`application/commands/photos.py:32-37`) never sees them.
  Scenario: production credentials lack `s3:DeleteObject`; every removal and every
  compensation leaves both objects behind and nothing is logged, contrary to the plan ("A
  failed delete is logged and leaves an orphan object") and to the later orphan cleanup that
  relies on those logs. Fix direction: raise (or return) when `response.get("Errors")` is
  non-empty, keeping "missing key is not an error".

**Low**

- **L1. 16-bit grayscale PNGs come out white.** `pillow_image_processor.py:38`
  `convert("RGB")` from mode `I;16` clips: an 800×800 PNG at 30000/65535 gray (mid gray) is
  stored as `(255, 255, 255)` (reproduced). Rare for product photos; fix by
  normalising `I;16`/`I` to `L` first, or reject them.
- **L2. ICC profiles are dropped without converting to sRGB.** Plan-level rule ("no EXIF, ICC")
  implemented as written, but Display P3 / Adobe RGB photos (iPhone JPEGs are P3) will look
  desaturated. Converting to sRGB with `ImageCms` before stripping keeps the rule and the
  colours. Product photos of a store; user decision, not a correctness bug against the plan.
- **L3. `Image.open` tries every Pillow plugin on untrusted bytes.** Line 30 opens without
  `formats=`, so headers of every supported format (PSD, FITS, TGA, …) are parsed before the
  format check at line 31. Passing `formats=["JPEG", "MPO", "PNG", "WEBP"]` shrinks the parser
  attack surface and makes M1's fix explicit. Hardening, no failure observed.
- **L4. S3 calls have boto3's default timeouts and retries (60 s connect/read, legacy
  retries).** `s3_media_storage.py:39` sets only the addressing style. Scenario: RustFS/S3
  unreachable → an upload or removal hangs for minutes, each attempt holding a thread of the
  default executor that Pillow and argon2 (logins) also use. Suggest `connect_timeout`,
  `read_timeout` and `retries` in the `Config`. Also the first `boto3.client(...)` is built on
  the event loop thread (`_s3()` is evaluated before `to_thread`), a one-off blocking of tens
  of ms — negligible.

**Checked and correct (no finding):** format sniffing ignores name and declared content type;
EXIF orientation applied before the 600 px check (an invalid orientation value is ignored, not
a 500 — probed); alpha kept for RGBA/LA/PA and `transparency` (P and RGB tRNS); WebP saved
without `exif`/`icc_profile`; compensation covers processor Err (nothing uploaded), a failed
second `put`, a refused or raising transaction, and a failing compensation does not mask the
Err; removal deletes keys only after commit and a failing delete stays Ok; positions are
serialized by the perfume row lock and the deferred unique constraint lets reorders and
renumbering swap positions (the `ON CONFLICT (id)` arbiter is the non-deferrable PK, so the
upsert is valid); `removed_photo_ids` cleared only after a successful save; URL building
`f"{base}/{key}"` with a validated base (no trailing `/`) and UUID-only keys;
`ensure_public_read` grants only `s3:GetObject` on `arn:aws:s3:::<bucket>/perfumes/*`, keeps
other statements, and is idempotent against the live local RustFS (read-only probe:
the stored policy equals the managed statement, so a second run returns False).

**Ruling on the tester's observation (4-id reorder body → `VALIDATION_ERROR`).** Not a
finding. Step 2 of this plan fixes `ReorderPhotosRequest.photo_ids = Field(max_length=3)`, so
the contract answering first is the planned behaviour, the status is the same 422, and the
bound keeps unbounded lists out of the use case. The "Rules fixed here" sentence is the
imprecise one; when the plan is next edited, it could read "… (missing, extra, duplicated or a
foreign id) gets a 422 `CATALOG_PHOTO_ORDER_INVALID`; a body of more than 3 ids is a 422
`VALIDATION_ERROR`". No code change.

**Ruling on the implementer's Deviations.** Step 4 (`PhotoUploadLimit` in
`application/commands/photos.py`, structlog in the application layer): accepted; import-linter
allows it and it avoided a port change. Step 5 (`type: ignore[import-untyped]` for boto3,
keyword-only `media_base_url`, in-memory default URL): accepted. Step 7 (`AddPhotoForm`):
accepted, needed by `test_schema_names_are_readable`. Touched existing tests: accepted
(additive). Step 8 (manages only its own `Sid`): accepted, safer than the plan. Step 1: the
text is wrong (see the failed checklist item); correct it to "`uv add` added `boto3`, `pillow`
and `python-multipart` to `apps/api`" or whatever actually happened.

**Result.** Findings needing code changes (H1 pending a user decision, M1, M2, M3) and a
Deviations correction: status stays `review`. Low findings are optional for the implementer
or the user to defer. *(Superseded by the re-review below.)*

### Re-review (repair round 1)

Reviewer, 2026-10-11, `git diff cf78ccb..HEAD` (`f539635` fix + `72e0e7a` tests) on
`feat/catalog-photos`, worktree clean. Scope: H1, M1-M3, L1-L4 and the deviation correction
only. Paths relative to `apps/api/src/fragancia_api/modules/catalog/`.

**Checklist (re-run for this diff): all applicable items pass.**

- [x] `plans-scope`: "26 declared, 40 changed (base main) ✔ Every change is inside the plan".
      No hot file changed.
- [x] `uv run just check` green (1253 unit passed / 3 skipped, 684 harness, lint, mypy, arch,
      plans-lint, harness-check, compose config).
- [x] `uv run just test-integration` green (329 passed, 2 skipped, the pre-existing identity
      round trips).
- [x] `openapi.json` regenerated (drift check green); `AddPhotoForm` gone from the schemas.
- [x] Error codes unchanged and stable (`CATALOG_PHOTO_TOO_LARGE` gains `max_pixels` in
      details on the pixel path only).
- [x] Deviations: item 9 recorded under "Repair round 1 implemented" (the original step 1 line
      is kept as history and corrected there). Spot-check of the new deviation "`MPO` in
      `formats=` raises `KeyError`": true, Pillow 12.3 `MpoImagePlugin.py:194-196` does not
      register an opener ("MPO shares a factory with JPEG"), `JpegImagePlugin.jpeg_factory`
      adopts MPO.

**Findings closed.**

- **H1 closed.** `http/perfume_router.py:add_photo` has no body parameter, so FastAPI 0.142.2
  builds no `body_field` and skips the pre-dependency read (`fastapi/routing.py:441-446` runs
  only `if body_field`); `require_admin`/`require_permission` and `provide(...)` are solved
  first (line 499) and the body is pulled in the handler. The only app middleware
  (`RequestContextMiddleware`, CORS) does not touch the body. Both paths are bounded: a
  declared `Content-Length` over the limit is refused before any read; a chunked body is
  refused as soon as the running total passes the limit, so memory is at most `max_bytes` plus
  one chunk. The tests use httpx's lazy ASGI body, so "0 chunks pulled" on 401/403 and "stopped
  early" on 422 are meaningful. The OpenAPI body (three `image/*`, `format: binary`, required)
  is sound; it also removes the previous, wrong `application/x-www-form-urlencoded` media type.
- **M1 closed.** `Image.open(..., formats=["JPEG", "PNG", "WEBP"])` plus `"MPO"` in the
  accepted set; regression test with a two-frame MPO.
- **M2 closed.** Pixel cap checked on the header size before `load()`. Probe: a 6324 x 6324
  1-bit PNG (15 KB, just under 40 MP) is accepted with peak RSS 457 MB (was 1.5 GB at 169 MP);
  above the cap it is `TOO_LARGE` without decoding (test patches `ImageFile.load`).
- **M3 closed.** `s3_media_storage.py:delete` collects `Errors` from every batch and raises
  `RuntimeError` with `key: code`; `_delete_quietly` (`application/commands/photos.py:32-37`)
  logs it with `exc_info`. Removal stays Ok, compensation still never masks the Err.
- **L1 closed.** `_to_8bit` scales `I;16*` over the full range; probe: 16-bit gray with `tRNS`
  also processes without error.
- **L2 closed.** `_to_srgb` after the RGB/RGBA conversion. Probes: alpha survives (RGBA + sRGB
  profile → `(201, 50, 51, 128)`, no ICC in the output); mismatched profiles (LAB profile on
  RGB/LA, sRGB profile on a CMYK JPEG) and a garbage profile fall back without an exception.
- **L3 closed** (as M1; `MPO` cannot be in `formats=`, see Deviations spot-check). GIF, BMP,
  TIFF remain `FORMAT_INVALID` (probed).
- **L4 closed.** `Config(connect_timeout=5, read_timeout=30, retries=standard/3)`.

**No regression.** The JPEG/PNG/WebP raw upload (201 and admin detail), compensation (processor
Err, failed put, refused or raising transaction, failing compensation), delete (keys after
commit, renumbering) and reorder tests all pass unchanged in behavior; the processor still
returns `FORMAT_INVALID` for non-images whatever the declared `Content-Type` (sniffing kept).

**New findings (all Low, none blocking).**

- **N1 (Low, uncertain, not reproduced).** `http/perfume_router.py:add_photo` iterates
  `request.stream()` itself. If the client disconnects mid-upload, Starlette raises
  `ClientDisconnect`, which reaches `RequestContextMiddleware` and is logged as
  `request.unhandled_error` with a 500 nobody receives (FastAPI's own body read turns this into a
  400). Scenario: an admin on a flaky mobile connection cancels an upload → a stack trace in the
  error log. Log noise only; no data effect.
- **N2 (Low).** `apps/api/pyproject.toml:16` still declares `python-multipart`, now unused (the
  implementer recorded leaving it). Removing it later avoids shipping a parser no route uses.
- **N3 (Low, plan text).** "Rules fixed here" (line 96: "whatever its name or declared content
  type") and acceptance criterion 1 ("multipart `file`") still describe the pre-repair contract.
  The differences are recorded in "Repair round 1" as the spec allowed, so this is not a
  failure, but the verifier must send the raw bytes with `Content-Type: image/jpeg` (and a
  non-image `Content-Type` is now `FORMAT_INVALID` before sniffing).

**Not verified here.** The H1 bound was checked through FastAPI's source and the in-process ASGI
tests, not against a running uvicorn (that is for the verifier); a real phone MPO or Apple P3
file was not used.

**Result.** H1, M1-M3, L1-L4 and the deviation correction are closed; nothing regressed;
checklist green. Status → `verify`.

## Verification

Verified on 2026-10-11 by the main session, inline, on `feat/catalog-photos` at 5768139,
including repair round 1. The upload is a raw body (README decision 54), so every upload below
sends the bytes with a `Content-Type: image/*` header, not multipart.

**Suites (run once)**
- `uv run just check`: ruff "All checks passed!"; mypy "no issues found in 221 source files";
  unit `1253 passed, 3 skipped`; harness `684 passed`.
- `uv run just test-integration`: `329 passed, 2 skipped`.
- `uv run just db-migrate` on the development database: clean, at 0009.
- The tester ran the migration round trip on the test database.
- `uv run just bootstrap` run twice. The second run printed "= bucket fragancia-media already
  exists" and "= public read on fragancia-media/perfumes/ already exists", so the step is
  idempotent.

**Setup.** `uv run just api` on port 8100, started and stopped by me, with local RustFS.
- Signed in as `verifier@example.test`.
- Created brand "Verif Fotos" and perfume "Foto Uno" (EDT, Floral).
- Generated the inputs with a scratchpad Pillow script:
  - a 2000×1500 JPEG carrying an EXIF Artist tag;
  - a 1000×800 PNG with transparency;
  - a 500×800 JPEG, a GIF and a text file named `.jpg`;
  - a 12 MB random blob;
  - a 900×900 JPEG and a 900×900 WebP.

**Uploads and renditions**
- The JPEG → 201. The admin detail shows position 1, 1200×900.
- The PNG → 201, position 2, 1000×800.
- Each file was fetched **anonymously** from its public URL. All 200 `image/webp`, `public,
  max-age=31536000, immutable`, no EXIF and no ICC profile:
  - JPEG card 400×300 RGB, detail 1200×900 RGB;
  - PNG card 400×320 RGBA, detail 1000×800 RGBA, transparency kept.

**Rejections**
- The 500×800 JPEG → 422 `CATALOG_PHOTO_TOO_SMALL`.
- The GIF as `image/gif`, the GIF as `image/jpeg`, and the text file → 422
  `CATALOG_PHOTO_FORMAT_INVALID`.
- The 12 MB blob → 422 `CATALOG_PHOTO_TOO_LARGE`, both with `Content-Length` and chunked.
- The same 12 MB blob **without a session** → 401 `AUTHENTICATION_REQUIRED` (H1).

**Limit and compensation**
- The 3rd photo (WebP) → 201.
- A 4th → 409 `CATALOG_PHOTO_LIMIT_REACHED`.
- Listing the bucket under `perfumes/<perfume>/` gave exactly 6 objects, two per stored photo:
  the refused photo left nothing.

**Reorder**
- The three ids reversed → 204, and the positions followed (`1:…fa97e5, 2:…9359c8,
  3:…df21d3`).
- A list missing an id, or one with a foreign id → 422 `CATALOG_PHOTO_ORDER_INVALID`.

**Public views**
- After adding a presentation and publishing, the storefront list showed `photo_url` = the
  card of the new position 1.
- The public detail listed the 3 card URLs in position order.
- A second published perfume without photos showed `photo_url: null` and `photos: []`.

**Removal**
- `DELETE` of position 1 → 204, and the rest were renumbered `1, 2`.
- Both of its objects return 404 anonymously, and the bucket held 4 objects.
- An unknown photo → 404 `CATALOG_PHOTO_NOT_FOUND`.

**Archived perfume**
- Upload, delete and reorder → 422 `CATALOG_PERFUME_ARCHIVED`.
- The bucket still held 4 objects, so the refused upload was compensated.

**Without a session**
- `DELETE` and `PUT …/order` → 401.
- The server log has no 500 and no traceback.

**Cleanup**
- The two remaining photos were removed through the API. The bucket prefix then held 0
  objects.
- I counted first, then deleted 2 presentations, 2 perfumes and 1 brand, and stopped the API.

**NOT VERIFIED**
- A real phone MPO and a real Apple Display P3 photo. The tests use Pillow-made MPO and a
  hand-built P3 profile.
- S3 timeouts and retries under a real outage. They are asserted as configuration only.
- The production bucket policy or CDN. It is a rollout step.
- The reviewer's N1: on a client disconnect mid-upload, a 500 is logged that nobody receives.

Every acceptance criterion passes. The plan is ready for the user to set `done`.

**Follow-up after verification** (2026-10-11, approved by the user together with `done`):
- **N2:** `python-multipart` was removed from `apps/api/pyproject.toml` and `uv.lock` with
  `uv remove --package fragancia-api python-multipart`, because no route uses it.
  - `uv run just check` stayed green: `1253 passed, 3 skipped`, harness `684 passed`.
  - Live smoke test without the package: a JPEG upload → 201, its removal → 204, and the bucket
    prefix ended with 0 objects. The smoke rows were then deleted (1 perfume, 1 brand).
- **N1:** kept as is, by the user's decision (README decision 56). A client disconnect during
  an upload stays visible in the log.
