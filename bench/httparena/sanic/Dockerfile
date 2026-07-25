# Sanic-on-HttpArena container.
#
# Sanic supports HTTP/1.1 + WebSocket only (no HTTP/2, no gRPC).
# Profiles: baseline, json, json-tls, static, json-comp, upload,
# echo-ws, echo-ws-pipeline, pipelined, limited-conn, crud, async-db,
# api-4, api-16.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Sanic + asyncpg + redis from requirements.txt.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

VOLUME /results
COPY app.py launcher.py ./
EXPOSE 8080 8081 8443

CMD ["python", "launcher.py"]
