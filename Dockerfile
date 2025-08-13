# Базовый образ с нужным Python
FROM python:3.10-slim

# Системные зависимости (достаточно для wheels pandas/numpy)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Ставим зависимости
ENV PIP_ONLY_BINARY=":all:"
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . /app

# (Опционально) отдаём robots.txt самим Flask (если добавляли маршрут в app.py)
# EXPOSE не обязателен для Serverless, но не мешает
EXPOSE 5000

# Старт сервиса
CMD ["gunicorn", "app:app", "--workers", "2", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:5000"]
