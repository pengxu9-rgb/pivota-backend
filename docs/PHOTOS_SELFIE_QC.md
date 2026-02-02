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
- `upload.url` + `upload.fields` (S3 POST form)
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

## Environment variables

- `PHOTO_UPLOAD_BUCKET` (required)
- `PHOTO_UPLOAD_PREFIX` (default: `selfies`)
- `PHOTO_UPLOAD_REGION` (default: `auto`)
- `PHOTO_UPLOAD_ENDPOINT_URL` (optional; S3-compatible endpoints like R2)
- `PHOTO_PRESIGN_TTL_SECONDS` (default: `900`)
- `PHOTO_UPLOAD_TTL_HOURS` (default: `24`)
- `PHOTO_UPLOAD_MAX_BYTES` (default: `10485760`)

## Notes

- QC uses Pillow (`Pillow`) to decode images and run simple heuristics (brightness / blur / color cast).
- Users must consent before presigning; otherwise `USER_CONSENT_REQUIRED` is returned.
