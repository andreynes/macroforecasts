"""Пересобирает лист main в macro_main_linked.xlsx (его читает вкладка «Макропрогнозы») из листов,
которые правятся вручную в том же файле: cpi, gdp, key_rate, usdrub, eurrub, cnyrub, FX add.

Курсы: за завершённые месяцы — среднемесячный официальный курс ЦБ, с текущего месяца — значения листов
usdrub/eurrub/cnyrub (EUR и CNY в прогнозе — USD/RUB × EUR/USD и USD/RUB ÷ USD/CNY с листа FX add).
Формулы на листах считаются здесь заново: после сохранения через openpyxl Excel-кэш формул пропадает.

Запуск:
  python data_scripts/update_macro.py
"""
import os
import shutil
from datetime import date

import openpyxl
import pandas as pd

from update_fx import cbr_monthly

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, "macro_main_linked.xlsx")
HEADER = ["indicator_name", "date", "value", "frequency", "aggregation", "units"]


def sheet(name):
    df = pd.read_excel(DATA_FILE, sheet_name=name)
    df["date"] = pd.to_datetime(df[df.columns[0]]) + pd.offsets.MonthEnd(0)
    return df.set_index("date")


def build():
    cpi, gdp, key, fx_add = sheet("cpi"), sheet("gdp"), sheet("key_rate"), sheet("FX add")
    mom = cpi["% м/м"].dropna()
    yoy_calc = ((1 + mom / 100).rolling(12).apply(lambda x: x.prod(), raw=True) - 1) * 100
    yoy = cpi["% г/г"].combine_first(yoy_calc).dropna()

    first_fc = pd.Timestamp(date.today()) + pd.offsets.MonthEnd(0)
    usd = sheet("usdrub")["value"]
    fx = pd.DataFrame({
        "USD/RUB": usd,
        "EUR/RUB": (usd * fx_add["EURUSD"]).combine_first(sheet("eurrub")["value"]),
        "CNY/RUB": (usd / fx_add["CNYUSD"]).combine_first(sheet("cnyrub")["value"]),
    }).loc[first_fc:]
    fact = cbr_monthly().loc[:first_fc - pd.offsets.MonthEnd(1)]
    fx = pd.concat([fact, fx]).round(4)

    parts = [(alias, fx[alias], "M", "avg", "руб.") for alias in fx.columns] + [
        ("ВВП", gdp["% г/г"], "A", "avg", "% г/г"),
        ("Инфляция", yoy, "M", "eop", "% г/г"),
        ("Инфляция", mom, "M", "eop", "% м/м"),
        ("Ключевая ставка", key["value"], "M", "eop", "%"),
    ]
    rows = [[alias, d.to_pydatetime(), float(v), freq, agg, units]
            for alias, s, freq, agg, units in parts for d, v in s.dropna().items()]
    return rows, first_fc


def write(rows):
    shutil.copy(DATA_FILE, DATA_FILE + ".bak")
    wb = openpyxl.load_workbook(DATA_FILE)
    ws = wb["main"]
    ws.delete_rows(1, ws.max_row)
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    for c in ws["B"][1:]:
        c.number_format = "yyyy-mm-dd"
    wb.save(DATA_FILE)


if __name__ == "__main__":
    rows, first_fc = build()
    write(rows)
    df = pd.DataFrame(rows, columns=HEADER)
    print(f"{DATA_FILE}: лист main пересобран (копия — .bak), курсы ЦБ по "
          f"{first_fc - pd.offsets.MonthEnd(1):%m.%Y}, далее — прогноз из листов")
    print(df.groupby(["indicator_name", "units"]).date.agg(["size", "min", "max"]).to_string())
