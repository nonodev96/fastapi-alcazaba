# Countries and Spain

--8<-- "docs/examples/animations/countries.svg"

An invoicing API's customers, from anywhere. Every address's country is an ISO 3166-1 code,
taken by [`CountryCode`](../guide/countries.md#countries); a Spanish customer must also have a
valid [NIF](../guide/countries.md#spanish-tax-ids), a DNI, NIE, NIF K/L/M or a company's CIF,
and a [postal code](../guide/countries.md#postal-codes) of a province, which the customer is
given. The app also lists the countries and provinces for its forms.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.countries.main:app
```

## Try it

A Spanish company, its NIF written with a hyphen and in lowercase:

```console
$ curl -i http://127.0.0.1:8000/customers -H "Content-Type: application/json" -d '{
    "name": "Aceites del Sur SL", "tax_id": "b-12345674",
    "address": {"street": "Calle Recogidas 1", "city": "Granada", "postal_code": "18001", "country": "es"}}'
HTTP/1.1 201 Created
content-type: application/json

{"id":1,"name":"Aceites del Sur SL","tax_id":"B12345674","tax_id_kind":"CIF","address":{"street":"Calle Recogidas 1","city":"Granada","postal_code":"18001","country":"ES"},"province":{"code":18,"iso_code":"ES-GR","name":"Granada","community":"Andalucía"}}
```

The NIF is stored as `B12345674`, and the customer is in Granada, Andalucía, by the postal code.
A DNI whose letter is wrong, a postal code of no province and a country that isn't one are
refused:

```console
$ curl http://127.0.0.1:8000/customers -H "Content-Type: application/json" -d '{
    "name": "Ana", "tax_id": "12345678A",
    "address": {"street": "Gran Vía 1", "city": "Madrid", "postal_code": "28013", "country": "ES"}}'
{"detail":[{"type":"value_error","loc":["body"],"msg":"Value error, the DNI's control letter doesn't match", ...}]}

$ curl http://127.0.0.1:8000/customers -H "Content-Type: application/json" -d '{
    "name": "Ana", "tax_id": "12345678Z",
    "address": {"street": "Gran Vía 1", "city": "Madrid", "postal_code": "99001", "country": "ES"}}'
{"detail":[{"type":"value_error","loc":["body"],"msg":"Value error, not a Spanish postal code", ...}]}

$ curl http://127.0.0.1:8000/customers -H "Content-Type: application/json" -d '{
    "name": "Ana", "tax_id": "12345678Z",
    "address": {"street": "Gran Vía 1", "city": "Madrid", "postal_code": "28013", "country": "ESP"}}'
{"detail":[{"type":"value_error","loc":["body","address","country"],"msg":"Value error, not an ISO 3166-1 alpha-2 country code", ...}]}
```

A French customer keeps their tax ID as written: the Spanish rules apply in Spain.

```console
$ curl http://127.0.0.1:8000/customers -H "Content-Type: application/json" -d '{
    "name": "Huiles de Provence", "tax_id": "FR40303265045",
    "address": {"street": "1 rue de Rivoli", "city": "Paris", "postal_code": "75001", "country": "FR"}}'
{"id":2,"name":"Huiles de Provence","tax_id":"FR40303265045","tax_id_kind":null,...,"province":null}
```

The lists, for a form: a country by any of its codes, and the provinces of a community.

```console
$ curl http://127.0.0.1:8000/countries/724
{"code":"ES","name":"Spain","flag":"🇪🇸"}

$ curl "http://127.0.0.1:8000/spain/provinces?community=ES-CN"
[{"code":35,"iso_code":"ES-GC","name":"Las Palmas","community":"Canarias"},{"code":38,"iso_code":"ES-TF","name":"Santa Cruz de Tenerife","community":"Canarias"}]
```

## The code

<!-- fmt: off -->
```python title="examples/countries/main.py"
--8<-- "examples/countries/main.py"
```
<!-- fmt: on -->

The province and the NIF's kind are private attributes of `NewCustomer`, set by its validator:
a client can't send them. A field with `exclude=True` would only leave them out of the response,
and still read them from the body.
