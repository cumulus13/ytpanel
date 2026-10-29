FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    wget \
    nano \
    net-tools \
    iputils-ping \
    fish \
    procps \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV YTP_DOWNLOAD_ROOT=/downloads
# `deno` (pip extra) installs its binary under the user's home; yt-dlp finds it on PATH
ENV PATH="/home/app/.local/bin:${PATH}"
COPY ytpanel/settings.py ytpanel/settings.py
COPY entrypoint.sh entrypoint.sh
RUN useradd -m -u 1000 app && mkdir -p staticfiles && mkdir -p /downloads && mkdir -p /data && chown -R app /data /app /downloads
USER app
EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
# CMD ["gunicorn", "ytpanel.wsgi", "-b", "0.0.0.0:8000", "--workers", "3"]
CMD ["web"]
