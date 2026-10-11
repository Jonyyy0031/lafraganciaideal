---
status: testing
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

- [ ] Uploading a 2000×1500 JPEG (`POST /api/v1/admin/perfumes/{id}/photos`, multipart
      `file`) → 201 `{id}`:
      - `GET /admin/perfumes/{id}` lists the photo at position 1 with width 1200 and height
        900.
      - Both URLs return 200 `image/webp` without credentials.
      - The card rendition's longest side is 400 px.
      - Neither file has EXIF.
- [ ] A PNG with transparency is accepted and stays transparent. A GIF, a text file renamed
      `.jpg` and a 500×800 image → 422 with `CATALOG_PHOTO_FORMAT_INVALID`,
      `CATALOG_PHOTO_FORMAT_INVALID` and `CATALOG_PHOTO_TOO_SMALL`. A file over 10 MB → 422
      `CATALOG_PHOTO_TOO_LARGE`.
- [ ] A 4th photo → 409 `CATALOG_PHOTO_LIMIT_REACHED`, and **no new object is left in the
      bucket** (compensation).
- [ ] `PUT …/photos/order` with the three ids reversed → 204, and the positions follow. A list
      missing an id, or with a foreign id → 422 `CATALOG_PHOTO_ORDER_INVALID`.
- [ ] `DELETE …/photos/{id}` → 204: the remaining photos are renumbered 1..n and both objects
      are gone from the bucket. An unknown photo → 404 `CATALOG_PHOTO_NOT_FOUND`.
- [ ] While the perfume is archived, upload, delete and reorder → 422 `CATALOG_PERFUME_ARCHIVED`.
- [ ] The storefront list shows `photo_url` = position 1's card URL, or `null` without photos.
      The public detail lists `photos` in order.
- [ ] Every new route returns 401 without a session.
- [ ] Migration 0009 applies. `openapi.json` is regenerated and the drift check passes.
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

## Test coverage

## Review findings

## Verification
