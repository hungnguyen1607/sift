FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SIFT_MODEL_CACHE=/models HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /app

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml README.md LICENSE ./
RUN mkdir sift && touch sift/__init__.py && pip install . && pip uninstall -y sift

# Bake the embedding model into the image: no download at startup, works offline.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/models')"

COPY sift ./sift
RUN pip install --no-deps . && useradd --system --uid 10001 sift && chown -R sift /models
USER sift

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)"
CMD ["uvicorn", "sift.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
