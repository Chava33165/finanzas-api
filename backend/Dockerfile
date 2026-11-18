FROM python:3.12-slim
# pdftotext (poppler-utils) para leer los recibos de nomina de Plexus.
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
# Mismo usuario que la imagen vieja (dueño de /data/tasks.db en el volumen task_data).
RUN useradd --uid 1000 --create-home appuser
USER appuser
ENV TZ=America/Mexico_City
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
