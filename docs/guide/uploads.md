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
- **What it doesn't do:** look inside the files. A PDF can carry scripts, Office documents
  macros, archives may expand to far more than their size, and the frames of a GIF aren't counted.
  Allow HTML, SVG or XML only to serve them as attachments from another domain.
