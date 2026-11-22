PYTHON ?= python

.PHONY: check lint format-check test
.NOTPARALLEL:

check: lint format-check test

lint:
	ruff check .

format-check:
	ruff format --check .

test:
	$(PYTHON) -m unittest discover -v
