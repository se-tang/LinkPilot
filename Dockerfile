FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PANEL_PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY entrypoint.sh banner.py ./
RUN chmod +x /app/entrypoint.sh && python -m compileall -q /app/app

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
