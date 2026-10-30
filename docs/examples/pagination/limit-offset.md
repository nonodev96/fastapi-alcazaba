# PaginationLimitOffset

--8<-- "docs/examples/animations/pagination_limit_offset.svg"

A price list for partners by limit and offset: any slice of the products, with the total, and
the filters kept in the links. The strategy:
[PaginationLimitOffset](../../guide/pagination/limit-offset.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.limit_offset:app
```

## Try it

Three olive oils, after the first six:

```console
$ curl -i "http://localhost:8000/prices?category=oil&limit=3&offset=6"
HTTP/1.1 200 OK
content-type: application/json
link: </prices?category=oil&limit=3&offset=0>; rel="first", </prices?category=oil&limit=3&offset=3>; rel="prev", </prices?category=oil&limit=3&offset=9>; rel="next", </prices?category=oil&limit=3&offset=27>; rel="last"
etag: W/"e6a6f68018d1a1bfe853c26557dca8b8"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"sku":"SKU-013","category":"oil","price":"3.25"},{"sku":"SKU-015","category":"oil","price":"3.75"},{"sku":"SKU-017","category":"oil","price":"4.25"}],"total":30,"limit":3,"offset":6}
```

## What to look at

- **The filters stay:** the links keep `category=oil`, and only change `limit` and `offset`.
- **On the grid of the limit:** the first and last slices that the links point at start at
  multiples of `limit` from 0, where a client that starts at the first slice walks.
- **The total:** counted on every slice, as page numbers do. For lists too long to count, see
  [PaginationOffset](../../guide/pagination/offset.md).

## The code

<!-- fmt: off -->
```python title="examples/pagination/limit_offset.py"
--8<-- "examples/pagination/limit_offset.py"
```
<!-- fmt: on -->
