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
- Код сайта: `./release_all.sh "сообщение"` (коммит, push, `deploy_yc.sh`, проверка вкладок) — по просьбе пользователя.
  Данные — не выкладкой, а `./publish.sh` (см. ниже).
- Только страницы инфляции: по-прежнему `~/Desktop/Совкомбанк/Инфляция/deploy_inflation_site.sh`.
- Пароль: вкладка «Макропрогнозы» (`/` и её API, см. `PUBLIC_PATHS` в `app.py`) открыта всем, остальное — после входа через форму `/login`
  (`site_auth.py`, cookie на 30 дней; Basic Auth в Yandex Serverless Containers не работает — платформа забирает заголовок Authorization).
  Логин/пароль — в `~/.config/macro/site_auth.env` (вне репозитория, не читать и не печатать); `deploy_yc.sh` без этого
  файла останавливается, `--no-auth` выкладывает без пароля.
- План дальнейших работ (вынос данных в Object Storage, автовыкладка из GitHub) — документ «План: порядок в проекте
  и вынос данных из сайта» в Claude.

## Данные в Object Storage (шаг 2 «данные отдельно от сайта»: этапы A–C — 14–15.11.2026)
- Приватный бакет `macro-site-data` (версионирование), смонтирован в контейнер только на чтение:
  `--mount type=object-storage,mount-point=/mnt/site-data,bucket=macro-site-data,mode=ro` в `deploy_yc.sh`.
  Сервисные аккаунты: `macro-sa` (сайт) — `storage.viewer`, `macro-publisher` (запись) — `storage.uploader`, обе роли
  на каталог (yc 0.159 не умеет роли на бакет; в каталоге один бакет — при появлении второго сузить обе роли).
- В бакете: `data/*.json` (9 файлов: ruble_fx, ruble_page, cbr_fx_ops, cny_volumes, exporters_need, futoi_households,
  oil_exports, ngd_dashboard, cbr_models), `excel/*.xlsx` (5 книг), `data/manifest.json`.
- `app.py`: JSON — `_data_file(name)` (DATA_DIR=/mnt/site-data/data, иначе `data/` из образа); книги — переменные
  `DATA_FILE`, `ANALYST_FORECASTS_FILE`, `YIELD_FILE`, `IRS_FILE`, `CC_FILE`, нет файла — копия из образа с тем же именем
  (`_main_book()`, `af._analyst_file()`, `yf._cached_read_excel()`). Кеш книг по mtime на монтировании работает.
- Изменение в бакете доходит до сайта за 1–4 мин; пока расходится, экземпляры контейнера отвечают по-разному.
- ВЫКЛАДКА ОБРАЗА ДАННЫЕ НЕ ОБНОВЛЯЕТ. Данные на сайт — только `./publish.sh <раздел>` (ruble, ngd, cbr_models, macro,
  analysts, yield), откат — `./publish.sh --rollback <раздел>`. Логика — `data_scripts/publish.py`: проверка (JSON без
  NaN/Infinity, Excel с нужными листами; хоть один плох — ничего не грузится) → загрузка изменённых (SHA-256 против
  манифеста) под ключом macro-publisher → manifest.json (по файлу: published_at, as_of, sha256, version_id; по разделу —
  история 10 публикаций, первая — `baseline` того, что уже лежало) → вход /login и ожидание sha в `/diag/data`
  (3 совпадения подряд, до PUBLISH_WAIT=420 с) → страницы раздела 200. Откат берёт версию из истории (yc 0.159 не умеет
  list-object-versions — поэтому version_id хранятся в манифесте).
- Пути publish.sh — из окружения: SITE_DIR, SITE_URL, DATA_BUCKET, PUBLISHER_KEY (`~/.config/macro/publisher-key.json`),
  YC_PUBLISHER_HOME (`~/.config/macro/yc-publisher` — отдельный HOME для yc, настройки пользователя не трогаются),
  SITE_USER/SITE_PASS или SITE_AUTH_FILE, PY, YC. Ключ не печатать.
- `/diag/data` (под входом): для каждого файла — источник (бакет/образ), размер, mtime, sha256.
- «Обновлено» на главной — `published_at` книги macro_main_linked.xlsx из манифеста (МСК), нет манифеста — пусто.
- `/export` (открыт всем) — новая книга только с листом `main` (значения, без других листов/связей/путей), имя прежнее,
  кеш по (путь, mtime) — `_export_main_only()`.
