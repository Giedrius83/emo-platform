FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schemas ./schemas
COPY telemetry ./telemetry

EXPOSE 8000

CMD ["uvicorn", "telemetry.app:app", "--host", "0.0.0.0", "--port", "8000"]
