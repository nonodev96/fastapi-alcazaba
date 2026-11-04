.PHONY: deps lint format test test-integration mutation services-up services-down docs docs-serve \
	build clean fuzz-requirements

# Python's own tools: a virtual environment in .venv, and pip (26.2 or later, for --group and
# --uploaded-prior-to). pip takes no release younger than three days: compromised versions are
# usually pulled sooner.
PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
export PIP_UPLOADED_PRIOR_TO := P3D
export PIP_DISABLE_PIP_VERSION_CHECK := 1

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

# The package in editable mode with every extra, and the development tools.
deps: $(BIN)/python
	$(BIN)/python -m pip --version > /dev/null 2>&1 || $(BIN)/python -m ensurepip --upgrade
	$(BIN)/python -m pip install --quiet --upgrade "pip>=26.2"
	$(BIN)/python -m pip install --quiet -e ".[all]" --group dev

SOURCES = fastapi_alcazaba examples tests scripts fuzz

# Multi-line signatures: one parameter per line, the last one with a comma. ruff format only
# guarantees it once the comma is there; scripts/check_signatures.py checks it (--fix adds it).
lint: deps
	$(BIN)/ruff check .
	$(BIN)/python scripts/check_signatures.py $(SOURCES)
	$(BIN)/ruff format --check .
	$(BIN)/mypy
	$(BIN)/pylint $(SOURCES)

format: deps
	$(BIN)/ruff check --fix .
	$(BIN)/python scripts/check_signatures.py --fix $(SOURCES)
	$(BIN)/ruff format .

test: deps
	$(BIN)/coverage run -m pytest
	$(BIN)/coverage report

INTEGRATION_ENV = REDIS_URL=redis://127.0.0.1:16379/15 VALKEY_URL=valkey://127.0.0.1:16380/15 \
	CLUSTER_URL=redis://127.0.0.1:16382 MEMCACHED_URL=127.0.0.1:21211 DYNAMODB_URL=http://127.0.0.1:18000 \
	OPA_URL=http://127.0.0.1:18181 POSTGRES_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:15432/postgres \
	OIDC_LIVE_ISSUER=http://localhost:18090/realms/alcazaba OIDC_LIVE_CLIENT_ID=alcazaba-example \
	OIDC_LIVE_CLIENT_SECRET=test-only-client-secret-of-the-ci-realm OIDC_LIVE_USERNAME=ana \
	OIDC_LIVE_PASSWORD=test-only-password-of-the-ci-realm MINIO_URL=http://127.0.0.1:19000 \
	CLAMAV_URL=tcp://127.0.0.1:13310 MAILPIT_URL=http://127.0.0.1:18025 MAILPIT_SMTP_PORT=11025

# Throwaway servers on high ports (stop them with `make services-down`).
services-up:
	docker run -d --rm --name fastapi-alcazaba-test-redis -p 127.0.0.1:16379:6379 redis:8
	docker run -d --rm --name fastapi-alcazaba-test-valkey -p 127.0.0.1:16380:6379 valkey/valkey:8
	docker run -d --rm --name fastapi-alcazaba-test-cluster -p 127.0.0.1:16382:16382 valkey/valkey:8 \
		valkey-server --port 16382 --cluster-enabled yes --cluster-announce-ip 127.0.0.1
	docker run -d --rm --name fastapi-alcazaba-test-memcached -p 127.0.0.1:21211:11211 memcached:1.6
	docker run -d --rm --name fastapi-alcazaba-test-dynamodb -p 127.0.0.1:18000:8000 amazon/dynamodb-local:3.3.2
	docker run -d --rm --name fastapi-alcazaba-test-opa -p 127.0.0.1:18181:8181 openpolicyagent/opa:1.21.4 \
		run --server --addr 0.0.0.0:8181
	docker run -d --rm --name fastapi-alcazaba-test-postgres -p 127.0.0.1:15432:5432 \
		-e POSTGRES_PASSWORD=postgres postgres:17
	docker run -d --rm --name fastapi-alcazaba-test-keycloak -p 127.0.0.1:18090:8080 \
		-e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
		-v "$(CURDIR)/tests/integration/keycloak:/opt/keycloak/data/import:ro" \
		quay.io/keycloak/keycloak:26.4 start-dev --import-realm
	docker run -d --rm --name fastapi-alcazaba-test-minio -p 127.0.0.1:19000:9000 \
		-e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
		cgr.dev/chainguard/minio:latest@sha256:3c60a2a8c4d33409a9bc7a623a7fcbfb4605adc3d89422d2f53b3f673a6e5597 server /data
	docker run -d --rm --name fastapi-alcazaba-test-mailpit -p 127.0.0.1:11025:1025 \
		-p 127.0.0.1:18025:8025 axllent/mailpit:v1.32.0
	# clamd loads its signatures for a minute or two before it answers (not in CI: too heavy).
	docker run -d --rm --name fastapi-alcazaba-test-clamav -p 127.0.0.1:13310:3310 clamav/clamav:1.4
	sleep 2 && docker exec fastapi-alcazaba-test-cluster valkey-cli -p 16382 cluster addslotsrange 0 16383

services-down:
	docker stop fastapi-alcazaba-test-redis fastapi-alcazaba-test-valkey fastapi-alcazaba-test-cluster \
		fastapi-alcazaba-test-memcached fastapi-alcazaba-test-dynamodb fastapi-alcazaba-test-opa \
		fastapi-alcazaba-test-postgres fastapi-alcazaba-test-keycloak fastapi-alcazaba-test-minio \
		fastapi-alcazaba-test-clamav fastapi-alcazaba-test-mailpit

test-integration: deps
	$(INTEGRATION_ENV) $(BIN)/pytest -m integration

# Mutation testing of the unit suite (slow: every mutant runs the tests). Results: mutmut results.
mutation: deps
	$(BIN)/mutmut run --max-children $$(nproc)
	$(BIN)/mutmut export-cicd-stats
	cat mutants/mutmut-cicd-stats.json

# The documentation as CI builds it (into site/), and a preview that reloads on every change
# (http://127.0.0.1:8000).
docs: deps
	$(BIN)/python -m pip install --quiet --group docs
	$(BIN)/mkdocs build --strict

# The documentation at http://$(DOCS_ADDR), rebuilt on every change (DOCS_ADDR=0.0.0.0:8000 to
# reach it from other machines).
DOCS_ADDR ?= 127.0.0.1:8000

docs-serve: deps
	$(BIN)/python -m pip install --quiet --group docs
	$(BIN)/mkdocs serve --dev-addr $(DOCS_ADDR)

# The sdist and the wheel, in dist/.
build: clean deps
	$(BIN)/python -m pip install --quiet --group package
	$(BIN)/python -m build

clean:
	rm -rf dist site

# The requirements of the fuzzing build, pinned by hash (.clusterfuzzlite/requirements.txt). pip
# resolves them with Python 3.11, the one in OSS-Fuzz's image: it evaluates markers for its own.
FUZZ_PYTHON ?= python3.11
fuzz-requirements:
	$(FUZZ_PYTHON) scripts/fuzz_requirements.py
