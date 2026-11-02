# Data Model: Whole cached responses

## Stored entry

What `coder.encode` produced until now is still what a bare value is stored as. Anything more
is stored as an **envelope**:

```text
\x00alcazaba-entry:1\n            magic + format version, one line
{"kind": ..., "status": ..., "headers": [[name, value], ...]}\n   metadata, one line of JSON
<payload bytes>                   the rest, raw
```

Then compressed, signed or encrypted as configured, exactly like a bare value.

| Field | Values | Meaning |
| --- | --- | --- |
| `kind` | `"response"` | a `Response` the endpoint returned: the payload is its body |
| | `"value"` | a value the endpoint returned, encoded by the coder: the payload is `coder.encode(value)` |
| | `"rendered"` | a value rendered through the route's response model: the payload is JSON, returned as decoded |
| `status` | int | the status (200; kept for the next change, which may cache others) |
| `headers` | list of `[name, value]`, latin-1 strings, order and repeats kept | the storable headers: for `response`, the returned response's; for `value`/`rendered`, those the endpoint added to the injected response |

### Validation when read

- Magic absent → legacy bare value: decoded by the coder, as today.
- Magic present, version unknown, metadata not one JSON object with these fields and types, a kind
  unknown, a header that isn't storable (R4) → the entry is unusable: logged, recomputed.
- A forbidden header can't come back from the backend even if someone wrote one: headers are
  filtered again when read.

### Storable headers

Every header except: `set-cookie`, hop-by-hop (`connection`, `keep-alive`, `proxy-authenticate`,
`proxy-authorization`, `te`, `trailer`, `transfer-encoding`, `upgrade`), `content-length`,
`cache-status`, `etag`, `age`, `date`; and `link` on paginated calls.

## Cache-Control policy

Inputs: `cache_control` (directives from `@cache`, or None), the endpoint's own header (from the
target response or the stored headers), `private` (the entry is per user), `max_age` (remaining
lifetime), `stale_while_revalidate`.

| Configured `cache_control` | Endpoint's own header | Sent |
| --- | --- | --- |
| set | any | the configured directives, + `max-age=<remaining>` if absent, + `stale-while-revalidate` if set |
| None | set | the endpoint's header, verbatim |
| None | none | today's: `private` if per user, `max-age=<remaining>`, `stale-while-revalidate` |

Then, if `private`: drop `public`, ensure `private` is first.
