FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .
# Build the sample database at image build time (it is also auto-created at start).
RUN python -m app.db.seed

EXPOSE 8000
# One worker keeps the in-memory conversation store consistent; threads give concurrency.
# Hosting platforms (Render, Railway, Fly.io...) pass the port in $PORT; 8000 locally.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 8 --timeout 120 run:app"]
