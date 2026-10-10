# API-only image for the vendored daily_stock_analysis sidecar.
# DSA's own Dockerfile also builds apps/dsa-web, which this product does not ship.
FROM python:3.11-slim-bookworm

ENV TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=8000 \
    DATABASE_PATH=/app/data/stock_analysis.db \
    LOG_DIR=/app/logs \
    HOME=/home/dsa \
    XDG_CACHE_HOME=/home/dsa/.cache \
    XDG_CONFIG_HOME=/home/dsa/.config \
    MPLCONFIGDIR=/home/dsa/.config/matplotlib \
    PYTHONPYCACHEPREFIX=/home/dsa/.cache/pyc \
    TMPDIR=/tmp

# wkhtmltopdf 的 Debian 包同时提供 wkhtmltoimage。分享图默认走这个引擎。
# fonts-noto-cjk 提供 "Noto Sans CJK SC"，与分享图 HTML 的中文字体栈一致。
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get update && apt-get install -y --no-install-recommends \
        gcc curl wkhtmltopdf fontconfig fonts-noto-cjk \
        libjpeg62-turbo libxrender1 libxext6 \
    && fc-cache -f \
    && wkhtmltoimage --version \
    && wkhtmltopdf --version \
    && fc-list :lang=zh family | grep -q "Noto Sans CJK SC" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# efinance 在 import 时创建包内 data/，并把 search-cache-v2.json 写进去。
# 目录默认属主是 root。运行 uid 由 compose 的 APP_UID 决定，构建时不能 chown 给固定用户。
RUN python -c "import pathlib; from efinance.config import DATA_DIR; p = pathlib.Path(DATA_DIR).resolve(); p.mkdir(parents=True, exist_ok=True); p.chmod(0o1777)"
COPY . .
# /root 在基础镜像里是 0700。日志和报告不挂到宿主机，避免再写出 root 属主文件。
# /app/config 要可写，DSA 才能在 .env 旁边创建 .env.tmp，再退回原地改写挂载文件。
RUN mkdir -p /home/dsa/.cache/pyc /home/dsa/.config/matplotlib \
        /app/data /app/logs /app/reports /app/config \
        /var/cache/fontconfig /tmp \
    && chmod 1777 /home/dsa /home/dsa/.cache /home/dsa/.cache/pyc \
        /home/dsa/.config /home/dsa/.config/matplotlib \
        /app/data /app/logs /app/reports /app/config \
        /var/cache/fontconfig /tmp \
    && chmod -R a+rwX /var/cache/fontconfig

EXPOSE 8000
CMD ["python", "main.py", "--serve-only", "--host", "0.0.0.0", "--port", "8000"]
