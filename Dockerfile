# ---- frontend build ----
FROM node:26-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- runtime ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 SG_DATA_DIR=/data SG_STATIC_DIR=/app/static
WORKDIR /app
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin sg && mkdir -p /data && chown sg:sg /data
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY entrypoint.sh /app/entrypoint.sh
COPY --from=ui /ui/dist ./static
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1
CMD ["/app/entrypoint.sh"]
