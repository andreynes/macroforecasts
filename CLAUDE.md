# macro_forecast_site — краткая карта для ИИ

Flask-сайт макропрогнозов (`app.py`, шаблоны `templates/`, данные — Excel/JSON в корне и `data/`), деплой —
Docker-образ в Yandex Serverless Containers через `deploy_yc.sh`.
URL: https://bba2agi0c621jbhicgod.containers.yandexcloud.net/ . Общий запуск/формат данных — `README_RU.md`;
ручной гайд для пользователя — `ИНСТРУКЦИЯ.md` (обновлять вместе с этим файлом).

## Страницы инфляции (`/inflation/weekly`, `/inflation/monthly`) — с 15.11.2026 данные в JSON
- Данные: `data/inflation_weekly.json` (20 блоков: ITOGO, MODEL_COMPARISON, … CATEGORY_WEEKLY) и
  `data/inflation_monthly.json` (15 блоков: LATEST, TABLE1, … DIFFUSION) + поле `data_as_of`; отдаются
  `/api/inflation/weekly|monthly` (под входом, не в PUBLIC_PATHS), на сайт — `publish.sh inflation_weekly|inflation_monthly`.
- Шаблоны: блоки — `let NAME;`, основной код `(function(){…})()` обёрнут в `initInflationPage()` и запускается после
  загрузки JSON; сбой (401/сеть) — `window.showDataLoadError()` из base.html (сообщение + «Войти заново»). Служебные
  константы кода (MONTHS_SHORT, RANGES, W/M в функциях, METRICS…) остаются в шаблонах.
- Скрипты — проект `~/Desktop/Совкомбанк/Инфляция` (см. его `CLAUDE.md`), общий модуль `site_data.py` (SITE_DIR):
  неделя — `tools/sync_site_data.py` (блоки верхнего уровня из `inflation_dashboard.html` → JSON), месяц —
  `update_table1_and_charts.py`, `update_demand_supply_cbr.py`, `update_fx_sensitivity*.py` (пишут блоки в JSON).
  По средам `run_add_week.sh` сам публикует неделю, если `add_week.py` без «🚩 АНОМАЛИЯ» (иначе — причина в add_week.log).
- Недельный шаблон сайта по разметке/JS — копия `dashboard_template.html` (кроме загрузки данных): правки делать в обоих.
  Месячный шаблон сайта содержит разделы, которых нет в локальном `dashboard_template_monthly.html` — не перегенерировать целиком.
- Платформа сама сжимает ответы gzip (/api/inflation/weekly: 948 КБ → 200 КБ) — сжатие в app.py не нужно.

