# PaginationPageNumber

--8<-- "docs/examples/animations/pagination_page_number.svg"

A blog's archive by page number: numbered pages of posts, newest first, with the total and the
number of pages, each page cached under its own key. The strategy:
[PaginationPageNumber](../../guide/pagination/page-number.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.pagination.page_number:app
```

## Try it

The second page of three posts: the response says how many there are, and links to the pages
around it:

```console
$ curl -i "http://localhost:8000/posts?page=2&page_size=3"
HTTP/1.1 200 OK
content-type: application/json
link: </posts?page=1&page_size=3>; rel="first", </posts?page=1&page_size=3>; rel="prev", </posts?page=3&page_size=3>; rel="next", </posts?page=15&page_size=3>; rel="last"
etag: W/"f33562cc4b3edbd0316e1399d0de90a0"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=300

{"items":[{"id":42,"title":"Post 42"},{"id":41,"title":"Post 41"},{"id":40,"title":"Post 40"}],"total":45,"page":2,"page_size":3,"pages":15}
```

Asked again, it comes from the cache, without a query:

```console
$ curl -si "http://localhost:8000/posts?page=2&page_size=3" | grep cache-status
cache-status: Alcazaba; hit; ttl=300
```

More than 20 posts a page is refused before the endpoint runs:

```console
$ curl -i "http://localhost:8000/posts?page_size=21"
HTTP/1.1 422 Unprocessable Content
```

Publishing a post (`POST /posts`) shifts every page by one: it drops them all, by the `posts`
tag.

## What to look at

- **Only the page, and the count:** the database fetches the page with `params.offset` and
  `params.limit`, and counts the posts: `pages.paginate(rows, params, total=count)`.
- **One entry per page and size:** the page parameters are an argument of the endpoint, so
  `@cache` keeps each page apart.
- **The cost of numbers:** each page counts every post, and a post published while someone reads
  shifts the pages after it. For feeds, see [PaginationCursor](cursor.md).

## The code

<!-- fmt: off -->
```python title="examples/pagination/page_number.py"
--8<-- "examples/pagination/page_number.py"
```
<!-- fmt: on -->
