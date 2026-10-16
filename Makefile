.PHONY: up down migrate seed test lint demo benchmark acceptance package
up:
	docker compose up --build -d
down:
	docker compose down
migrate:
	docker compose run --rm migrate
seed:
	docker compose exec api python scripts/seed.py
demo: seed
test:
	python -m pytest
lint:
	python -m ruff check backend tests
benchmark:
	python scripts/benchmark.py
acceptance:
	python scripts/reliability.py
package:
	python scripts/package.py
