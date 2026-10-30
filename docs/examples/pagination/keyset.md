# PaginationKeyset

--8<-- "docs/examples/animations/pagination_keyset.svg"

A shop's olive oils, cheapest first, browsed with "Previous" and "Next": `after` gives the page
after a key, `before` the page before one, each a query through an index on `(price, id)`, as
fast on the last page as on the first. The strategy:
[PaginationKeyset](../../guide/pagination/keyset.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.keyset:app
```

Set `KEYSET_SECRET` (`secrets.token_urlsafe(32)`), the same in every process. Without it each
start makes up a throwaway one, and the keys stop working when the app restarts.

## Try it

The three cheapest, and the key of the last one in `after`. There's no page before the first,
so `before` is null:

```console
$ curl -i "http://localhost:8000/products"
HTTP/1.1 200 OK
content-type: application/json
link: </products?limit=3>; rel="first", </products?after=WzQuMiwxMl0.zM2TmzuDyg8Vo75Stxcmwg&limit=3>; rel="next"
etag: W/"038bf00da3a11ca2f20291641a8ec769"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"name":"Arbequina 250 ml","price":3.9,"id":7},{"name":"Cornicabra 250 ml","price":4.2,"id":4},{"name":"Empeltre 250 ml","price":4.2,"id":12}],"limit":3,"before":null,"after":"WzQuMiwxMl0.zM2TmzuDyg8Vo75Stxcmwg"}
```

The next page, which links to the pages on both sides of it, and is cached under its own key:

```console
$ curl -i "http://localhost:8000/products?after=WzQuMiwxMl0.zM2TmzuDyg8Vo75Stxcmwg"
HTTP/1.1 200 OK
content-type: application/json
link: </products?limit=3>; rel="first", </products?before=WzUuOSw2XQ.1o7f5U_K7c_feS-Wp_MOnQ&limit=3>; rel="prev", </products?after=WzYuNSwzXQ.k8bInIhY5pO55FxwXz--sQ&limit=3>; rel="next"
etag: W/"46e9660cca1657c3e339bab191af58c7"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"name":"Picual 500 ml","price":5.9,"id":6},{"name":"Arbequina 500 ml","price":6.5,"id":1},{"name":"Hojiblanca 500 ml","price":6.5,"id":3}],"limit":3,"before":"WzUuOSw2XQ.1o7f5U_K7c_feS-Wp_MOnQ","after":"WzYuNSwzXQ.k8bInIhY5pO55FxwXz--sQ"}

$ curl -si "http://localhost:8000/products?after=WzQuMiwxMl0.zM2TmzuDyg8Vo75Stxcmwg" | grep cache-status
cache-status: Alcazaba; hit; ttl=300
```

Two more pages forward, the last one with `after` null:

```console
$ curl "http://localhost:8000/products?after=WzYuNSwzXQ.k8bInIhY5pO55FxwXz--sQ"
{"items":[{"name":"Cornicabra 500 ml","price":7.2,"id":10},{"name":"Empeltre 500 ml","price":7.8,"id":5},{"name":"Picual 1 l","price":9.9,"id":2}],"limit":3,"before":"WzcuMiwxMF0.HDEHygRJ9v31_0oGx4c33g","after":"WzkuOSwyXQ.8dnKeWm85oyCQfuRmnPbQg"}

$ curl -i "http://localhost:8000/products?after=WzkuOSwyXQ.8dnKeWm85oyCQfuRmnPbQg"
HTTP/1.1 200 OK
content-type: application/json
link: </products?limit=3>; rel="first", </products?before=WzkuOSw5XQ.pNc7AuuUCfeauD_KoWyfJA&limit=3>; rel="prev"
etag: W/"d26bdc92ea03d5fa03aa5f983561cff0"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"name":"Hojiblanca 1 l","price":9.9,"id":9},{"name":"Manzanilla 1 l","price":11.5,"id":8},{"name":"Picual 5 l","price":38.0,"id":11}],"limit":3,"before":"WzkuOSw5XQ.pNc7AuuUCfeauD_KoWyfJA","after":null}
```

The two oils at 9.9 fall on two pages, and neither is lost nor repeated: the key is the price and
the id. Back from the last page, `before` gives the third page again, with the same keys:

```console
$ curl "http://localhost:8000/products?before=WzkuOSw5XQ.pNc7AuuUCfeauD_KoWyfJA"
{"items":[{"name":"Cornicabra 500 ml","price":7.2,"id":10},{"name":"Empeltre 500 ml","price":7.8,"id":5},{"name":"Picual 1 l","price":9.9,"id":2}],"limit":3,"before":"WzcuMiwxMF0.HDEHygRJ9v31_0oGx4c33g","after":"WzkuOSwyXQ.8dnKeWm85oyCQfuRmnPbQg"}
```

A product added meanwhile doesn't shift the pages: the second one is still the products after
the key, and only the first page has the new oil:

```console
$ curl -X POST http://localhost:8000/products -H "Content-Type: application/json" -d '{"name": "Picual 250 ml", "price": 3.5}'
{"name":"Picual 250 ml","price":3.5,"id":13}

$ curl "http://localhost:8000/products?after=WzQuMiwxMl0.zM2TmzuDyg8Vo75Stxcmwg"
{"items":[{"name":"Picual 500 ml","price":5.9,"id":6},{"name":"Arbequina 500 ml","price":6.5,"id":1},{"name":"Hojiblanca 500 ml","price":6.5,"id":3}],"limit":3,"before":"WzUuOSw2XQ.1o7f5U_K7c_feS-Wp_MOnQ","after":"WzYuNSwzXQ.k8bInIhY5pO55FxwXz--sQ"}

$ curl "http://localhost:8000/products"
{"items":[{"name":"Picual 250 ml","price":3.5,"id":13},{"name":"Arbequina 250 ml","price":3.9,"id":7},{"name":"Cornicabra 250 ml","price":4.2,"id":4}],"limit":3,"before":null,"after":"WzQuMiw0XQ.3qBOxW7lhzvI3kAQ4RZo6g"}
```

A key without its signature (its first part alone is `[9.9,2]` in base64), and both directions
at once, are refused before anything is queried:

```console
$ curl -i "http://localhost:8000/products?after=WzkuOSwyXQ"
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"Invalid after"}

$ curl -i "http://localhost:8000/products?after=WzkuOSwyXQ.8dnKeWm85oyCQfuRmnPbQg&before=WzkuOSw5XQ.pNc7AuuUCfeauD_KoWyfJA"
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"Give after or before, not both"}
```

## What to look at

- **A unique key:** `(price, id)`, since two oils may cost the same; the database has an index
  on both, so every page, the last one too, is a short walk along it.
- **Backwards, nearest first:** with `before`, the database reads the index the other way
  (`ORDER BY price DESC, id DESC`), and `paginate` puts the rows back in order.
- **One row more:** each query fetches `params.fetch` rows (the limit and one), which tells
  whether there's another page that way. The other way there's always one: the page the key
  came from.
- **Readable, not changeable:** the keys are base64 JSON, so clients can read the price and the
  id, and signed, so they can't change them.
- **Cached page by page:** each key and limit is an entry of its own, and adding a product drops
  them all (tag `products`).

## The code

<!-- fmt: off -->
```python title="examples/pagination/keyset.py"
--8<-- "examples/pagination/keyset.py"
```
<!-- fmt: on -->
