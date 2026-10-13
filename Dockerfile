# QIANLI Super Cam LC-AD11 - live view relay
# Self-contained: auto-discovers the camera and serves the web GUI.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg iproute2 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV CAMERA_HOST=auto \
    PORT=8099 \
    CALIBRATION_PATH=/data/calibration.json
VOLUME ["/data"]
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health',timeout=4).status==200 else 1)"

CMD ["python", "relay.py"]
