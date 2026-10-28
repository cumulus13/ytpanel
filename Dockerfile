FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 1000 app && mkdir -p /downloads && chown -R app /data /app /downloads
USER app
ENV YTP_DOWNLOAD_ROOT=/downloads
# `deno` (pip extra) installs its binary under the user's home; yt-dlp finds it on PATH
ENV PATH="/home/app/.local/bin:${PATH}"
EXPOSE 8000
CMD ["gunicorn", "ytpanel.wsgi", "-b", "0.0.0.0:8000", "--workers", "3"]
