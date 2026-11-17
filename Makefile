.PHONY: install run up down test test-db lint format

install:
	pip install -r requirements-dev.txt -c constraints.txt

# App + PostgreSQL + Redis in Docker (reads .env)
up:
	docker compose up -d --build

down:
	docker compose down

# Local API with reload; needs DATABASE_URL and REDIS_URL from .env
run:
	alembic upgrade head
	uvicorn app.main:app --reload --port 8000

# Unit and API tests; storage tests are skipped without TEST_DATABASE_URL
test:
	python -m pytest -q -rs

# Full suite against a throwaway PostgreSQL in Docker
test-db:
	docker run -d --rm --name llm-sales-agent-test-db -p 5432:5432 \
		-e POSTGRES_USER=parmangal_test -e POSTGRES_PASSWORD=parmangal_test \
		-e POSTGRES_DB=parmangal_test postgres:16
	sleep 5
	TEST_DATABASE_URL=postgresql+asyncpg://parmangal_test:parmangal_test@127.0.0.1:5432/parmangal_test \
		python -m pytest -q -rs; status=$$?; docker stop llm-sales-agent-test-db; exit $$status

lint:
	ruff check .
	ruff format --check .

format:
	ruff check . --fix
	ruff format .
