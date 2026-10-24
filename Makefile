.PHONY: deps lint format test test-integration mutation services-up services-down build clean

deps:
	uv sync --all-extras

lint: deps
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format: deps
	uv run ruff check --fix .
	uv run ruff format .

test: deps
	uv run coverage run -m pytest
	uv run coverage report

INTEGRATION_ENV = REDIS_URL=redis://127.0.0.1:16379/15 VALKEY_URL=valkey://127.0.0.1:16380/15 \
	CLUSTER_URL=redis://127.0.0.1:16382 MEMCACHED_URL=127.0.0.1:21211 DYNAMODB_URL=http://127.0.0.1:18000

# Throwaway servers on high ports (stop them with `make services-down`).
services-up:
	docker run -d --rm --name fastapi-alcazaba-test-redis -p 127.0.0.1:16379:6379 redis:8
	docker run -d --rm --name fastapi-alcazaba-test-valkey -p 127.0.0.1:16380:6379 valkey/valkey:8
	docker run -d --rm --name fastapi-alcazaba-test-cluster -p 127.0.0.1:16382:16382 valkey/valkey:8 \
		valkey-server --port 16382 --cluster-enabled yes --cluster-announce-ip 127.0.0.1
	docker run -d --rm --name fastapi-alcazaba-test-memcached -p 127.0.0.1:21211:11211 memcached:1.6
	docker run -d --rm --name fastapi-alcazaba-test-dynamodb -p 127.0.0.1:18000:8000 amazon/dynamodb-local:3.3.2
	sleep 2 && docker exec fastapi-alcazaba-test-cluster valkey-cli -p 16382 cluster addslotsrange 0 16383

services-down:
	docker stop fastapi-alcazaba-test-redis fastapi-alcazaba-test-valkey fastapi-alcazaba-test-cluster \
		fastapi-alcazaba-test-memcached fastapi-alcazaba-test-dynamodb

test-integration: deps
	$(INTEGRATION_ENV) uv run pytest -m integration

# Mutation testing of the unit suite (slow: every mutant runs the tests). Results: mutmut results.
mutation: deps
	uv run mutmut run --max-children $$(nproc)
	uv run mutmut export-cicd-stats
	cat mutants/mutmut-cicd-stats.json

build: clean
	uv build

clean:
	rm -rf dist
