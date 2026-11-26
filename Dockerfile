# Reproducible build (optional): pin the base images by digest and install Python deps from the hash-locked file.
#   ./tools/strict-build.sh        (see docs/reproducible-build.md). Plain `docker build .` behaves as before.
ARG NODE_IMAGE=node:22-alpine
ARG PYTHON_IMAGE=python:3.12-slim
ARG REQUIREMENTS=requirements.txt

# ---- frontend build ----
FROM ${NODE_IMAGE} AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- runtime ----
FROM ${PYTHON_IMAGE}
ARG REQUIREMENTS
ENV PYTHONUNBUFFERED=1 SG_DATA_DIR=/data SG_STATIC_DIR=/app/static
WORKDIR /app
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin sg && mkdir -p /data && chown sg:sg /data
COPY backend/requirements.txt backend/requirements.lock ./
# requirements.lock carries hashes, so pip refuses anything that does not match; pip itself is only upgraded in the default build
RUN if [ "$REQUIREMENTS" = requirements.txt ]; then pip install --no-cache-dir --upgrade pip; fi && pip install --no-cache-dir -r "$REQUIREMENTS"
COPY backend/app ./app
COPY entrypoint.sh /app/entrypoint.sh
COPY --from=ui /ui/dist ./static
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1
CMD ["/app/entrypoint.sh"]
