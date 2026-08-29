FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# job_alerts.db is created at runtime and lives in a mounted volume (see docker-compose.yml)
CMD ["python", "bot.py"]
