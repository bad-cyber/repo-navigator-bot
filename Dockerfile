FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# Системные зависимости для компиляции/работы библиотек при необходимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Устанавливаем лёгкий CPU-вариант PyTorch перед sentence-transformers
RUN pip install --upgrade pip && \
    pip install torch --index-url https: / / download.pytorch.org / whl / cpu && \
    pip install -r requirements.txt

COPY src/ ./src/
COPY tests/ ./tests/

CMD ["python", "-m", "src.main"]
