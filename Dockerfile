FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /data && chown 1000:1000 /data
COPY --chown=1000:1000 bot.py answer_helpers.py features.py feature_ui.py questions.txt ./
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/progress.sqlite3
USER 1000:1000
CMD ["python", "bot.py"]
