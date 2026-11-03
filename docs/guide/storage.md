# Object storage

!!! warning "Experimental"
    New in 0.7: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../features/scope.md) lists the tools that are settled.

Files in S3, or anything that speaks its API (MinIO, Cloudflare R2, Backblaze B2, Google Cloud
Storage's interoperability...), without passing through the app: `S3Presigner` signs URLs and
upload forms with AWS's Signature Version 4, and the browser uses them directly. Nothing to
install, and no SDK.

Every class and function: [the API reference](../reference/uploads.md#fastapi_alcazaba.uploads.S3Presigner).

```python
from fastapi_alcazaba.uploads import S3Presigner

s3 = S3Presigner(
    bucket="uploads",
    region="eu-west-1",
    access_key=settings.s3_access_key,
    secret_key=settings.s3_secret_key,  # a SecretStr
)
```

The bucket stays private: each URL is for one object, one method, and a few minutes (`expires`,
300 seconds by default, 7 days at most). Give the access key a user that may only do what the
URLs need on this bucket.

## Uploads

```python
@app.post("/avatar/upload")
async def avatar_upload(user: CurrentUser) -> UploadForm:
    return s3.upload_form(
        f"avatars/{user.id}/{uuid4()}",
        max_size=2_000_000,
        content_type_prefix="image/",
    )
```

The browser posts the file to `url`, with `fields` first and the file last, as `file`:

```javascript
const body = new FormData();
for (const [name, value] of Object.entries(form.fields)) body.append(name, value);
body.append("Content-Type", file.type);
body.append("file", file);
await fetch(form.url, { method: "POST", body });
```

- **Bounded by the service:** a file smaller than `min_size` (1 byte) or larger than `max_size`
  is refused by S3, as is one that declares another type than `content_type`, or one not
  starting with `content_type_prefix`.
- **A key of its own for each upload:** a form overwrites what's under its key.

For a client that PUTs (an app, a script), `upload_url` signs a PUT of exactly the size the
client declared, and of its SHA-256 when it gives one:

```python
@app.post("/scans")
async def new_scan(body: NewScan, user: CurrentUser) -> PresignedUpload:
    if body.size > 50_000_000:
        raise HTTPException(413, "File too large")
    return s3.upload_url(f"scans/{user.id}/{uuid4()}", size=body.size, sha256=body.sha256)
```

The client sends the returned `headers` as they are: `Content-Length` and `x-amz-checksum-sha256`
are signed, so S3 (MinIO, R2) refuses any other length (403), and bytes whose checksum isn't the
one given (400).

## Checking what was uploaded

Neither the form nor the URL say what the file is. `check_uploaded` reads it back from the
bucket, runs [`FastAPIUploads`](uploads.md)'s checks on it, its malware scanner too, and deletes it
when it fails them (`pip install "fastapi-alcazaba[uploads]"`):

```python
from fastapi_alcazaba.uploads.direct import check_uploaded


@app.post("/scans/{key:path}/done")
async def scan_done(key: str) -> dict[str, str]:
    checked = await check_uploaded(uploads, s3, key)  # 413, 415, 422: and deleted
    await checked.file.close()
    return {"type": checked.content_type}
```

- At most `max_size` + 1 bytes are read: a larger object is "too large" without reading the rest.
- Refused (413, 415, 422): deleted from the bucket with a signed DELETE, and the HTTPException
  goes on. Not scanned (503, the scanner down): kept, to check later. No such object: 404; the
  service failing: 502.
- The returned `CheckedFile` holds the bytes read (in memory up to 1 MiB, then on disk): close it
  when done.
- Not yet: multipart (resumable) uploads.

## Downloads

```python
@app.get("/invoices/{invoice_id}")
async def invoice(invoice_id: int, user: CurrentUser) -> RedirectResponse:
    invoice = await db.invoices.get(invoice_id, owner=user.id)  # yours to authorize
    url = s3.download_url(f"invoices/{invoice.id}.pdf", filename="invoice.pdf", expires=60)
    return RedirectResponse(url, status_code=303)
```

With `filename`, browsers download the object as that file (`Content-Disposition: attachment`)
rather than show it, so an uploaded HTML or SVG never runs on the bucket's origin.
`content_type` overrides the type it's served with.

## Keys and services

- **Object keys** are 1 to 1024 bytes, without a leading `/`, an empty, `.` or `..` segment, or
  control characters: a key made from user input can't escape its prefix.
- **Other services:** `endpoint` is their URL (`https://<account>.r2.cloudflarestorage.com`,
  `http://localhost:9000` for MinIO), and the bucket goes in the path: what most of them want.
  With AWS, the bucket is in the host (`path_style=False`). Plain `http` is only for
  `localhost`.
- **Temporary credentials** (STS, an instance's role): `session_token` is signed into each URL.
  The URLs stop working when the credentials expire, whatever their `expires`.
