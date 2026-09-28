PYTHON ?= .venv/bin/python
BACKEND_HOST ?= 192.168.100.99
BACKEND_PORT ?= 8000
HEALTH_URL ?= http://$(BACKEND_HOST):$(BACKEND_PORT)/health

.PHONY: test pycompile shellcheck docker-build compose-up healthcheck

test:
	$(PYTHON) -m pytest -q

pycompile:
	$(PYTHON) -m py_compile $$(find app scripts tests -name '*.py')

shellcheck:
	shellcheck scripts/*.sh

docker-build:
	docker build -t local-printer-api:latest .

compose-up:
	docker compose up -d --build

healthcheck:
	curl -fsS "$(HEALTH_URL)"
