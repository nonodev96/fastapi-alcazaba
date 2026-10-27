# Pagination

A product catalogue paginated with [`FastAPIPagination`](../index.md#pagination) and
[`FastAPIPaginationCursor`](../index.md#by-cursor), and cached three ways: one entry per page,
one entry for a whole short list sliced into pages, and one entry per cursor. Every list is
tagged, so adding a product drops every page at once.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.main:app
```

Cursors are signed: set `CURSOR_SECRET` (`secrets.token_urlsafe(32)`), the same in every process,
or a cursor made by one process is refused by the others.

## Try it

A page, computed once and then served from the cache, which links to the pages around it in the
[`Link`](https://www.rfc-editor.org/rfc/rfc8288) header:

```console
$ curl -i "http://127.0.0.1:8000/products?page=2&size=3"
HTTP/1.1 200 OK
link: </products?page=1&size=3>; rel="first", </products?page=1&size=3>; rel="prev", </products?page=3&size=3>; rel="next", </products?page=20&size=3>; rel="last"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"name":"Product 4","category":"vegetables","price":1.4,"id":4},{"name":"Product 5","category":"oil","price":1.5,"id":5},{"name":"Product 6","category":"fruit","price":1.6,"id":6}],"total":60,"page":2,"size":3,"pages":20}

$ curl -si "http://127.0.0.1:8000/products?page=2&size=3" | grep cache-status
cache-status: Alcazaba; hit; ttl=300
```

Pages have a maximum size, checked before anything is queried:

```console
$ curl "http://127.0.0.1:8000/products?size=1000"
{"detail":[{"type":"less_than_equal","loc":["query","size"],"msg":"Input should be less than or equal to 100","input":"1000","ctx":{"le":100}}]}
```

By cursor, newest first: each `next_cursor` gives the products right after the last one seen,
however many are added meanwhile. Cursors are signed, so a made-up one is refused:

```console
$ curl "http://127.0.0.1:8000/products/newest?size=2"
{"items":[{"name":"Product 60",...,"id":60},{"name":"Product 59",...,"id":59}],"size":2,"next_cursor":"NTk.fYIHDRFKRMk2AgxgOKB_Xw"}

$ curl "http://127.0.0.1:8000/products/newest?size=2&cursor=NTk.fYIHDRFKRMk2AgxgOKB_Xw"
{"items":[{"name":"Product 58",...,"id":58},{"name":"Product 57",...,"id":57}],"size":2,"next_cursor":"NTc.Vp3GtrQxFhsATIf18CX4aA"}

$ curl -si "http://127.0.0.1:8000/products/newest?size=2" | grep link
link: </products/newest?size=2>; rel="first", </products/newest?cursor=NTk.fYIHDRFKRMk2AgxgOKB_Xw&size=2>; rel="next"

$ curl -i "http://127.0.0.1:8000/products/newest?cursor=abc"
HTTP/1.1 400 Bad Request

{"detail":"Invalid cursor"}
```

Adding a product drops every cached page:

```console
$ curl -X POST http://127.0.0.1:8000/products -H "Content-Type: application/json" \
    -d '{"name": "Capers", "category": "vegetables", "price": 2.5}'
{"name":"Capers","category":"vegetables","price":2.5,"id":61}

$ curl -si "http://127.0.0.1:8000/products?page=2&size=3" | grep cache-status
cache-status: Alcazaba; fwd=miss; stored
```

## What to look at

- **The page parameters are arguments of the endpoint,** so `@cache` keeps each page, size and
  category under its own key. The catalogue (the database session in a real app) is left out of
  the key with `exclude_params`, since it changes from one request to the next.
- **Categories are an enum, not any string:** clients can't create cache entries without end.
- **Three strategies for three kinds of lists:** a page per entry for big lists where most clients
  read the first pages; the whole list once (`get_or_set`) for short ones; a cursor for infinite
  scroll and clients that sync everything.
- **Tags:** every list is tagged `products`, and a write drops them all without knowing their
  keys.
- **Public data:** the cache is created with `vary_headers=()`, so pages are shared by everybody.
  Don't do that for anything that depends on who asks.
- **Links made for each request:** a cached page gets the `Link` header of the request it answers,
  with that request's other query parameters; the links aren't stored with the entry, where one
  client's parameters would reach everybody else.

## The code

<!-- fmt: off -->
```python title="examples/pagination/main.py"
--8<-- "examples/pagination/main.py"
```
<!-- fmt: on -->