- Дальше — этап D (инфляция: /api/inflation/*), этап E (GitHub Actions + Lockbox) — только после подтверждения.

## Прочее
- `models/taylor_rule` — модель «Модели ЦБ» → `data/cbr_models.json` (см. README_RU.md).
- `data_scripts/`, `data/*.json` (рубль, НГД и т.п.) — другие разделы; обновляются `data_scripts/update_ruble_tab.sh`, на сайт — `publish.sh`.

## Данные остальных вкладок (Python: `/Users/andrejkrylov/anaconda3/bin/python3`)
| Вкладка | Данные | Кто пишет |
|---|---|---|
| Макропрогнозы `/` | лист `main` в `macro_main_linked.xlsx` | `data_scripts/update_macro.py` из листов пользователя (cpi, gdp, key_rate, usdrub, eurrub, cnyrub, FX add) — их НЕ перезаписывать |
| Прогнозы аналитиков | `analyst_forecasts.xlsx` | выгрузка Google Sheet `16sNSTaFOL3SmIlTUMGBWGUztAmlizwdsm8-FEAU1p6g/export?format=xlsx` |
| Модели ЦБ `/cbr-models` | `data/cbr_models.json` | `models/taylor_rule/src/export_site.py` — запускать ПОСЛЕ каждого update_macro.py |
| НГД `/ngd` | `data/ngd_dashboard.json` | `~/Desktop/Совкомбанк/НГД/ngd_model.py` |
| Рубль `/ruble` | `data/ruble_page.json` (декомпозиция, баланс потоков, пересмотр, снимок; `/api/ruble/page`) + `data/ruble_fx.json`, `futoi_households.json`, `cny_volumes.json`, `exporters_need.json`, `oil_exports.json`, `cbr_fx_ops.json` | конвейер ниже |

## Вкладка «Рубль» — конвейер `bash data_scripts/update_ruble_tab.sh [--publish]` (`--deploy` — старое имя того же)
1. `update_fx.py --model RMM` → `ruble_fx.json`: факт ЦБ; текущий месяц — MTD, если прошло >10 дней (`MTD_MIN_DAYS`);
   прогноз — RMM лист RUB стр. 273 (EUR/CNY через BoP_monthly).
2. `update_ruble_data.py --model RMM --orfr <сводный ОРФР>` → `data/ruble_page.json` (блоки RUB_*). Снимок RUB_SNAPSHOT
   (28.10.2026) меняется ТОЛЬКО с `--new-snapshot` по прямой просьбе.
3. `update_futoi.py`, 4. `update_cny_volumes.py` — ALGOPACK (кэш `data/cache/`).
5–6. `~/Desktop/Совкомбанк/Рубль/Модель экспортеров/need_model.py`, `oil_exports.py`.
7. `update_cbr_fx_ops.py` → `cbr_fx_ops.json` (факт ЦБ flikvid; прогноз БП — `forecast.bp_ops_forecast`; текущий месяц —
   Urals/Brent/курс MTD; `FNB_INVEST`). 8. проверка NaN. 9. (`--publish`) `./publish.sh ruble`.
Новый ОРФР ЦБ (~10-го числа, `~/Downloads/ORFR_ГГГГ-М.xlsx`): `update_orfr.py <файл>` → шаг 2.
14.11.2026 пользователь сказал из «рублёвой» сессии «пока ничего не выкладывать» — `--publish` (бывш. `--deploy`) только по явной просьбе.

## Правила для данных
- В JSON не должно быть NaN (JSON.parse падает → пустой график). Проверка — в update_ruble_tab.sh и publish.sh.
- Токен ALGOPACK: `~/.config/moex/algopack_token` (chmod 600). Не печатать; в curl — через stdin (`-H @-`).
  apim.moex.com, tbank.ru — системным curl (российский УЦ); TLS-проверку не отключать.
- Подписи под заголовками графиков ≤ 5 строк; на графиках «Рубля» кнопка «Скачать PNG» (`addPngButtons`).
- Локально `python app.py` слушает порт 5050 (переменная `PORT`).
