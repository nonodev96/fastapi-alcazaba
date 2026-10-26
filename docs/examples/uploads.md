# Checking uploads

An app where users upload an avatar and documents, then download them again. Every file is
judged by its bytes, not by its name or the `Content-Type` the client sent:
[`FastAPIUploads`](../index.md#checking-uploads) checks avatars (PNG, JPEG, GIF or WebP, at most
2 MiB and 16 million pixels) as a dependency, and documents (PDF, Word, Excel and PowerPoint,
up to five at a time) with `check()`. Files are stored under names of the app's own and served
back with the type found in them.

[`BodySizeLimitMiddleware`](../index.md#security-headers-and-body-size) gives each route a body
limit, and [`SecurityHeadersMiddleware`](../index.md#security-headers-and-body-size) adds
`X-Content-Type-Options: nosniff` to every response, so browsers don't second-guess the type.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.uploads.main:app
```

Files go to a temporary directory, deleted at shutdown; set `UPLOAD_DIR` to keep them.

## Try it

An image is stored, with the type and dimensions read from its bytes:

```console
$ curl -i http://127.0.0.1:8000/avatars -F file=@cat.png
HTTP/1.1 201 Created
content-type: application/json
x-content-type-options: nosniff

{"id":"6b88b761-8ff4-4477-bc9e-568420bd4205","name":"cat.png","content_type":"image/png","size":179,"width":64,"height":64}
```

An HTML page sent as `cat.png`, with `Content-Type: image/png`, is refused. Served back as it
was declared, it would run its script in the browser:

```console
$ printf '<!doctype html><script>alert(document.cookie)</script>' > cat.html
$ curl -i http://127.0.0.1:8000/avatars -F 'file=@cat.html;filename=cat.png;type=image/png'
HTTP/1.1 415 Unsupported Media Type

{"detail":"Unsupported file type"}
```

A PNG of 33 bytes whose header says 50,000 × 50,000 pixels would take gigabytes of memory to
decode. Its dimensions are read from the header, without decoding it:

```console
$ python -c "import struct, sys, zlib; h = b'IHDR' + struct.pack('>IIBBBBB', 50000, 50000, 8, 2, 0, 0, 0); sys.stdout.buffer.write(b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + h + struct.pack('>I', zlib.crc32(h)))" > bomb.png
$ curl -i http://127.0.0.1:8000/avatars -F file=@bomb.png
HTTP/1.1 413 Content Too Large

{"detail":"Image too large"}
```

A body over the route's limit is refused from its `Content-Length`, before the multipart parser
stores it:

```console
$ head -c 3145728 /dev/urandom > big.jpg
$ curl -i http://127.0.0.1:8000/avatars -F file=@big.jpg
HTTP/1.1 413 Content Too Large
x-content-type-options: nosniff

{"detail":"Request body too large"}
```

Several documents at once. The second is a Word document named `minutes.pdf`: it's stored as
what it is.

```console
$ curl -i http://127.0.0.1:8000/documents -F files=@invoice.pdf -F 'files=@minutes.docx;filename=minutes.pdf'
HTTP/1.1 201 Created

[{"id":"1f8b5cef-0d26-48e2-b6d0-03cdd086c6f0","name":"invoice.pdf","content_type":"application/pdf","size":142,"width":null,"height":null},{"id":"5a667cf4-565c-4e7a-88ef-ae6a014c1761","name":"minutes.pdf","content_type":"application/vnd.openxmlformats-officedocument.wordprocessingml.document","size":540,"width":null,"height":null}]
```

And it's served as a Word document, as an attachment, with the extension that says so:

```console
$ curl -D - -o minutes.docx http://127.0.0.1:8000/files/5a667cf4-565c-4e7a-88ef-ae6a014c1761
HTTP/1.1 200 OK
content-type: application/vnd.openxmlformats-officedocument.wordprocessingml.document
content-disposition: attachment; filename="minutes.docx"
content-length: 540
x-content-type-options: nosniff
content-security-policy: default-src 'none'; frame-ancestors 'none'
```

## What to look at

- **The type comes from the bytes:** `CheckedFile.content_type` is what the file is, whatever its
  name or the client's `Content-Type`. It's stored with the file and used to serve it, together
  with `nosniff`, so a browser can't be talked into reading an upload as HTML.
- **Names of the app's own:** each file is stored as `{uuid}{CheckedFile.extension}`. The
  client's name (`CheckedFile.filename`, already made safe) is kept for display, and the
  download name goes through `safe_filename` again with the real extension: no path, no
  control or invisible characters (such as the right-to-left override), `_` before the names
  Windows reserves.
- **Two size limits, on purpose:** the multipart parser stores the whole request before an
  endpoint runs, so `BodySizeLimitMiddleware` bounds it per route (a little more than the files
  allowed, for the multipart framing; 64 KiB anywhere else). `FastAPIUploads` then counts each
  file exactly, and checks the pixels of images.
- **All or nothing:** `/documents` checks every file before it stores any, so one bad file in a
  request leaves nothing half-saved.
- **Attachments for documents:** a PDF can carry scripts, so documents are downloaded rather
  than opened by the browser; images are shown inline.
- **`SecurityHeadersMiddleware` last:** added last, it's the outermost middleware, so the 413s of
  `BodySizeLimitMiddleware` get the security headers too.

## The code

<!-- fmt: off -->
```python title="examples/uploads/main.py"
--8<-- "examples/uploads/main.py"
```
<!-- fmt: on -->
