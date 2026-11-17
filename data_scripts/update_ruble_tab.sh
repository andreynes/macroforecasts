#!/usr/bin/env bash
# Обновляет все данные вкладки «Рубль» по порядку и (с флагом --publish) публикует их на сайт.
#
#   bash data_scripts/update_ruble_tab.sh             # только пересчитать (сайт на компьютере)
#   bash data_scripts/update_ruble_tab.sh --publish   # пересчитать и опубликовать (./publish.sh ruble):
#                                                     # файлы уходят в бакет, Docker и выкладка не нужны
#   --deploy — прежнее название того же флага
#
# Шаги: курс (RMM) → декомпозиция и баланс потоков (RMM + ОРФР) → фьючерсы физлиц (ALGOPACK) →
# объёмы юаня (ALGOPACK) → потребность экспортеров → нефть и экспорт → операции ЦБ → проверка NaN → публикация.
# Ошибка одного шага не останавливает остальные; в конце печатается сводка.

PY="${PY:-/Users/andrejkrylov/anaconda3/bin/python3}"
SITE="/Users/andrejkrylov/Desktop/projectmacro/macro_forecast_site"
RUB="/Users/andrejkrylov/Desktop/Совкомбанк/Рубль"
EXP="$RUB/Модель экспортеров"
RMM="$RUB/Russia Mega Model.xlsx"
ORFR="$RUB/Операции на валютном рынке (ОРФР, сводно).xlsx"

PUBLISH=0
case "${1:-}" in --publish|--deploy) PUBLISH=1 ;; esac

OK=()
FAIL=()

step() {  # step "название" папка команда...
  local name="$1" dir="$2"; shift 2
  echo
  echo "=== $name"
  if (cd "$dir" && "$@"); then
    OK+=("$name")
  else
    FAIL+=("$name")
    echo "!!! Шаг «$name» завершился с ошибкой (см. сообщение выше)"
  fi
}

step "1. Курс + прогноз (RMM)"              "$SITE" "$PY" data_scripts/update_fx.py --model "$RMM"
step "2. Декомпозиция и баланс потоков"     "$SITE" "$PY" data_scripts/update_ruble_data.py --model "$RMM" --orfr "$ORFR"
# Шаги 3–4 (Мосбиржа) — только для просмотра на Mac: на сайт эти данные с 17.11.2026 публикует облачная машина
# (раздел moex). publish.sh ruble их не загружает, поэтому Mac и машина не пишут одни и те же файлы.
step "3. Фьючерсы физлиц (ALGOPACK, только локально)"  "$SITE" "$PY" data_scripts/update_futoi.py
step "4. Объём торгов юанем (ALGOPACK, только локально)" "$SITE" "$PY" data_scripts/update_cny_volumes.py
step "5. Потребность экспортеров"           "$EXP"  "$PY" need_model.py
step "5а. Доля продажи выручки (считалка)" "$EXP"  "$PY" fx_sales_ratio.py
step "6. Нефть и экспорт"                   "$EXP"  "$PY" oil_exports.py
step "7. Операции ЦБ на валютном рынке"     "$SITE" "$PY" data_scripts/update_cbr_fx_ops.py

echo
echo "=== 8. Проверка: в JSON не должно быть NaN (иначе график на сайте будет пустым)"
BAD=$(grep -l "NaN" "$SITE"/data/*.json 2>/dev/null)
if [ -n "$BAD" ]; then
  echo "!!! NaN найден в: $BAD"
  FAIL+=("8. Проверка NaN")
else
  echo "ок"
  OK+=("8. Проверка NaN")
fi

if [ "$PUBLISH" = "1" ]; then
  if [ ${#FAIL[@]} -gt 0 ]; then
    echo
    echo "!!! Были ошибки — публикация пропущена. Исправьте и запустите снова с --publish."
  else
    step "9. Публикация на сайт (бакет)" "$SITE" env PY="$PY" ./publish.sh ruble
  fi
fi

echo
echo "================ Сводка"
for s in "${OK[@]}"; do echo "  ок      $s"; done
for s in "${FAIL[@]+"${FAIL[@]}"}"; do echo "  ОШИБКА  $s"; done
[ ${#FAIL[@]} -eq 0 ]
