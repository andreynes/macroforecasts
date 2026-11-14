# taylor_rule — памятка для ИИ

Репликация правила Тейлора ЦБ (КПМ ДДКП/ДИП) + калибровка по прогнозам ЦБ → вкладка «Модели ЦБ» сайта.
Методика подробно — `README.md`; ручной гайд пользователя — `ИНСТРУКЦИЯ.md` (обновлять вместе с этим файлом).
Python: `/Users/andrejkrylov/anaconda3/bin/python3`.

- `src/download_data.py` — ключевая ставка, ИПЦ (ЦБ), ВВП (Росстат), PDF среднесрочных прогнозов ЦБ, разрыв выпуска ЦБ → `data/raw/`.
- `src/export_site.py` — калибровка + бэктест → `../../data/cbr_models.json` (переменная `CBR_SITE_JSON`).
- Наш прогноз — лист `main` в `../../macro_main_linked.xlsx`: после каждого `data_scripts/update_macro.py` ОБЯЗАТЕЛЬНО
  перезапускать export_site.py (однажды забыли — на сайте висел старый прогноз СКБ).
- Ручные допущения в `src/taylor_rule.py`: `EXTRA_DEC_FORECASTS`, `GDP_YOY_PRELIM`, `GDP_YOY_FORECAST`, `NEUTRAL_NOMINAL`.
- Вход вне `data/raw`: `data/ИПЦ и БИПЦ.xls(x)` (ЕМИСС; `.xls` режется на 256 столбцах — нужен `.xlsx`).
- При новых «Основных направлениях ДКП» — `PDF_URL`/`PAGE`/`FIRST_Q` в `src/extract_cbr_gap.py`.
- Папка `models/` исключена из Docker-образа (`.dockerignore`) — на сайт идёт только JSON.
