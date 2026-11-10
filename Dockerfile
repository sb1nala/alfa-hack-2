FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home bot \
    && mkdir -p /app/storage /app/data \
    && chown -R bot:bot /app/storage
USER bot

CMD ["python", "-m", "app.main"]
