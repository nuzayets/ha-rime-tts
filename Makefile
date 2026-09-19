.PHONY: test lint format check

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --select I --fix .
	uv run ruff format .

check: lint test
