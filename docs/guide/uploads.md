# Checking uploads

In an app: [the example](../examples/uploads.md). Every class and function: [the API reference](../reference/uploads.md).

The name and the `Content-Type` of an upload are whatever the client chose: an HTML page sent as
`cat.png` would be served back as HTML. `FastAPIUploads` checks the file itself, with nothing but
the standard library:

```python
from fastapi_alcazaba.uploads import IMAGES, CheckedFile, FastAPIUploads

uploads = FastAPIUploads(types=IMAGES, max_size=5 * 1024 * 1024, max_pixels=25_000_000)


@app.post("/avatars")
async def avatar(upload: Annotated[CheckedFile, Depends(uploads)]) -> dict[str, str]:
    name = f"{uuid4()}{upload.extension}"  # never the client's name
    await storage.save(name, upload.file.file, content_type=upload.content_type)
    return {"name": name}
```

As a dependency it reads the form field `file`; for other fields or several files, call
`await uploads.check(file)`. It returns a `CheckedFile`: the `UploadFile` read back to its start,
the `content_type` found in its bytes, its `size`, its `width` and `height` (images) and a safe
`filename`.

- **The real type** (`sniff(data)`): PNG, JPEG, GIF, WebP, PDF, ZIP, GZIP, MP4 and QuickTime from
  their signatures; Word, Excel and PowerPoint documents from the content types their
  `[Content_Types].xml` declares (as Office reads them), with the macro-enabled ones (`.docm`,
  `.xlsm`, `.pptm`) told apart, and anything ambiguous reported as a plain ZIP; UTF-8 text without
  control characters (`text/plain`: CSV is text, it can't be told apart). Text that starts with a
  tag is HTML, SVG or XML, never plain text, and SVG is never an image: all three can carry
  scripts. `types` must come from `KNOWN_TYPES` (`IMAGES`, `OFFICE_DOCUMENTS`...), so a typo like
  `image/jpg` fails at start-up. Store and serve files with the detected type, never the
  client's, plus `X-Content-Type-Options: nosniff` (`SecurityHeadersMiddleware`).
- **Size:** counted while the file is read in chunks, whatever `UploadFile.size` says; 413 beyond
  `max_size`, 415 for a type that isn't allowed, 422 for an empty file. By the time an endpoint
  runs the multipart parser has stored the whole request already, so limit it with
  `BodySizeLimitMiddleware` on the route as well.
- **Decompression bombs:** the width and height of PNG, JPEG (however much metadata comes before
  the frame), GIF and WebP images are read from their headers (`image_size(data)`) without
  decoding them; more than `max_pixels` (40 million by default) gets a 413, and an image whose
  dimensions can't be read a 415.
- **Safe names** (`safe_filename(name)`): NFKC-normalised, only the last path component, without
  control or invisible characters (such as the right-to-left override that shows `exe.txt` as
  `txt.exe`), `<>:"|?*` replaced, without leading dots or dashes, `_` in front of the names Windows
  reserves (`CON`, `NUL`, `COM1`...), and at most 255 bytes, keeping the extension. For display and
  `Content-Disposition`; store files under names of your own (`CheckedFile.extension`).
- **What it doesn't do by itself:** look inside the files. A PDF can carry scripts, Office
  documents macros, archives may expand to far more than their size, and the frames of a GIF
  aren't counted. Allow HTML, SVG or XML only to serve them as attachments from another domain.
  A malware scanner looks inside them: see below.

## Scanning for malware

```python
from fastapi_alcazaba.uploads import ClamAVScanner, FastAPIUploads

scanner = ClamAVScanner("tcp://clamav:3310")  # or "unix:///run/clamav/clamd.ctl"
uploads = FastAPIUploads(
    types=OFFICE_DOCUMENTS,
    scanner=scanner,
    on_infected=quarantine,  # optional: (file, result) -> None, before the 422
)
app.include_router(health_router({"clamav": HealthCheck(scanner.ping, critical=False)}))
```

`pip install "fastapi-alcazaba[clamav]"`: nothing more is installed, `ClamAVScanner` speaks
clamd's protocol itself.

- **After the other checks:** only a file of an accepted type, size and pixels is scanned: the
  scanner never gets a 2 GB upload or an unknown type.
- **ClamAV:** the file goes to clamd (ClamAV's daemon) with its `INSTREAM` command, in chunks
  over TCP or a Unix socket, never through a shared disk; `ping()` is a health check. Run clamd
  next to the app (the `clamav/clamav` image), with `freshclam` keeping its signatures up to
  date; give it about 1 GB of memory, and raise its `StreamMaxLength` (25 MB by default) above
  `max_size`, or larger files can't be scanned.
- **Infected:** a 422 `File rejected`, which doesn't say what was found; the threat's name is
  logged, and counted (`fastapi_alcazaba.uploads`, outcome `infected`). `on_infected(file,
  result)`, sync or async, runs before: to keep the file in quarantine, or alert someone.
- **Fails closed:** when the scanner doesn't answer within `scan_timeout` (30 s) or can't scan
  the file, the upload gets a 503 (`scan_failed`). `scan_fail_open=True` accepts it unscanned
  instead (`unscanned`, logged).
- **Your own scanner:** anything with `async scan(file) -> ScanResult` (a commercial engine, a
  sandbox service); the file comes at its start.
- **What it can't do:** a scanner knows the malware it has signatures for. A script in a PDF or
  a macro written for you goes through: keep the other precautions (attachments, another
  domain).

Files uploaded straight to S3 get the same checks with
[`check_uploaded`](storage.md#checking-what-was-uploaded).
