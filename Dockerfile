FROM python:3.12-slim

WORKDIR /app

# System-Abhängigkeiten für matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY portfolio_analyse.py portfolio_app.py ./
COPY .streamlit/ .streamlit/

# Persistente Daten (SQLite + Kurs-Cache) als Volume einbinden
VOLUME ["/app/data"]

ENV DB_PATH=/app/data/portfolio.db \
    CACHE_DIR=/app/data/cache \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8501

CMD ["streamlit", "run", "portfolio_app.py", "--server.address=0.0.0.0"]
