FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/openbb \
    OPENBB_API_HOST=0.0.0.0 \
    OPENBB_API_PORT=6900

WORKDIR /opt/tsp-openbb
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt ./
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt
COPY tsp_yahoo ./tsp_yahoo
COPY sidecar.py ./
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" ./tsp_yahoo \
    && useradd --create-home --uid 1000 openbb \
    && chown -R openbb:openbb /opt/tsp-openbb /home/openbb
USER openbb
EXPOSE 6900
# openbb-api 默认只挂载显式安装的 FRED 与 tsp_yahoo 扩展。
CMD ["openbb-api", "--app", "sidecar:app", "--host", "0.0.0.0", "--port", "6900"]
