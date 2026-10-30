# PaginationRange

--8<-- "docs/examples/animations/pagination_range.svg"

A contact list for a data grid, which asks for the rows it shows by the indexes of the first and
the last, and reads which ones it got, and how many there are in all, from `Content-Range`. The
strategy: [PaginationRange](../../guide/pagination/range.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.ranges:app
```

## Try it

The first three contacts, items 0 to 2 of 319:

```console
$ curl -i "http://localhost:8000/contacts?start=0&end=2"
HTTP/1.1 200 OK
content-type: application/json
content-range: items 0-2/319
link: </contacts?start=0&end=2>; rel="first", </contacts?start=3&end=5>; rel="next", </contacts?start=318&end=320>; rel="last"
etag: W/"76667f0812a81129bb32cdab8752ca68"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"name":"Contact 001","email":"contact1@example.com","id":1},{"name":"Contact 002","email":"contact2@example.com","id":2},{"name":"Contact 003","email":"contact3@example.com","id":3}],"start":0,"end":2,"total":319,"limit":3}
```

The last range of five holds four: `end` and `Content-Range` say which items came, while the
links keep the size asked for. Asked again, it comes from the cache, `Content-Range` and links
included:

```console
$ curl -i "http://localhost:8000/contacts?start=315&end=319"
HTTP/1.1 200 OK
content-type: application/json
content-range: items 315-318/319
link: </contacts?start=0&end=4>; rel="first", </contacts?start=310&end=314>; rel="prev", </contacts?start=315&end=319>; rel="last"
etag: W/"dfb6651db798fb4f78bdba3a7f653b34"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"name":"Contact 316","email":"contact316@example.com","id":316},{"name":"Contact 317","email":"contact317@example.com","id":317},{"name":"Contact 318","email":"contact318@example.com","id":318},{"name":"Contact 319","email":"contact319@example.com","id":319}],"start":315,"end":318,"total":319,"limit":5}

$ curl -si "http://localhost:8000/contacts?start=315&end=319" | grep -E "cache-status|content-range"
cache-status: Alcazaba; hit; ttl=300
content-range: items 315-318/319
```

A range that starts past the last contact is a 416, which says how many there are:

```console
$ curl -i "http://localhost:8000/contacts?start=319"
HTTP/1.1 416 Range Not Satisfiable
content-range: items */319
content-type: application/json

{"detail":"Range Not Satisfiable"}
```

A range that ends before it starts, or holds more than 100 contacts, is a 400:

```console
$ curl -i "http://localhost:8000/contacts?start=10&end=5"
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"Invalid range: end is before start"}

$ curl -i "http://localhost:8000/contacts?start=0&end=499"
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"Invalid range: more than 100 items"}
```

## What to look at

- **Both ends included, from 0:** `start=0&end=24` is 25 contacts; without `end`, a range holds
  `default_items` (25 here). For the database, `params.offset` and `params.limit`.
- **The total twice:** in the body (`total`) and in `Content-Range: items 0-24/319`, where a grid
  that reads headers finds it; `items */319` when the range holds nothing.
- **400 or 416:** a range that can't be one (`end` before `start`, or too big) is the client's
  mistake, a 400 before anything is queried. One that starts past the end is a 416, after the
  count: the list may have shrunk since the client last read its total.
- **Cached with its headers:** each range is an entry, and a hit gets its `Content-Range` and
  links back. A new contact (`POST /contacts`) changes the total, so it drops every range.

## The code

<!-- fmt: off -->
```python title="examples/pagination/ranges.py"
--8<-- "examples/pagination/ranges.py"
```
<!-- fmt: on -->
