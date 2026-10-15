# Базовый образ с Python 3.10 и готовыми библиотеками для pandas/numpy
FROM python:3.10-slim

# Устанавливаем системные зависимости для pandas, numpy и openpyxl
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Копируем только requirements.txt для кеширования зависимостей
COPY requirements.txt .

# Устанавливаем зависимости Python
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем весь проект в контейнер
COPY . .

# Открываем порт
EXPOSE 5000

# Запуск приложения через gunicorn
CMD ["sh", "-c", "gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-5000}"]


