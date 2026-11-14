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
- Пароль: вкладка «Макропрогнозы» (`/` и её API, см. `PUBLIC_PATHS` в `app.py`) открыта всем, остальное — после входа через форму `/login`
  (`site_auth.py`, cookie на 30 дней; Basic Auth в Yandex Serverless Containers не работает — платформа забирает заголовок Authorization).
  Логин/пароль — в `~/.config/macro/site_auth.env` (вне репозитория, не читать и не печатать); `deploy_yc.sh` без этого
  файла останавливается, `--no-auth` выкладывает без пароля.
- План дальнейших работ (вынос данных в Object Storage, автовыкладка из GitHub) — документ «План: порядок в проекте
  и вынос данных из сайта» в Claude.

## Данные в Object Storage (шаг 2 «данные отдельно от сайта», этап A — 14.11.2026)
- Приватный бакет `macro-site-data` (версионирование включено), смонтирован в контейнер только на чтение:
  `--mount type=object-storage,mount-point=/mnt/site-data,bucket=macro-site-data,mode=ro` в `deploy_yc.sh`;
  у `macro-sa` роль `storage.viewer` на каталог.
- `app.py`: `DATA_DIR=/mnt/site-data/data` → `_data_file(name)` берёт файл из бакета, если его нет — `data/` из образа.
  Так читаются 8 JSON: ruble_fx, cbr_fx_ops, cny_volumes, exporters_need, futoi_households, oil_exports,
  ngd_dashboard, cbr_models. Кеша у них нет — файл читается при каждом запросе.
- Ручная загрузка: `yc storage s3 cp data/X.json s3://macro-site-data/data/X.json`. Скрипты пока пишут в `data/`
  (команда публикации — этап C). Дальше: Excel (B), publish.sh + manifest (C), инфляция (D).

## Прочее
- `models/taylor_rule` — модель «Модели ЦБ» → `data/cbr_models.json` (см. README_RU.md).
- `data_scripts/`, `data/*.json` (рубль, НГД и т.п.) — другие разделы; обновляются `data_scripts/update_ruble_tab.sh`.

## Данные остальных вкладок (Python: `/Users/andrejkrylov/anaconda3/bin/python3`)
| Вкладка | Данные | Кто пишет |
|---|---|---|
| Макропрогнозы `/` | лист `main` в `macro_main_linked.xlsx` | `data_scripts/update_macro.py` из листов пользователя (cpi, gdp, key_rate, usdrub, eurrub, cnyrub, FX add) — их НЕ перезаписывать |
| Прогнозы аналитиков | `analyst_forecasts.xlsx` | выгрузка Google Sheet `16sNSTaFOL3SmIlTUMGBWGUztAmlizwdsm8-FEAU1p6g/export?format=xlsx` |
| Модели ЦБ `/cbr-models` | `data/cbr_models.json` | `models/taylor_rule/src/export_site.py` — запускать ПОСЛЕ каждого update_macro.py |
| НГД `/ngd` | `data/ngd_dashboard.json` | `~/Desktop/Совкомбанк/НГД/ngd_model.py` |
| Рубль `/ruble` | константы RUB_* в `templates/ruble.html` + `data/ruble_fx.json`, `futoi_households.json`, `cny_volumes.json`, `exporters_need.json`, `oil_exports.json`, `cbr_fx_ops.json` | конвейер ниже |

## Вкладка «Рубль» — конвейер `bash data_scripts/update_ruble_tab.sh [--deploy]`
1. `update_fx.py --model RMM` → `ruble_fx.json`: факт ЦБ; текущий месяц — MTD, если прошло >10 дней (`MTD_MIN_DAYS`);
   прогноз — RMM лист RUB стр. 273 (EUR/CNY через BoP_monthly).
2. `update_ruble_data.py --model RMM --orfr <сводный ОРФР>` → вшивает RUB_* в ruble.html. Снимок RUB_SNAPSHOT
   (28.10.2026) меняется ТОЛЬКО с `--new-snapshot` по прямой просьбе.
3. `update_futoi.py`, 4. `update_cny_volumes.py` — ALGOPACK (кэш `data/cache/`).
5–6. `~/Desktop/Совкомбанк/Рубль/Модель экспортеров/need_model.py`, `oil_exports.py`.
7. `update_cbr_fx_ops.py` → `cbr_fx_ops.json` (факт ЦБ flikvid; прогноз БП — `forecast.bp_ops_forecast`; текущий месяц —
   Urals/Brent/курс MTD; `FNB_INVEST`). 8. проверка NaN.
Новый ОРФР ЦБ (~10-го числа, `~/Downloads/ORFR_ГГГГ-М.xlsx`): `update_orfr.py <файл>` → шаг 2.
14.11.2026 пользователь сказал из «рублёвой» сессии «пока ничего не выкладывать» — `--deploy` только по явной просьбе.

## Правила для данных
- В JSON не должно быть NaN (JSON.parse падает → пустой график). Проверка встроена в update_ruble_tab.sh.
- Токен ALGOPACK: `~/.config/moex/algopack_token` (chmod 600). Не печатать; в curl — через stdin (`-H @-`).
  apim.moex.com, tbank.ru — системным curl (российский УЦ); TLS-проверку не отключать.
- Подписи под заголовками графиков ≤ 5 строк; на графиках «Рубля» кнопка «Скачать PNG» (`addPngButtons`).
- Локально `python app.py` слушает порт 5050 (переменная `PORT`).
