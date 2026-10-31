# PaginationPageNumber

Numbered pages, with the total: `?page=2&page_size=20`.

In an app: [the example](../../examples/pagination/page-number.md). How strategies work: [Pagination](index.md#strategies). Every argument: [the API reference](../../reference/pagination.md#fastapi_alcazaba.pagination.page_number.PaginationPageNumber).

Numbered pages, with the total: `GET /products?page=2&page_size=20` answers
`{"items": [...], "total": 245, "page": 2, "page_size": 20, "pages": 13}`. For lists that people
browse page by page, jump through, and want to know the size of.

- **From a database:** query only the page, with `params.offset` and `params.limit`, and count:
  `pages.paginate(rows, params, total=count)`. Without `total`, `paginate` slices the page from
  the whole list.
- **Links:** first, previous, next and last.
- **Its cost:** counting the whole list on every page, and the shifting of pages by position.
