FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000 \
    DB_PATH=/app/data/leaderboard.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates/ ./templates/
RUN mkdir -p /app/data

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"

# Shell form so $PORT is honored if overridden at run time; exec keeps signal handling clean
CMD exec gunicorn --bind "0.0.0.0:${PORT:-5000}" --workers 2 --threads 4 --timeout 60 app:app
