# Feature Specification: Scanned uploads and bounded direct uploads

**Feature Branch**: `feature/upload-scanning-and-bounds` · **Created**: 2026-11-03 · **Status**: Done

**Input**: "Files users upload, directly or through presigned URLs, should be checked as strictly
as files that go through the app."

## Clarifications (no question asked; no backward compatibility kept)

- A `Scanner` protocol (`async scan(file) -> ScanResult`) and `ClamAVScanner`, which speaks
  clamd's INSTREAM and PING over TCP or a Unix socket with asyncio only: the `clamav` extra is
  empty (as the maintainer asked for `fastapi-alcazaba[clamav]`; no unmaintained client library).
- The scan runs after type, size and pixels; infected → 422 "File rejected" (the threat logged,
  never answered), `on_infected` before; unavailable or past `scan_timeout` → 503 unless
  `scan_fail_open`. Outcomes `infected`, `scan_failed`, `unscanned`.
- `upload_url(key, size=, sha256=, content_type=)` signs those headers (SigV4 signed headers) and
  returns them; `delete_url`.
- `check_uploaded(uploads, presigner, key)` (the `uploads` extra, httpx2): reads at most
  max_size + 1 bytes back, checks, deletes on 413/415/422, keeps on 503.
- Live tests: MinIO in CI and `make services-up`; clamd in `make services-up` only (a minute to
  load, ~1 GB).
- Out of scope: multipart/resumable presigning.

## Acceptance

Fake scanner, fake clamd, fake bucket; MinIO (size 403, checksum 400) and clamd (EICAR) live;
uploads.md and storage.md.
