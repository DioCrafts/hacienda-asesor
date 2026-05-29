.PHONY: format lint type-check test quality

format:
	uv run black .
	uv run ruff check . --fix

lint:
	uv run ruff check .

type-check:
	uv run mypy hacienda_gpt

test:
	uv run pytest -q

quality: format lint type-check test
