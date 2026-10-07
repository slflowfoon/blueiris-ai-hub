FROM python:3.14-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser && \
    install -d -o appuser -g appuser /app /app/data /app/logs /tmp_images && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# pip is only needed to install dependencies. Remove it so its bundled copies of
# msgpack/urllib3/setuptools (pip/_vendor/vendor.txt) are not shipped or scanned.
RUN pip install --no-cache-dir -r requirements.txt && pip uninstall -y pip

COPY --chown=appuser:appuser app /app
COPY --chown=appuser:appuser android-tv-overlay/dist/android-tv-overlay-debug.apk /app/tv-overlay/android-tv-overlay-debug.apk
ARG APP_VERSION=dev
RUN echo "$APP_VERSION" > /app/VERSION

USER appuser

CMD ["python3"]
