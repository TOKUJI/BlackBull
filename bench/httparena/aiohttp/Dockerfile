# aiohttp-on-HttpArena container.
#
# aiohttp supports HTTP/1.1 + WebSocket (no HTTP/2, no gRPC).
# Profiles: baseline (HTTP/1.1 cleartext on :8080).
# Multi-process via gunicorn + aiohttp.GunicornWebWorker (launcher.py).

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install aiohttp + gunicorn from requirements.txt.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

VOLUME /results
COPY app.py launcher.py ./
EXPOSE 8080

CMD ["python", "launcher.py"]
