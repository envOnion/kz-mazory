ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 mazory
COPY tokenizers/*manifest.json tokenizers/
COPY fetch_tokenizer.py .
RUN python fetch_tokenizer.py
COPY --chown=mazory:mazory . .
RUN mkdir -p /app/media /app/staticfiles /app/avatars && chown -R mazory:mazory /app/media /app/staticfiles /app/avatars
USER mazory
EXPOSE 8000
CMD ["gunicorn", "mazory_backend.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "30"]
