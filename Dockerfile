# One image for the API (which also serves the web app), the scheduler and one-off CLI jobs.
ARG PYTHON_IMAGE=python:3.12-slim
ARG NODE_IMAGE=node:22-slim

# ---- 1. build the web app
FROM ${NODE_IMAGE} AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. runtime
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRIAGE_LOG_FORMAT=json
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend/ backend/
COPY --from=web /src/frontend/dist frontend/dist
# landing zone lives on a volume; create it owned by the app user so the volume inherits that
RUN useradd --system --uid 10001 --home /app triage && mkdir -p /app/data && chown -R triage /app/data
USER triage
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
ENTRYPOINT ["python", "-m", "triage"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
