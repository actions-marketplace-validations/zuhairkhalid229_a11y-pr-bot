# Build from the repo root so both `app/` (shared logging) and `worker/` are
# in context:
#     docker build -f worker/Dockerfile -t a11y-worker .
#
# The official Playwright image ships Chromium and every apt dependency it
# needs (fonts, libnss, libgbm, ...). Installing those by hand on python:slim
# is ~40 packages and breaks on each Chromium bump. The tag MUST match the
# playwright version in worker/requirements.txt -- a mismatch fails at launch
# with "Executable doesn't exist", not at build.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY worker/requirements.txt ./worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

# The worker shares GitHub auth, Firestore access and settings with the api.
COPY app ./app
COPY worker ./worker

# The image's pwuser already exists with the browsers installed under its
# home; stay as that user rather than creating another.
USER pwuser

ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn worker.main:app --host 0.0.0.0 --port ${PORT} --workers 1
