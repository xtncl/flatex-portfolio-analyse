FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY portfolio_analyse.py server.py ./

VOLUME ["/app/data"]

ENV DB_PATH=/app/data/portfolio.db \
    CACHE_DIR=/app/data/cache \
    PORT=8080

EXPOSE 8080

CMD ["python3", "server.py"]
