# macro_forecast_site — краткая карта для ИИ

Flask-сайт макропрогнозов (`app.py`, шаблоны `templates/`, данные — Excel/JSON в корне и `data/`), деплой —
Docker-образ в Yandex Serverless Containers через `deploy_yc.sh`.
URL: https://bba2agi0c621jbhicgod.containers.yandexcloud.net/ . Общий запуск/формат данных — `README_RU.md`;
ручной гайд для пользователя — `ИНСТРУКЦИЯ.md` (обновлять вместе с этим файлом).

## Страницы инфляции (`/inflation/weekly`, `/inflation/monthly`)
- `templates/inflation_weekly.html`, `templates/inflation_monthly.html` — самодостаточные страницы с данными
  в однострочных `const NAME = …;` внутри `{% extends "base.html" %}`.
- Источник данных и скрипты — проект `~/Desktop/Совкомбанк/Инфляция` (см. его `CLAUDE.md`):
  неделя — `update_site_weekly.sh` (переносит const из `inflation_dashboard.html`), месяц — точечные патчи
  `update_table1_and_charts.py`, `update_demand_supply_cbr.py`, `update_fx_sensitivity*.py`.
- Недельный шаблон — отдельная копия `dashboard_template.html`: правки разметки/JS делать в обоих.
  Месячный шаблон сайта содержит разделы, которых нет в локальном `dashboard_template_monthly.html` — не перегенерировать целиком.

## Выкладка
С 14.11.2026 все накопленные правки (рубль, НГД, Метрика, data_scripts, data/*.json) закоммичены в `main` —
рабочая папка и коммит совпадают. Точка отката до этого коммита — ветка `before-2026-11-14`.
- Весь сайт: `./deploy_yc.sh` в этой папке (нужны Docker Desktop и `yc`). Коммитить/пушить/выкладывать — по просьбе пользователя.
- Только страницы инфляции: по-прежнему `~/Desktop/Совкомбанк/Инфляция/deploy_inflation_site.sh`.
- Пароль: вкладка «Макропрогнозы» (`/` и её API, см. `PUBLIC_PATHS` в `app.py`) открыта всем, остальное — Basic Auth.
  Логин/пароль — в `~/.config/macro/site_auth.env` (вне репозитория, не читать и не печатать); `deploy_yc.sh` без этого
  файла останавливается, `--no-auth` выкладывает без пароля.
- План дальнейших работ (вынос данных в Object Storage, автовыкладка из GitHub) — документ «План: порядок в проекте
  и вынос данных из сайта» в Claude.

## Прочее
- `models/taylor_rule` — модель «Модели ЦБ» → `data/cbr_models.json` (см. README_RU.md).
- `data_scripts/`, `data/*.json` (рубль, НГД и т.п.) — другие разделы; обновляются `data_scripts/update_ruble_tab.sh`.
