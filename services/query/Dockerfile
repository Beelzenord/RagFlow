FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY services/shared /opt/shared
RUN pip install /opt/shared

COPY services/query/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY services/query/app /app/app

EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
