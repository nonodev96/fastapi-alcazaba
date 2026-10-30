# PaginationIdBased

--8<-- "docs/examples/animations/pagination_id_based.svg"

A chat's messages, paginated by id: clients ask for what came after the last message they have,
and get only the new ones however many arrive meanwhile, or page back through the history,
newest first. The strategy: [PaginationIdBased](../../guide/pagination/id-based.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.id_based:app
```

## Try it

The first three messages; `last_id` is the id of the last one, where the next page starts:

```console
$ curl -i "http://localhost:8000/messages?limit=3"
HTTP/1.1 200 OK
content-type: application/json
link: </messages?limit=3>; rel="first", </messages?last_id=3&limit=3>; rel="next"
etag: W/"88867ffe07c3547a6f1109a3f3cd3b39"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"author":"leo","text":"Message 1","id":1},{"author":"eva","text":"Message 2","id":2},{"author":"ana","text":"Message 3","id":3}],"limit":3,"last_id":3,"has_more":true}

$ curl "http://localhost:8000/messages?last_id=3&limit=3"
{"items":[{"author":"leo","text":"Message 4","id":4},{"author":"eva","text":"Message 5","id":5},{"author":"ana","text":"Message 6","id":6}],"limit":3,"last_id":6,"has_more":true}
```

A client that has every message up to 60 asks for what came after it. Nothing yet:

```console
$ curl "http://localhost:8000/messages?last_id=60"
{"items":[],"limit":20,"last_id":null,"has_more":false}
```

Somebody writes, which drops the cached pages, and the next time the client asks it gets the
new message alone:

```console
$ curl -X POST http://localhost:8000/messages -H "Content-Type: application/json" -d '{"author": "ana", "text": "Anyone for lunch?"}'
{"author":"ana","text":"Anyone for lunch?","id":61}

$ curl -i "http://localhost:8000/messages?last_id=60"
HTTP/1.1 200 OK
content-type: application/json
link: </messages?limit=20>; rel="first"
etag: W/"8ceadc3c519c090a72dabac286daedee"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"author":"ana","text":"Anyone for lunch?","id":61}],"limit":20,"last_id":61,"has_more":false}
```

The history, newest first: there `last_id` is the oldest message of the page, and the next page
holds the messages before it:

```console
$ curl -i "http://localhost:8000/messages/newest?limit=3"
HTTP/1.1 200 OK
content-type: application/json
link: </messages/newest?limit=3>; rel="first", </messages/newest?last_id=59&limit=3>; rel="next"
etag: W/"a4dd6c0e05c8e0b009f6adce7b744fb9"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"author":"ana","text":"Anyone for lunch?","id":61},{"author":"ana","text":"Message 60","id":60},{"author":"eva","text":"Message 59","id":59}],"limit":3,"last_id":59,"has_more":true}
```

An id that isn't an integer is a 422, before anything is queried:

```console
$ curl -i "http://localhost:8000/messages?last_id=abc"
HTTP/1.1 422 Unprocessable Content
content-type: application/json

{"detail":[{"type":"int_parsing","loc":["query","last_id"],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"abc"}]}
```

## What to look at

- **The id as it is:** `last_id=60` is the id of a message, neither encoded nor signed: ids are
  public, and a client can keep the last one it has and ask from there. To sort by other columns,
  or keep the sort key from clients, use [PaginationCursor](cursor.md).
- **Nothing shifts:** each page is `WHERE id > :last_id ORDER BY id LIMIT :fetch`, so messages
  written or deleted meanwhile don't move the pages, and the primary key finds any page as fast
  as the first.
- **`has_more`, from one row more:** the database is asked for `limit + 1` rows; the extra one
  says whether there's a next page. A page that comes back empty has no `last_id`: the client
  keeps the one it had.
- **Both directions:** `/messages` goes up from `last_id` (`paginate(...)`), `/messages/newest`
  down from it (`descending=True`, with `WHERE id < :last_id ORDER BY id DESC`).
- **Cached per page, dropped on writes:** every page is tagged `messages`, and a new message
  drops them all, so a client that has caught up sees it at once.

## The code

<!-- fmt: off -->
```python title="examples/pagination/id_based.py"
--8<-- "examples/pagination/id_based.py"
```
<!-- fmt: on -->
