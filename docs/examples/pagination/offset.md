# PaginationOffset

--8<-- "docs/examples/animations/pagination_offset.svg"

An access log of a thousand lines, read newest first by offset and limit, without ever being
counted: each page says whether there's another after it. The strategy:
[PaginationOffset](../../guide/pagination/offset.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.offset:app
```

## Try it

The three newest lines, and whether there are more:

```console
$ curl -i "http://localhost:8000/logs?limit=3"
HTTP/1.1 200 OK
content-type: application/json
link: </logs?offset=0&limit=3>; rel="first", </logs?offset=3&limit=3>; rel="next"
etag: W/"058a76d9e5793e791c88050cae14514f"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=10

{"items":[{"id":1000,"level":"error","message":"GET /products/30"},{"id":999,"level":"info","message":"GET /products/29"},{"id":998,"level":"info","message":"GET /products/28"}],"offset":0,"limit":3,"has_more":true}
```

The `next` link is the following page:

```console
$ curl -i "http://localhost:8000/logs?offset=3&limit=3"
HTTP/1.1 200 OK
content-type: application/json
link: </logs?offset=0&limit=3>; rel="first", </logs?offset=0&limit=3>; rel="prev", </logs?offset=6&limit=3>; rel="next"
etag: W/"b23265fd81d0eeed72304c4dcdbfec83"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=10

{"items":[{"id":997,"level":"info","message":"GET /products/27"},{"id":996,"level":"info","message":"GET /products/26"},{"id":995,"level":"info","message":"GET /products/25"}],"offset":3,"limit":3,"has_more":true}
```

The errors alone: twenty of them, so from the nineteenth on there are only two, `has_more` is
false and the links have no `next`. Asked again within ten seconds, the page comes from the cache:

```console
$ curl -i "http://localhost:8000/logs?level=error&offset=18"
HTTP/1.1 200 OK
content-type: application/json
link: </logs?level=error&offset=0&limit=20>; rel="first", </logs?level=error&offset=0&limit=20>; rel="prev"
etag: W/"47586190d3552a24d4378013041c3f12"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=10

{"items":[{"id":100,"level":"error","message":"GET /products/3"},{"id":50,"level":"error","message":"GET /products/50"}],"offset":18,"limit":20,"has_more":false}

$ curl -si "http://localhost:8000/logs?level=error&offset=18" | grep cache-status
cache-status: Alcazaba; hit; ttl=10
```

Pages hold at most 100 lines, checked before anything is queried:

```console
$ curl -i "http://localhost:8000/logs?limit=500"
HTTP/1.1 422 Unprocessable Content
content-type: application/json

{"detail":[{"type":"less_than_equal","loc":["query","limit"],"msg":"Input should be less than or equal to 100","input":"500","ctx":{"le":100}}]}
```

## What to look at

- **One row more, no count:** the database is asked for `limit + 1` rows (`params.fetch`); the
  extra one isn't returned, it only says that there's a next page. Nothing counts the whole log,
  which on a big table takes longer than the page itself.
- **No total, no last page:** the links are first, previous and, while `has_more`, next. A client
  that needs the number of pages or the last one wants
  [PaginationLimitOffset](limit-offset.md), which counts.
- **Lines written meanwhile shift the pages:** a line logged while page one is read pushes its
  last line into page two, which repeats it. To follow a list that grows at the top without
  that, page by id: [PaginationIdBased](id-based.md).
- **A short expiry:** the log changes all the time, so each page is cached for ten seconds,
  under its own key (`offset`, `limit` and `level`). The level is an enum: clients can't create
  cache entries without end.

## The code

<!-- fmt: off -->
```python title="examples/pagination/offset.py"
--8<-- "examples/pagination/offset.py"
```
<!-- fmt: on -->
