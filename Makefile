.RECIPEPREFIX := >
PY := .venv/bin/python
.PHONY: setup up down smoke test demo

setup:
> python -m venv .venv
> $(PY) -m pip install -q -U pip
> $(PY) -m pip install -q -r requirements.txt
> test -f .env || cp .env.example .env

up:
> docker compose up -d
> $(PY) -m parachute.cli wait --timeout 90

down:
> docker compose down

smoke:
> $(PY) scripts/smoke_test.py

test:
> $(PY) -m pytest -q

demo:
> $(PY) -m parachute.cli demo
