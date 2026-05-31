FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY src/ ./src/

RUN mkdir -p /data/files

VOLUME ["/data"]
EXPOSE 8000

ENV PYTHONPATH=/app
ENV DOWNLOAD_DIR=/data/files
ENV STATE_FILE=/data/.state.json

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
