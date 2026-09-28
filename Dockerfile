# Movie Discovery Agent - web UI (chat + monitoring dashboard)
# Secrets are NOT baked in: pass OPENAI_API_KEY / ANTHROPIC_API_KEY at run time (docker compose reads .env).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# CPU-only torch first (the default wheel pulls ~2 GB of CUDA libraries); only the local
# bge-small / cross-encoder fallbacks need it.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ src/
COPY app/ app/
COPY scripts/ scripts/
COPY eval/ eval/
COPY data/ data/
# tuned blend weights and eval results the app reads at start-up
COPY outputs/eval/ outputs/eval/

RUN useradd --create-home appuser && mkdir -p logs artifacts data_store .cache && chown -R appuser /app
USER appuser

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/api/health')" || exit 1

# server.py starts warming the models in a background thread before the first visitor arrives
CMD ["python", "app/server.py"]
