FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    JELLYDUPE_CONFIG=/config

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
VOLUME ["/config"]
EXPOSE 8095

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095"]
