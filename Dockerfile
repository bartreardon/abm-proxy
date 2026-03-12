FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static/ ./static/

# Cache directory – mount a volume here for persistence
RUN mkdir -p /data/cache/devices
ENV CACHE_DIR=/data/cache
ENV PORT=5050

EXPOSE $PORT

CMD sh -c "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 60 server:app"
