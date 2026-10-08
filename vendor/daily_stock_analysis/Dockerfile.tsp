# API-only image for the vendored daily_stock_analysis sidecar.
# DSA's own Dockerfile also builds apps/dsa-web, which this product does not ship.
FROM python:3.11-slim-bookworm

ENV TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=8000 \
    DATABASE_PATH=/app/data/stock_analysis.db

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get update && apt-get install -y --no-install-recommends \
        gcc curl wkhtmltopdf fontconfig fonts-noto-cjk \
        libjpeg62-turbo libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
CMD ["python", "main.py", "--serve-only", "--host", "0.0.0.0", "--port", "8000"]
