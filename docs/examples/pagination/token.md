# PaginationToken

--8<-- "docs/examples/animations/pagination_token.svg"

A library's books, a page at a time: the first request asks for a page size, and each page hands
out a `next_page_token` for the one after. The token is opaque and signed, bound to the query it
was issued for, and lasts ten minutes. Each page is cached for a minute. The strategy:
[PaginationToken](../../guide/pagination/token.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.page_token:app
```

Set `PAGE_TOKEN_SECRET` (`secrets.token_urlsafe(32)`), the same in every process. Without it each
start makes up a throwaway one, and the tokens stop working when the app restarts, or on another
process.

## Try it

The first page of poetry, four books, with the token of the next page in the body and in the
[`Link`](https://www.rfc-editor.org/rfc/rfc8288) header:

```console
$ curl -i "http://localhost:8000/books?genre=poetry&page_size=4"
HTTP/1.1 200 OK
link: </books?genre=poetry&page_size=4&page_token=WzQsIlpqSEdfOThjTnE4UWpGOHluQzJfYXciLDE3OTMzNTE4MjVd.i67GWpoWCJRwK-pdeD32Vw>; rel="next"
etag: W/"166a5b996896154fe0b3897c8c0f1a95"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60

{"items":[{"id":3,"title":"Book 3","genre":"poetry"},{"id":6,"title":"Book 6","genre":"poetry"},{"id":9,"title":"Book 9","genre":"poetry"},{"id":12,"title":"Book 12","genre":"poetry"}],"next_page_token":"WzQsIlpqSEdfOThjTnE4UWpGOHluQzJfYXciLDE3OTMzNTE4MjVd.i67GWpoWCJRwK-pdeD32Vw"}
```

The token, with the same query, gives the next page, cached like the first one, and so on until
`next_page_token` is null:

```console
$ TOKEN=WzQsIlpqSEdfOThjTnE4UWpGOHluQzJfYXciLDE3OTMzNTE4MjVd.i67GWpoWCJRwK-pdeD32Vw
$ curl "http://localhost:8000/books?genre=poetry&page_size=4&page_token=$TOKEN"
{"items":[{"id":15,"title":"Book 15","genre":"poetry"},{"id":18,"title":"Book 18","genre":"poetry"},{"id":21,"title":"Book 21","genre":"poetry"},{"id":24,"title":"Book 24","genre":"poetry"}],"next_page_token":"WzgsIlpqSEdfOThjTnE4UWpGOHluQzJfYXciLDE3OTMzNTE4MzVd.ejl-rzlIi-Zn5ai3MPncJA"}

$ curl -si "http://localhost:8000/books?genre=poetry&page_size=4&page_token=$TOKEN" | grep cache-status
cache-status: Alcazaba; hit; ttl=60

$ curl "http://localhost:8000/books?genre=poetry&page_size=4&page_token=WzgsIlpqSEdfOThjTnE4UWpGOHluQzJfYXciLDE3OTMzNTE4MzVd.ejl-rzlIi-Zn5ai3MPncJA"
{"items":[{"id":27,"title":"Book 27","genre":"poetry"},{"id":30,"title":"Book 30","genre":"poetry"}],"next_page_token":null}
```

The token belongs to `genre=poetry`: with another genre, it's refused before anything is queried.
So is a token whose offset a client changed (here, from 4 to 40), since the signature no longer
matches:

```console
$ curl -i "http://localhost:8000/books?genre=novel&page_size=4&page_token=$TOKEN"
HTTP/1.1 400 Bad Request

{"detail":"page_token was issued for other query parameters"}

$ curl -i "http://localhost:8000/books?genre=poetry&page_size=4&page_token=WzQwLCJaakhHXzk4Y05xOFFqRjh5bkMyX2F3IiwxNzkzMzUxODI1XQ.i67GWpoWCJRwK-pdeD32Vw"
HTTP/1.1 400 Bad Request

{"detail":"Invalid page_token"}
```

Ten minutes later, the first token has expired:

```console
$ curl -i "http://localhost:8000/books?genre=poetry&page_size=4&page_token=$TOKEN"
HTTP/1.1 400 Bad Request

{"detail":"Expired page_token"}
```

Pages have a maximum size, checked before anything is queried:

```console
$ curl "http://localhost:8000/books?page_size=1000"
{"detail":[{"type":"less_than_equal","loc":["query","page_size"],"msg":"Input should be less than or equal to 50","input":"1000","ctx":{"le":50}}]}
```

## What to look at

- **Opaque, not secret:** the token is base64 JSON and a signature. Its first part reads
  `[4,"ZjHG_98cNq8QjF8ynC2_aw",1793351825]`: the offset of the next page, the digest of the other
  query parameters (`genre=poetry`), and when it was issued. Clients are meant to send it back as
  it is; the signature (HMAC-SHA256) stops them from changing it.
- **Bound to the query:** every query parameter but `page_token` and `page_size` counts, in any
  order, so a token never jumps to the middle of another list. `page_size` may change from one
  page to the next.
- **Short-lived:** after `ttl` (600 seconds here) a token is a 400, and the client starts again
  from the first page. Nobody bookmarks page 40 of a list that has changed since.
- **From the database:** `params.offset` and `params.fetch` (`page_size` plus one, which tells
  whether there's a next page) go into `LIMIT :fetch OFFSET :offset`, and
  `paginate(rows, params, queried=True)` makes the page and the next token.
- **Cached apart:** the digest of the query is part of `TokenParams`, and so of the cache key: a
  page cached for one query never hands its token to another. A hit hands out the token issued
  when the page was cached, so keep the cache's `expire` well under the token's `ttl`.

## The code

<!-- fmt: off -->
```python title="examples/pagination/page_token.py"
--8<-- "examples/pagination/page_token.py"
```
<!-- fmt: on -->
