FROM python:3.12-slim

# ffmpeg нужен для MP3, склейки видео+аудио и сжатия
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Deno нужен yt-dlp для расшифровки ссылок YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Код держим не в /app: bothost монтирует туда свои тома, /app/data — постоянный
WORKDIR /usr/src/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Распознавание музыки (shazamio). Если не поставился или ломает aiogram — бот всё равно соберётся без него
RUN pip install --no-cache-dir shazamio \
    && python -c "import aiogram, yt_dlp, shazamio" \
    || (echo "shazamio отключён"; pip uninstall -y shazamio shazamio-core; pip install --no-cache-dir -r requirements.txt)

COPY bot.py .
COPY webapp ./webapp

ENV HOME=/root \
    DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
