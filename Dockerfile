FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEMO_AUTOSEED=0 \
    BOOTSTRAP_PROD_SNAPSHOT=1 \
    PORT=8000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY replica/design/tokens.css ./replica/design/tokens.css
COPY scripts ./scripts

RUN mkdir -p /app/data/sessions

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
