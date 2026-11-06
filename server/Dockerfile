FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py apcore.py gateway.py capture.py ./
ENV DB_PATH=/data/ap.db
VOLUME /data
EXPOSE 8000 20332
# По умолчанию HTTP-сервер. Один процесс: запись в SQLite сериализуется внутри приложения.
# Шлюз для трекеров запускается отдельным сервисом из этого же образа (см. docker-compose.yml).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
