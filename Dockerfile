FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY db ./db
COPY scripts ./scripts
COPY evals ./evals

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
# PORT is injected by hosts such as Render. --proxy-headers makes request.client the real visitor IP
# (from X-Forwarded-For), which the per-client rate limit relies on; only the host's proxy can reach
# the container, so trusting forwarded headers from any address is safe here.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
