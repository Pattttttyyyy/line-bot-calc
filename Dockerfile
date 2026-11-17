FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD exec gunicorn --preload --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app
