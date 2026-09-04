#
# Runs unchanged on EC2, App Runner or Fargate: everything comes from the
# environment, nothing is baked in. Never COPY .env into the image.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# No apt layer on purpose. The healthcheck below uses the Python that is already
# here rather than installing curl, which keeps the image smaller and lets the
# build succeed on networks that cannot reach the Debian mirrors. No build
# toolchain is needed either: every wheel we install is manylinux
# (psycopg2-BINARY, not psycopg2).

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Non-root. After COPY so ownership is right.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Hits the real route, which round-trips to Postgres — so an unhealthy database
# marks the container unhealthy instead of leaving it silently broken.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

# One worker: fits a 512 MB free instance, and matches compose. Background jobs
# no longer live in this process (pg_cron, migrations/005), so a second worker
# buys nothing here. Scale with replicas behind the host's load balancer instead.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
