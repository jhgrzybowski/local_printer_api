# Real CUPS + the Gutenprint MG5350 driver + a fake LPD printer, for tests and
# benchmarks without paper. Build from the repository root:
#   docker build -f testbed/Dockerfile -t local-printer-testbed .
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cups cups-client cups-filters printer-driver-gutenprint \
        poppler-utils python3-cups python3-venv curl ca-certificates libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt /tmp/requirements/
RUN python3 -m venv --system-site-packages /venv \
    && /venv/bin/pip install --no-cache-dir -r /tmp/requirements/requirements.txt \
        -r /tmp/requirements/requirements-dev.txt

COPY testbed/fake_lpd.py testbed/start.sh /testbed/
RUN chmod +x /testbed/start.sh

WORKDIR /app
CMD ["/testbed/start.sh", "--wait"]
