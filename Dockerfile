# syntax=docker/dockerfile:1

# ---- terminal component (xterm.js), bundled so the app works with no internet access
FROM node:22-alpine AS vendor
WORKDIR /v
RUN npm install --no-audit --no-fund --omit=dev @xterm/xterm@5.5.0 @xterm/addon-fit@0.10.0

# ---- app
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ND_DATA_DIR=/data \
    USER=netdevices \
    LOGNAME=netdevices \
    HOME=/tmp \
    ND_SSH_KEY_DIR=/ssh
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY client ./client
COPY --from=vendor /v/node_modules/@xterm/xterm/lib/xterm.js \
                   /v/node_modules/@xterm/xterm/css/xterm.css \
                   /v/node_modules/@xterm/addon-fit/lib/addon-fit.js \
                   ./static/vendor/
RUN mkdir -p /data /ssh && chmod 0777 /data
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--log-level", "warning"]
