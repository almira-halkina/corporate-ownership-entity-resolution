# Two stages. The builder runs the batch pipeline over the committed fixture
# corpus and flattens it into the serving index; the runtime image carries that
# artifact and the API, and nothing else.
#
# Baking the index into the image rather than mounting a volume or provisioning
# a database is what lets this deploy as a single container with no managed
# dependency: the index is immutable between pipeline runs, so it is build
# output, not state. A new corpus means a new image, which also makes a deploy
# reproducible and a rollback a tag change.

# ---------------------------------------------------------------- builder
FROM python:3.11-slim AS builder

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY fixtures/ ./fixtures/

# Paths are pinned explicitly. `Settings.paths` derives its default root from
# the package location, which is the repo when installed editable but
# site-packages when installed normally — so without these the pipeline would
# write its warehouse next to the installed library and the COPY below would
# silently pick up an empty index.
ENV OER_PATH_DATA=/build/data \
    OER_PATH_FIXTURES=/build/fixtures \
    OER_PATH_OUTPUTS=/build/outputs

# Batch pipeline, then the serving-index build. Both run at image build time so
# the runtime image never executes the pipeline. Stages are listed individually
# rather than via `run-all` because the image needs resolution, not the
# analysis and evaluation reports.
RUN python -m ownership_er.cli normalize --fixtures \
 && python -m ownership_er.cli block \
 && python -m ownership_er.cli match --matcher rules \
 && python -m ownership_er.cli cluster \
 && python -m ownership_er.serve.index \
 && test -s /build/data/warehouse/serving.duckdb

# ---------------------------------------------------------------- runtime
FROM python:3.11-slim AS runtime

# Non-root: the process only ever reads, so it has no reason to own anything.
RUN useradd --create-home --shell /usr/sbin/nologin app
WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OER_SERVING_INDEX=/app/data/warehouse/serving.duckdb

COPY pyproject.toml README.md ./
COPY src/ ./src/
# The API needs the package plus a web server; the pipeline's heavier optional
# extras (splink, neo4j, anthropic) are deliberately absent from the runtime.
RUN pip install --no-cache-dir . "fastapi>=0.110" "uvicorn[standard]>=0.29" \
 && rm -rf /root/.cache

COPY --from=builder /build/data/warehouse/serving.duckdb /app/data/warehouse/serving.duckdb

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4).status==200 else 1)"

# One worker: the index is read-only and the workload is CPU-bound inside
# DuckDB, so extra workers would each map the same file and compete for the
# same cores. Horizontal scaling is a second container, not a second worker.
CMD ["uvicorn", "ownership_er.serve.api:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--log-level", "info", "--access-log"]
