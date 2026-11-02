# Open Policy Agent

--8<-- "docs/examples/animations/authz_opa.svg"

A shop whose [Open Policy Agent](../../guide/authorization/opa.md) policy decides who reads each
order and who refunds it, asked over the network: everybody reads the catalogue, customers their
own orders and the support staff every order. Support refunds up to 100 euros, and a manager
above that. While OPA is down, every request gets a 503.

Users log in with a fixed token each, which stands in for your authentication: `ana-token` and
`leo-token` (customers), `sam-token` (support) and `max-token` (support and manager).

## Run it

From a clone of the repository, OPA in Docker with the policy, and the app:

```bash
docker run -p 8181:8181 -v "$PWD/examples/authz:/policies:ro" openpolicyagent/opa:1.21.4 \
    run --server --addr 0.0.0.0:8181 /policies/shop.rego
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.authz.opa_service:app
```

`OPA_URL` and `OPA_TOKEN` (environment variables or a `.env` file) point it to another OPA.

## Try it

Everybody reads the catalogue, a token or not:

```console
$ curl http://127.0.0.1:8000/products
[{"id":1,"name":"Olive oil","price":9.5}]
```

Ana reads her orders, not Leo's; Sam, in support, reads every order:

```console
$ curl http://127.0.0.1:8000/orders/1 -H "Authorization: Bearer ana-token"
{"id":1,"customer":"ana","total":40.0,"refunded":false}

$ curl -i http://127.0.0.1:8000/orders/3 -H "Authorization: Bearer ana-token"
HTTP/1.1 404 Not Found
content-type: application/json

{"detail":"No such order"}

$ curl http://127.0.0.1:8000/orders/3 -H "Authorization: Bearer sam-token"
{"id":3,"customer":"leo","total":15.0,"refunded":false}
```

The policy reads the order's total: Sam doesn't refund 250 euros, Max, a manager, does. Ana
doesn't refund her own:

```console
$ curl -i -X POST http://127.0.0.1:8000/orders/2/refund -H "Authorization: Bearer sam-token"
HTTP/1.1 403 Forbidden
content-type: application/json

{"detail":"Refunds are for the support staff, and a manager above 100 euros"}

$ curl -X POST http://127.0.0.1:8000/orders/2/refund -H "Authorization: Bearer max-token"
{"id":2,"customer":"ana","total":250.0,"refunded":true}

$ curl -i -X POST http://127.0.0.1:8000/orders/1/refund -H "Authorization: Bearer ana-token"
HTTP/1.1 403 Forbidden
content-type: application/json

{"detail":"Refunds are for the support staff, and a manager above 100 euros"}
```

Stop OPA, and nothing is let through:

```console
$ curl -i http://127.0.0.1:8000/products
HTTP/1.1 503 Service Unavailable
content-type: application/json

{"detail":"Authorization unavailable"}
```

## What to look at

- **The policy is elsewhere:** the app sends who asks (the user with its roles), the order and
  the action, and OPA answers. Changing who refunds what is a change to `shop.rego`, which OPA
  loads, not a deploy of the app.
- **Attributes in the input:** `input.subject.roles` and `input.resource.total` are what the
  route gives `authz.enforce`, the user the authentication loaded and the order it read.
- **`default allow := false`:** without it, the rule would be undefined for what no body
  matches, which the enforcer refuses with a warning.
- **Fails closed:** OPA down is a 503, never a request let through, and after 5 failures the
  circuit breaker refuses at once instead of waiting for OPA on every request.
- **404 for what you may not read:** an order a user may not read doesn't exist for them.

## The code

```rego title="examples/authz/shop.rego"
--8<-- "examples/authz/shop.rego"
```

<!-- fmt: off -->
```python title="examples/authz/opa_service.py"
--8<-- "examples/authz/opa_service.py"
```
<!-- fmt: on -->
