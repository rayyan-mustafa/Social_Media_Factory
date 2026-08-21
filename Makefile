.PHONY: up down build test lint format

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose up --build -d

test:
	python -m pytest tests/

lint:
	flake8 src/ tests/
	black --check src/ tests/

format:
	black src/ tests/
