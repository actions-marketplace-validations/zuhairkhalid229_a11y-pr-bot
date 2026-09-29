FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run unprivileged; Cloud Run does not require root and this keeps the image
# usable in stricter environments later.
RUN useradd --create-home --uid 1000 appuser
USER appuser

# Cloud Run injects PORT and it is not always 8080. Honour it.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1
