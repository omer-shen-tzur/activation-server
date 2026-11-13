# Image for the activation server (used by docker-compose.demo.yml).
# .env is never copied in (see .dockerignore): settings arrive as environment
# variables from compose.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# migrations/ is applied by the app on startup; scripts/ holds the simulator.
COPY app/ app/
COPY scripts/ scripts/
COPY migrations/ migrations/

RUN useradd --system --no-create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
CMD ["python", "-m", "app"]
