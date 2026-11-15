#!/usr/bin/env bash
# Публикация данных сайта в бакет Object Storage — без Docker и выкладки сайта.
#
#   ./publish.sh ruble                    # проверить, загрузить, обновить manifest.json, дождаться на сайте
#   ./publish.sh macro --as-of 2026-11-14 # дата данных вручную (если автоматическая не подходит)
#   ./publish.sh --rollback ruble         # вернуть раздел к предыдущей публикации
#   ./publish.sh --list                   # разделы: ruble, ngd, cbr_models, macro, analysts, yield,
#                                         #          inflation_weekly, inflation_monthly
#
# Всё описано в data_scripts/publish.py; пути и ключи — из переменных окружения (SITE_DIR, PUBLISHER_KEY,
# YC_PUBLISHER_HOME, SITE_AUTH_FILE или SITE_USER/SITE_PASS, PY), по умолчанию — как на рабочем компьютере.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export SITE_DIR="${SITE_DIR:-$HERE}"   # откуда брать файлы данных (по умолчанию — эта папка)
exec "${PY:-python3}" "$HERE/data_scripts/publish.py" "$@"
