FROM python:3.9-slim

# Install system dependencies (FFmpeg is required for video conversion)
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app
ARG APP_VERSION=dev
RUN echo "$APP_VERSION" > /app/VERSION

CMD ["python3"]
