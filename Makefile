.PHONY: demo install test seed

demo: install
	.venv/bin/python -m payquill

install:
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements-dev.txt

test: install
	.venv/bin/python -m pytest -q

seed: install
	.venv/bin/python -m payquill.seed
