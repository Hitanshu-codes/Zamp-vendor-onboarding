PY ?= .venv/Scripts/python.exe

.PHONY: install run test seed

install:
	$(PY) -m pip install -r requirements-dev.txt

run:
	$(PY) -m uvicorn app.main:app --reload --port 8000

test:
	$(PY) -m pytest -q

seed:
	$(PY) scripts/seed.py