## Выкладка
С 14.11.2026 все накопленные правки (рубль, НГД, Метрика, data_scripts, data/*.json) закоммичены в `main` —
рабочая папка и коммит совпадают. Точка отката до этого коммита — ветка `before-2026-11-14`.
- Код сайта: `./release_all.sh "сообщение"` (коммит, push, `deploy_yc.sh`, проверка вкладок) — по просьбе пользователя.
  Данные — не выкладкой, а `./publish.sh` (см. ниже).
- Только код страниц инфляции: `~/Desktop/Совкомбанк/Инфляция/deploy_inflation_site.sh` (данные — publish.sh).
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
- В бакете: `data/*.json` (11 файлов: ruble_fx, ruble_page, cbr_fx_ops, cny_volumes, exporters_need, futoi_households,
  oil_exports, ngd_dashboard, cbr_models, inflation_weekly, inflation_monthly), `excel/*.xlsx` (5 книг), `data/manifest.json`.
- Дата данных в manifest — явное поле `data_as_of` в JSON (пишут ngd_model.py, oil_exports.py, скрипты инфляции,
  с 22.11.2026 update_futoi.py и update_cny_volumes.py); нет поля — последняя дата в файле не позже сегодняшней
  (22.11.2026 так взялась дата из служебного поля `updated` — «данные по 22.11» у юаня без торгов).
  ПЛАН (после этапа 5): publish.py не берёт дату из служебных полей (updated, generated и т.п.).
- moex: update_futoi.py пишет только завершённые торговые дни (сегодня — после закрытия: будни 23:50, выходные 19:00 МСК
  + 15 мин); оба скрипта переписывают файл, только если изменились данные (иначе байт в байт прежний → «загружать нечего»).
- `app.py`: JSON — `_data_file(name)` (DATA_DIR=/mnt/site-data/data, иначе `data/` из образа); книги — переменные
  `DATA_FILE`, `ANALYST_FORECASTS_FILE`, `YIELD_FILE`, `IRS_FILE`, `CC_FILE`, нет файла — копия из образа с тем же именем
  (`_main_book()`, `af._analyst_file()`, `yf._cached_read_excel()`). Кеш книг по mtime на монтировании работает.
- Изменение в бакете доходит до сайта за 1–4 мин; пока расходится, экземпляры контейнера отвечают по-разному.
- Раздел `moex` (futoi_households.json, cny_volumes.json) с 17.11.2026 публикует ОБЛАЧНАЯ МАШИНА (таймер moex,
  пн–пт 19:30 МСК, inflation/tools/run_moex_vm.sh); раздел `ruble` их не содержит, update_ruble_tab.sh считает их
  на Mac только локально. Откат раздела трогает только файлы, которые сейчас в нём.
- С 20.11.2026 данных НЕТ ни в git, ни в образе (`.gitignore`, `.dockerignore`): `data/*.json` и 5 книг (macro_main_linked,
  analyst_forecasts, yield_forecast_data, «Денежный рынок – Ставки_исправлено_v2», «Условия кредитования ЦБ») — только бакет.
  На Mac/машине файлы лежат на диске (их пишут расчёты, читает publish.sh). Резервной копии из образа больше нет: нет
  файла в бакете — у вкладки ошибка. Неиспользуемые Excel в корне (PBC…, «Данные по физикам», Минфин…) оставлены.
- ВЫКЛАДКА ОБРАЗА ДАННЫЕ НЕ ОБНОВЛЯЕТ. Данные на сайт — только `./publish.sh <раздел>` (ruble, moex, ngd, cbr_models, macro,
  analysts, yield, inflation_weekly, inflation_monthly, credit), откат — `./publish.sh --rollback <раздел>`. Логика — `data_scripts/publish.py`: проверка (JSON без
  NaN/Infinity, Excel с нужными листами; хоть один плох — ничего не грузится) → загрузка изменённых (SHA-256 против
  манифеста) под ключом macro-publisher → manifest.json (по файлу: published_at, as_of, sha256, version_id; по разделу —
  история 10 публикаций, первая — `baseline` того, что уже лежало) → вход /login и ожидание sha в `/diag/data`
  (3 совпадения подряд, до PUBLISH_WAIT=420 с) → страницы раздела 200. Откат берёт версию из истории (yc 0.159 не умеет
  list-object-versions — поэтому version_id хранятся в манифесте).
- Пути publish.sh — из окружения: SITE_DIR, SITE_URL, DATA_BUCKET, PUBLISHER_KEY (`~/.config/macro/publisher-key.json`),
  YC_PUBLISHER_HOME (`~/.config/macro/yc-publisher` — отдельный HOME для yc, настройки пользователя не трогаются),
  SITE_USER/SITE_PASS или SITE_AUTH_FILE, PY, YC. Ключ не печатать.
- На облачной машине: `PUBLISH_AUTH=instance` (доступ от сервисного аккаунта машины, без PUBLISHER_KEY),
  `SITE_AUTH_LOCKBOX_ID=e6q09c2gll0osrvbi873` (пароль сайта из Lockbox); `--dry-run` — всё, кроме загрузки и manifest.
- `/diag/data` (под входом): для каждого файла — источник (бакет/образ), размер, mtime, sha256.
- `/status` (под входом, `site_status.py`, кеш 5 мин, в меню не выведена): светофор по разделам из manifest.json (расписания и пороги — `SCHEDULES`),
  кто опубликовал — поле `published_by` (пишет publish.py: `PUBLISHER_NAME`, иначе macro-calc при PUBLISH_AUTH=instance, иначе Mac),
  блок машины — папки parallel/macro-calc/ и backup/ смонтированного бакета (итог сверки — `compare.json` в папке расчёта, если есть).
- «Обновлено» на главной — `published_at` книги macro_main_linked.xlsx из манифеста (МСК), нет манифеста — пусто.
- `/export` (открыт всем) — новая книга только с листом `main` (значения, без других листов/связей/путей), имя прежнее,
  кеш по (путь, mtime) — `_export_main_only()`.
- Этап D (инфляция) — 15.11.2026. Дальше — этап E (GitHub Actions + Lockbox) — только после подтверждения.

## Облачная машина расчётов `macro-calc` (шаг 3 — перенос расчётов; с 15.11.2026, расчёты пока не запущены)
`ssh macro-calc` (84.252.129.65, пользователь macro, ключ `~/.ssh/macro_calc_ed25519`). Переносятся инфляция, графики
бюллетеней ЦБ, Мосбиржа (futoi, cny_volumes); остальное — на Mac. Секреты — Lockbox `macro-site-auth`, `algopack-token`
(читать по id). Подробно — CLAUDE.md папки «Инфляция», раздел «Облачная машина расчётов». Публикует всегда одна сторона.
Сайт на машине — одна копия `~/macroforecasts` (git, только чтение; `git pull --ff-only`; данные в ней не в git):
из неё публикуют moex и (после этапа 5) неделя, `SITE_DIR` в юнитах, `PUBLISHER_NAME=macro-calc` задан в юнитах явно.
`~/site_old` (бывшая мини-копия) — удалить после этапа 5. Сверка параллельного расчёта — `compare.json` рядом с результатом
(`compare_parallel.py --published --json`, повтор в чт 10:00, вручную `RECHECK_COMPARE=1`) — подробно в CLAUDE.md «Инфляции».

## Прочее
- Ветка `feature/gdp` — черновик раздела «ВВП и промышленность» (`/gdp`, `data_scripts/gdp/`, источники — README там);
  в main НЕ сливать без решения пользователя; перед `release_all.sh` убедиться, что текущая ветка — main.
- Вкладка «Кредит» (`/credit`, `/api/credit` ← `data/credit_money.json`, под входом; с 26.11.2026 в main) — расчёт в репо
  andreynes/credit (машина `~/credit`, Mac `~/Desktop/Совкомбанк/Кредит`, см. его CLAUDE.md), на сайт — `publish.sh credit`.
  Реальные ставки: основная — КС минус ожидания населения (inFOM); для сравнения — аналитики, предприятия, ИПЦ SAAR, ИПЦ г/г.
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
   (19.11.2026; дату задаёт `--today ГГГГ-ММ-ДД`) меняется ТОЛЬКО с `--new-snapshot` по прямой просьбе.
3. `update_futoi.py`, 4. `update_cny_volumes.py` — ALGOPACK (кэш `data/cache/`).
5–6. `~/Desktop/Совкомбанк/Рубль/Модель экспортеров/need_model.py`, `oil_exports.py`.
5а. `fx_sales_ratio.py` (там же) → `data/fx_sales_ratio.json` — график «Продажи валюты нефин. компаниями и доля от экспорта (лаг 1,5 мес.)» (столбцы S всего; линии: оценка доли продажи экспортерами вкл. посредников = exporters_share(), K_lag, доля 29; после экспортеров;
   `/api/ruble/fx_sales_ratio`, в разделе publish `ruble`).
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
