.PHONY: format lint type-check test quality

format:
	PYTHONPATH=. poetry run black .
	PYTHONPATH=. poetry run ruff check . --fix

lint:
	PYTHONPATH=. poetry run ruff check .

type-check:
	PYTHONPATH=. poetry run mypy hacienda_gpt

test:
	PYTHONPATH=. poetry run pytest -q

quality: format lint type-check test
