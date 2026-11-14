"""Переносит новый выпуск «Обзора рисков финансовых рынков» ЦБ (ORFR_ГГГГ-М.xlsx) в сводный файл
«Рубль/Операции на валютном рынке (ОРФР, сводно).xlsx», который читает график «Баланс потоков» вкладки «Рубль».

Листы сводного файла, которые есть в новом выпуске, заменяются целиком (значения, без формул); листы, которых в
выпуске нет (например, «РВ продажи 29 экспортеров» — ЦБ перестал их публиковать с ноября 2026 г.), остаются как были.
В «РВ USD, EUR, CNY» оставляются только будние дни (как в прежней сетке). На листе README обновляется источник.
Перед записью делается копия сводного файла рядом: «… .bak.xlsx».

Запуск:
  python data_scripts/update_orfr.py ~/Downloads/ORFR_2026-11.xlsx
  (затем — data_scripts/update_ruble_data.py, чтобы пересчитать «Баланс потоков», и деплой)
"""
import datetime as dt
import os
import re
import shutil
import sys

import openpyxl

SVOD = "/Users/andrejkrylov/Desktop/Совкомбанк/Рубль/Операции на валютном рынке (ОРФР, сводно).xlsx"
MONTHS_GEN = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
              "ноябрь", "декабрь"]


def main(new_path, svod=SVOD):
    tag = os.path.basename(new_path)
    shutil.copy2(svod, svod.replace(".xlsx", ".bak.xlsx"))
    s = openpyxl.load_workbook(svod)
    n = openpyxl.load_workbook(new_path, data_only=True)
    updated, kept = [], []
    for name in list(s.sheetnames):
        if name == "README":
            continue
        if name not in n.sheetnames:
            kept.append(name)
            continue
        idx = s.sheetnames.index(name)
        s.remove(s[name])
        ws = s.create_sheet(name, idx)
        for r in n[name].iter_rows(values_only=True):
            if name == "РВ USD, EUR, CNY":
                d = next((v for v in r if isinstance(v, dt.datetime)), None)
                if d is not None and d.weekday() >= 5:
                    continue
            ws.append(list(r))
        updated.append(name)

    m = re.search(r"(\d{4})-(\d{1,2})", tag)
    period = f"{MONTHS_GEN[int(m.group(2)) - 1]} {m.group(1)}" if m else "новый выпуск"
    rd = s["README"]
    for row in rd.iter_rows():
        v = row[0].value
        if v in updated and len(row) > 1:
            row[1].value = tag
        elif isinstance(v, str) and v.startswith("ORFR_") and "(" in v:
            row[0].value = f"{tag} ({period}); листы, которых в нём нет, — из прежних выпусков (см. таблицу ниже)"
        elif isinstance(v, str) and v.startswith("Обновлено "):
            row[0].value = (f"Обновлено {dt.date.today()} из {tag}. «РВ USD, EUR, CNY»: ряд дневной календарный, "
                            "оставлены только рабочие дни (как в прежней сетке).")
    s.save(svod)
    print(f"{svod}\n  обновлены из {tag}: {', '.join(updated)}\n  оставлены прежними: {', '.join(kept) or '—'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Укажите путь к выпуску ЦБ: python data_scripts/update_orfr.py ~/Downloads/ORFR_2026-11.xlsx")
    main(os.path.expanduser(sys.argv[1]))
