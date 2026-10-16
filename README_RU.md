# Сайт макропрогнозов (Flask)

## Как поднять сайт без Яндекс Облака

Сайт — обычное Flask-приложение в Docker, к Яндексу не привязан (с ним связаны только `deploy_yc.sh`
и `serverless-containers.yaml`). Всё нужное лежит в этом репозитории: код, шаблоны и Excel-файлы с данными.

### Вариант 1: Docker (любой сервер или хостинг контейнеров)
```bash
git clone https://github.com/andreynes/macroforecasts.git
cd macroforecasts
docker build -t macro-site .
docker run -d -p 80:5000 \
  -e DATA_FILE=/app/macro_main_linked.xlsx \
  -e REQUIRE_BASIC_AUTH=0 \
  --name macro-site --restart unless-stopped macro-site
```
Сайт откроется на `http://<адрес сервера>/`. Подойдёт любой VPS с Docker или облако, которое запускает контейнер
по Dockerfile (Render, Railway, Fly.io, Cloud Run, другие serverless-контейнеры). Порт берётся из переменной `PORT`
(по умолчанию 5000).

### Вариант 2: без Docker
```bash
python3.10 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DATA_FILE=macro_main_linked.xlsx gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:8000
```

### Переменные окружения
| Переменная | Зачем | По умолчанию |
|---|---|---|
| `DATA_FILE` | основной Excel с прогнозами | `macro_main_linked.xlsx` в корне |
| `REQUIRE_BASIC_AUTH` | `1` — вход по логину и паролю | `0` |
| `BASIC_AUTH_USER`, `BASIC_AUTH_PASS` | логин и пароль, если вход включён | `user` / `pass` |
| `PORT` | порт внутри контейнера | `5000` |

Остальные Excel (`yield_forecast_data.xlsx`, `analyst_forecasts.xlsx`, файлы по денежному рынку, ОФЗ и т.д.)
читаются из корня проекта; пути можно переопределить переменными, см. `utils/*.py`.

### Данные вкладки «Модели ЦБ»
Сайт читает готовый `data/cbr_models.json`. Пересчитать его (код модели — в `models/taylor_rule`):
```bash
cd models/taylor_rule
pip install -r requirements.txt
python src/download_data.py   # свежие данные ЦБ и Росстата
python src/export_site.py     # пишет ../../data/cbr_models.json
```
Прогноз СКБ модель берёт из `macro_main_linked.xlsx` в корне сайта. PDF ЦБ в git не хранятся —
`download_data.py` скачивает их заново.

---

## Локальный запуск и формат данных (исходная инструкция)

Готовый мини‑сайт, который **автоматически подхватывает данные из вашего Excel/CSV**, показывает их в таблице, строит **графики по выбранному показателю** и позволяет **выгружать всё в Excel**.

## Быстрый старт

### 1) Установите зависимости
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2) Проверьте файл конфигурации `config.yaml`
- `data_file` — путь к вашему файлу (.xlsx или .csv). По умолчанию: `data/forecast_sample.xlsx`.
- `date_column` — столбец с датой/периодом (если есть).
- `excel_sheet` — (опционально) имя листа в Excel.
- `table_page_size` — сколько строк показывать в таблице.

### 3) Запустите сайт
```bash
python app.py
```
Откройте в браузере: http://127.0.0.1:5000

Сайт **читает ваш файл на каждый запрос**. То есть достаточно **заменить файл** на новый (или обновить его содержимое) — и страница покажет свежие данные **без перезапуска**.

## Формат данных

Минимально: один столбец с датой/периодом (например, `date`) и несколько числовых столбцов — это отдельные показатели, которые появятся в выпадающем списке «Графики показателей».

Пример в `data/forecast_sample.xlsx`:
- `date` — даты кварталов
- `GDP_growth_yoy`, `CPI_inflation_yoy`, `Unemployment_rate`, `Interest_rate` — числовые ряды

> Если ваш файл имеет другие названия колонок — просто скорректируйте `config.yaml`.

## Экспорт в Excel

Кнопка **«Экспорт в Excel»** формирует `macro_forecasts_export.xlsx` из текущих данных.

## Как развернуть на сервере

1. Установите Python (3.10+).
2. Скопируйте файлы проекта на сервер.
3. Поставьте зависимости `pip install -r requirements.txt`.
4. Настройте systemd‑сервис или используйте `gunicorn`:
   ```bash
   pip install gunicorn
   gunicorn -w 2 -b 0.0.0.0:8000 app:app
   ```
5. Поставьте Nginx как обратный прокси (опционально).

## Частые вопросы

**Можно ли читать несколько листов или файлов?**  
Базово — один файл. Если нужно больше, скажите — добавлю выбор источника и агрегацию.

**Поддерживаются ли месячные/квартальные/годовые данные?**  
Да. Даты парсятся автоматически. Если это просто периоды как строки — тоже ок, будут показаны как есть.

**Хотите загрузку файла через веб‑интерфейс?**  
Легко добавить. Сейчас чтение идёт автоматически из пути `data_file`.

---

Если нужно «допилить» структуру данных, KPI‑панель, несколько графиков на одной странице, тренды с прогнозом и т. д. — скажите, расширю.
