# Kronos 推理 sidecar。**不**参与主 app 镜像构建。
#
# 主应用零 torch；只有这个容器里有推理运行时。默认不拉起：
#   docker compose --profile kronos up --build
#
# 权重不 COPY 进镜像，运行时从宿主机只读挂到 /opt/kronos/weights。
# 构建源用官方 PyPI，需要加速时自行传 --build-arg PIP_INDEX_URL=<镜像>。
FROM python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/kronos

# CPU 版 torch：研究侧车默认跑 CPU 小规模推理，显卡是另拍的事。
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        "fastapi>=0.110" "uvicorn[standard]>=0.29" \
        "torch>=2.2" "numpy>=1.26"

COPY server.py /opt/kronos/server.py

RUN useradd -m -u 1000 kronos && mkdir -p /opt/kronos/weights && chown -R kronos:kronos /opt/kronos
USER 1000:1000

EXPOSE 8200
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8200"]
