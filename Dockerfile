FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY entrypoint.sh banner.py ./
RUN chmod +x /app/entrypoint.sh \
    && useradd --uid 10001 --user-group --no-create-home \
       --home-dir /app --shell /usr/sbin/nologin linkpilot \
    && mkdir -p /data \
    && chown -R linkpilot:linkpilot /data \
    && python -m compileall -q /app/app

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000
EXPOSE 8001
USER 10001:10001

ENTRYPOINT ["/app/entrypoint.sh"]
