FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY services/shared /opt/shared
RUN pip install /opt/shared

COPY services/query/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Preload the cross-encoder reranker so first request is hot (no cold download
# from HuggingFace). Override RERANKER_MODEL at build time via --build-arg
# RERANKER_MODEL=... if you want a different one.
ARG RERANKER_MODEL=BAAI/bge-reranker-v2-m3
ENV RERANKER_MODEL=${RERANKER_MODEL}
RUN python -c "from sentence_transformers import CrossEncoder; \
    CrossEncoder('${RERANKER_MODEL}', max_length=512)"

COPY services/query/app /app/app

EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
