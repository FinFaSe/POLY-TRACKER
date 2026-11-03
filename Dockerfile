FROM python:3.11-slim

# Logs inmediatos en 'docker compose logs' y sin archivos .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Base de datos persistente: monta aquí un volumen (ver docker-compose.yml).
VOLUME ["/app/data"]
ENV DB_PATH=/app/data/copybot.db

# Modo continuo: revisa a los traders cada POLL_INTERVAL_SECONDS hasta que se detenga.
CMD ["python", "main.py"]
