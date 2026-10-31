# PaginationKeyset

--8<-- "docs/examples/animations/pagination_keyset.svg"

A chat's messages by keyset, the key in plain sight: clients ask for what came after the last
message they have, or page back through the history, newest first. The strategy:
[PaginationKeyset](../../guide/pagination/keyset.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.keyset:app
```

## Try it

The first three messages, and the key of the last one in `after`:

```console
$ curl -i "http://localhost:8000/messages?limit=3"
HTTP/1.1 200 OK
content-type: application/json
link: </messages?limit=3>; rel="first", </messages?after=3&limit=3>; rel="next"
etag: W/"27bea474cecbda7b21974e6e090dff20"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"author":"leo","text":"Message 1","id":1},{"author":"eva","text":"Message 2","id":2},{"author":"ana","text":"Message 3","id":3}],"limit":3,"after":"3","has_more":true}

$ curl "http://localhost:8000/messages?after=3&limit=3"
{"items":[{"author":"leo","text":"Message 4","id":4},{"author":"eva","text":"Message 5","id":5},{"author":"ana","text":"Message 6","id":6}],"limit":3,"after":"6","has_more":true}
```

The last page says there's no more, and still where it ended:

```console
$ curl -i "http://localhost:8000/messages?after=58"
HTTP/1.1 200 OK
content-type: application/json
link: </messages?limit=20>; rel="first"
etag: W/"b2c145cc80d08760326a4e2e28fade67"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"author":"eva","text":"Message 59","id":59},{"author":"ana","text":"Message 60","id":60}],"limit":20,"after":"60","has_more":false}
```

A client that syncs keeps that `after`, and asks with it later for what came since:

```console
$ curl -X POST http://localhost:8000/messages -H "Content-Type: application/json" -d '{"author": "ana", "text": "¿Comemos?"}'
{"author":"ana","text":"¿Comemos?","id":61}

$ curl -i "http://localhost:8000/messages?after=60"
HTTP/1.1 200 OK
content-type: application/json
link: </messages?limit=20>; rel="first"
etag: W/"9ac579890d7cc4d21627e6ab93c9e11d"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"author":"ana","text":"¿Comemos?","id":61}],"limit":20,"after":"61","has_more":false}
```

The history read backwards, newest first:

```console
$ curl -i "http://localhost:8000/messages/newest?limit=3"
HTTP/1.1 200 OK
content-type: application/json
link: </messages/newest?limit=3>; rel="first", </messages/newest?after=59&limit=3>; rel="next"
etag: W/"1403824bdb6e14ea4cf53f720ab18589"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"author":"ana","text":"¿Comemos?","id":61},{"author":"ana","text":"Message 60","id":60},{"author":"eva","text":"Message 59","id":59}],"limit":3,"after":"59","has_more":true}
```

A key that isn't an id is refused before anything is queried:

```console
$ curl -i "http://localhost:8000/messages?after=abc"
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"Invalid after"}
```

## What to look at

- **The key, as it is:** `after` is the id of the last message, as text; a key of several
  values, such as `(created_at, id)`, would be a JSON array.
- **One row more:** each query fetches `params.fetch` rows (the limit and one), which tells
  whether there's a next page.
- **Syncing:** the last page still gives its `after`, so a client that has caught up asks for
  whatever came since with it.
- **Both orders:** `/messages/newest` reads the index the other way, and `after` there means the
  messages before it.
- **Cached page by page:** each key and limit is an entry of its own, and a new message drops
  them all (tag `messages`).

## The code

<!-- fmt: off -->
```python title="examples/pagination/keyset.py"
--8<-- "examples/pagination/keyset.py"
```
<!-- fmt: on -->
