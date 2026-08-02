# Experientia API — AWS App Runner (ap-south-1)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app

RUN mkdir -p /app/uploads/pdf-exports \
    && useradd --create-home --uid 1001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# App Runner sets PORT. Prefer horizontal scale via ECS tasks.
# Keep max-task-count modest until PDF jobs use shared storage.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2 --proxy-headers --timeout-keep-alive 65"]


