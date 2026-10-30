# PaginationScroll

--8<-- "docs/examples/animations/pagination_scroll.svg"

An export of orders: a script reads every order in batches while the shop keeps selling, and
gets them as they were when it started. The query runs once; its results are kept in the cache's
backend, and each `scroll_id` reads the next batch of them. The strategy:
[PaginationScroll](../../guide/pagination/scroll.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.scroll:app
```

Set `SCROLL_SECRET` and `CACHE_SIGNING_KEY` (`secrets.token_urlsafe(32)` each), the same in every
process. Without them each start makes up throwaway ones, and the scroll ids stop working when the
app restarts.

## Try it

The export starts: the first four of the 12 orders, and the `scroll_id` of the next batch, in the
body and in the `Link` header:

```console
$ curl -i "http://localhost:8000/orders/export?batch_size=4"
HTTP/1.1 200 OK
content-type: application/json
link: </orders/export?batch_size=4&scroll_id=WyJsR3NqcG9ZNGo5QVZIMGFoU3ZJZDVRIiw0XQ.3VRVV5FF93szTc6nwQegzA>; rel="next"

{"items":[{"customer":"customer-1","amount":10.0,"id":1,"status":"paid"},{"customer":"customer-2","amount":20.0,"id":2,"status":"paid"},{"customer":"customer-3","amount":30.0,"id":3,"status":"shipped"},{"customer":"customer-4","amount":40.0,"id":4,"status":"paid"}],"scroll_id":"WyJsR3NqcG9ZNGo5QVZIMGFoU3ZJZDVRIiw0XQ.3VRVV5FF93szTc6nwQegzA","total":12}
```

An order is placed meanwhile:

```console
$ curl -X POST http://localhost:8000/orders -H "Content-Type: application/json" -d '{"customer": "late", "amount": 99.5}'
{"customer":"late","amount":99.5,"id":13,"status":"paid"}
```

The export goes on with the orders it found: still 12, nothing seen twice. The same request
again, as a script does when the response is lost, gets the same batch:

```console
$ curl "http://localhost:8000/orders/export?batch_size=4&scroll_id=WyJsR3NqcG9ZNGo5QVZIMGFoU3ZJZDVRIiw0XQ.3VRVV5FF93szTc6nwQegzA"
{"items":[{"customer":"customer-5","amount":50.0,"id":5,"status":"paid"},{"customer":"customer-6","amount":60.0,"id":6,"status":"shipped"},{"customer":"customer-7","amount":70.0,"id":7,"status":"paid"},{"customer":"customer-8","amount":80.0,"id":8,"status":"paid"}],"scroll_id":"WyJsR3NqcG9ZNGo5QVZIMGFoU3ZJZDVRIiw4XQ.FvL2Kiaq0Y5sccGOjqoMZQ","total":12}

$ curl "http://localhost:8000/orders/export?batch_size=4&scroll_id=WyJsR3NqcG9ZNGo5QVZIMGFoU3ZJZDVRIiw0XQ.3VRVV5FF93szTc6nwQegzA"
{"items":[{"customer":"customer-5","amount":50.0,"id":5,"status":"paid"},{"customer":"customer-6","amount":60.0,"id":6,"status":"shipped"},{"customer":"customer-7","amount":70.0,"id":7,"status":"paid"},{"customer":"customer-8","amount":80.0,"id":8,"status":"paid"}],"scroll_id":"WyJsR3NqcG9ZNGo5QVZIMGFoU3ZJZDVRIiw4XQ.FvL2Kiaq0Y5sccGOjqoMZQ","total":12}
```

The last batch has no `scroll_id`, and no `Link`:

```console
$ curl "http://localhost:8000/orders/export?batch_size=4&scroll_id=WyJsR3NqcG9ZNGo5QVZIMGFoU3ZJZDVRIiw4XQ.FvL2Kiaq0Y5sccGOjqoMZQ"
{"items":[{"customer":"customer-9","amount":90.0,"id":9,"status":"shipped"},{"customer":"customer-10","amount":100.0,"id":10,"status":"paid"},{"customer":"customer-11","amount":110.0,"id":11,"status":"paid"},{"customer":"customer-12","amount":120.0,"id":12,"status":"shipped"}],"scroll_id":null,"total":12}
```

A `scroll_id` holds the snapshot's id and where the next batch starts, signed. One changed by
hand to skip a batch doesn't match its signature:

```console
$ curl -i "http://localhost:8000/orders/export?scroll_id=WyJsR3NqcG9ZNGo5QVZIMGFoU3ZJZDVRIiw4XQ.3VRVV5FF93szTc6nwQegzA"
HTTP/1.1 400 Bad Request

{"detail":"Invalid scroll_id"}
```

A new export runs the query again, and finds the 13 orders. When the script is done with it, it
deletes the snapshot rather than leave it until it expires (60 seconds after its last batch):

```console
$ curl "http://localhost:8000/orders/export?batch_size=10"
{"items":[...],"scroll_id":"WyJwNXRWQU9uV0ZnS3VuM01iSTlNekp3IiwxMF0.9cQSX6b7Hpxm_wVvrAsyAg","total":13}

$ curl -i -X DELETE "http://localhost:8000/orders/export?scroll_id=WyJwNXRWQU9uV0ZnS3VuM01iSTlNekp3IiwxMF0.9cQSX6b7Hpxm_wVvrAsyAg"
HTTP/1.1 204 No Content

$ curl -i "http://localhost:8000/orders/export?batch_size=10&scroll_id=WyJwNXRWQU9uV0ZnS3VuM01iSTlNekp3IiwxMF0.9cQSX6b7Hpxm_wVvrAsyAg"
HTTP/1.1 404 Not Found

{"detail":"Expired or unknown scroll_id"}
```

Batches have a maximum size, checked before anything is queried:

```console
$ curl "http://localhost:8000/orders/export?batch_size=500"
{"detail":[{"type":"less_than_equal","loc":["query","batch_size"],"msg":"Input should be less than or equal to 50","input":"500","ctx":{"le":50}}]}
```

## What to look at

- **One query per export:** without a `scroll_id` the endpoint queries the orders and `start`
  keeps them; with one, `next` reads the batch from what was kept, and the database isn't
  touched.
- **A snapshot:** what was found when the export started, whatever is written meanwhile. Items
  added, changed or deleted don't shift the batches.
- **Retries are safe:** the `scroll_id` says where the batch starts, so the same request gets
  the same batch; reading a batch doesn't move anything forward.
- **Kept alive while it's read:** each batch gives the snapshot another 60 seconds. A script that
  stops for longer gets a 404 and starts again.
- **Bounded:** the query asks for one row more than `max_items`, and `start` refuses more than
  that, rather than keep a million rows in the backend.
- **Sealed in the backend:** the cache's signing key seals the snapshot, so whoever can write to
  the backend can't plant one; an `encryption_key` instead also hides it, for personal data.
- **Not cached:** the endpoint has no `@cache`, since each start keeps a snapshot of its own.

## The code

<!-- fmt: off -->
```python title="examples/pagination/scroll.py"
--8<-- "examples/pagination/scroll.py"
```
<!-- fmt: on -->
