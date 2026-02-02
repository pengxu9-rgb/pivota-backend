# Selfie Upload + QC API

This service supports “upload selfie → run basic QC → return retry suggestions”.

## Endpoints

All endpoints require agent auth (`X-API-Key` or `X-Checkout-Token`) via existing `get_agent_context`.

### `POST /photos/presign`

Request body:

- `content_type` (required) e.g. `image/jpeg`
- `byte_size` (optional) client-reported size
- `file_name` (optional)
- `consent` (required) must be `true` (explicit user consent)
- `user_id` (optional) used for ownership/deletion

Response:

- `upload_id`
- `upload.method` + `upload.url` + `upload.headers` (presigned `PUT` upload)
- `expires_at` (TTL for keeping the upload record)
- `max_bytes`
- `tips.daylight` / `tips.indoor_white` (displayable shooting guidance)

### `POST /photos/confirm`

Request body:

- `upload_id` (required)
- `byte_size` (optional)

Response includes `qc.state=pending` and `next_poll_ms`.

### `GET /photos/qc?upload_id=...`

Response:

- `qc_status`: `passed | too_dark | has_filter | blurry` (or `null` while pending)
- `qc.advice.summary` + `qc.advice.suggestions[]`
- `qc.advice.tips.daylight` / `qc.advice.tips.indoor_white`
- `next_poll_ms` while pending

### `DELETE /photos?upload_id=...`

Best-effort delete (marks record deleted and attempts storage delete).

### `POST /photos/cleanup`

Admin-only cleanup for expired uploads (soft-deletes DB rows + best-effort storage delete).

- Requires `X-ADMIN-KEY` (matches `ADMIN_API_KEY` or `PROMOTIONS_ADMIN_KEY`)
- Query params:
  - `limit` (default: `PHOTO_CLEANUP_BATCH_SIZE`, max: `1000`)
  - `dry_run` (default: `false`) — return candidates only, do not delete

## Environment variables

- `PHOTO_UPLOAD_BUCKET` (required)
- Fallbacks: `S3_BUCKET`, `AWS_S3_BUCKET`
- `PHOTO_UPLOAD_PREFIX` (default: `selfies`)
- `PHOTO_UPLOAD_REGION` (default: `auto`)
- `PHOTO_UPLOAD_ENDPOINT_URL` (optional; S3-compatible endpoints like R2)
- Fallbacks: `AWS_ENDPOINT_URL`, `S3_ENDPOINT_URL`, and region fallbacks `AWS_REGION` / `AWS_DEFAULT_REGION`
- Optional dedicated credentials (recommended for R2 to avoid clobbering global AWS creds):
  - `PHOTO_UPLOAD_ACCESS_KEY_ID`
  - `PHOTO_UPLOAD_SECRET_ACCESS_KEY`
  - `PHOTO_UPLOAD_SESSION_TOKEN` (optional)
- `PHOTO_PRESIGN_TTL_SECONDS` (default: `900`)
- `PHOTO_UPLOAD_TTL_HOURS` (default: `24`)
- `PHOTO_UPLOAD_MAX_BYTES` (default: `10485760`)
- `PHOTO_CLEANUP_LOOP_ENABLED` (default: `false`) — run an in-process cleanup loop
- `PHOTO_CLEANUP_INTERVAL_SECONDS` (default: `900`)
- `PHOTO_CLEANUP_BATCH_SIZE` (default: `200`)
- `PHOTO_CLEANUP_STARTUP_DELAY_SECONDS` (default: `30`)

## Notes

- QC uses Pillow (`Pillow`) to decode images and run simple heuristics (brightness / blur / color cast).
- Users must consent before presigning; otherwise `USER_CONSENT_REQUIRED` is returned.
- Cloudflare R2 note: region is forced to `auto` and session-token signing is disabled.
