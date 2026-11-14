"""Курсы USD/RUB, EUR/RUB, CNY/RUB для графика «Динамика курса + прогноз» на вкладке «Рубль» (data/ruble_fx.json).
macro_main_linked.xlsx (вкладка «Макропрогнозы», свой прогноз курса) не трогает — его собирает update_macro.py.

Факт — среднемесячный официальный курс ЦБ за все завершённые месяцы; текущий месяц — тоже факт (среднее с начала
месяца), если с его начала прошло больше 10 календарных дней (MTD_MIN_DAYS), иначе он прогнозный.
Прогноз (с первого нефактического месяца до конца 2027 года) — из Russia Mega Model:
  USD/RUB — лист RUB, строка «USDRUB fact + forecast» (Модель 2);
  EUR/RUB и CNY/RUB — через кросс-курсы EUR/USD и USD/CNY с листа BoP_monthly (на листе RUB их нет).

Запуск:
  python data_scripts/update_fx.py --model "<путь>/Russia Mega Model.xlsx"
"""
import argparse
import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date

import openpyxl
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(BASE_DIR, "data", "ruble_fx.json")

CBR_CODES = {"USD/RUB": "R01235", "EUR/RUB": "R01239", "CNY/RUB": "R01375"}
FC_END = "2027-12-31"
MTD_MIN_DAYS = 10  # текущий месяц считается фактом (среднее с начала месяца), если прошло > 10 календарных дней
RUB_ROW_DATES, RUB_ROW_USD_FC, RUB_FIRST_COL = 3, 273, 3        # данные листа RUB — со столбца D
BOP_ROW_DATES, BOP_ROW_EURUSD, BOP_ROW_USDCNY, BOP_FIRST_COL = 3, 9, 10, 4  # BoP_monthly — со столбца E


def cbr_monthly(start="2000-01-01"):
    out = {}
    for alias, code in CBR_CODES.items():
        url = (f"https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1={pd.Timestamp(start):%d/%m/%Y}"
               f"&date_req2={date.today():%d/%m/%Y}&VAL_NM_RQ={code}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(urllib.request.urlopen(req, timeout=90).read())
        daily = pd.Series({
            pd.to_datetime(r.get("Date"), dayfirst=True):
                float(r.find("Value").text.replace(",", ".")) / float(r.find("Nominal").text)
            for r in root.findall("Record")
        })
        out[alias] = daily.resample("ME").mean()
    return pd.DataFrame(out)


def model_row(ws_rows, row_dates, row, first_col):
    dates = [pd.Timestamp(v) + pd.offsets.MonthEnd(0) for v in ws_rows[row_dates][first_col:] if v is not None]
    vals = pd.to_numeric(pd.Series(ws_rows[row][first_col:first_col + len(dates)]), errors="coerce")
    return pd.Series(vals.values, index=dates)


def model_forecast(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rub = {i: r for i, r in enumerate(wb["RUB"].iter_rows(min_row=1, max_row=RUB_ROW_USD_FC, values_only=True), 1)}
    bop = {i: r for i, r in enumerate(wb["BoP_monthly"].iter_rows(min_row=1, max_row=BOP_ROW_USDCNY, values_only=True), 1)}
    usd = model_row(rub, RUB_ROW_DATES, RUB_ROW_USD_FC, RUB_FIRST_COL)
    eurusd = model_row(bop, BOP_ROW_DATES, BOP_ROW_EURUSD, BOP_FIRST_COL)
    usdcny = model_row(bop, BOP_ROW_DATES, BOP_ROW_USDCNY, BOP_FIRST_COL)
    return pd.DataFrame({"USD/RUB": usd, "EUR/RUB": usd * eurusd, "CNY/RUB": usd / usdcny})


def build(model_path):
    fact = cbr_monthly()
    today = pd.Timestamp(date.today())
    first_fc = today + pd.offsets.MonthEnd(0)  # текущий месяц — прогноз, если прошло ≤ 10 дней
    if (today - today.replace(day=1)).days >= MTD_MIN_DAYS:
        first_fc = first_fc + pd.offsets.MonthEnd(1)  # иначе — факт: среднее с начала месяца
    fact = fact.loc[:first_fc - pd.offsets.MonthEnd(1)]
    fc = model_forecast(model_path).loc[first_fc:FC_END]
    if fc.isna().any().any():
        raise SystemExit(f"В модели нет прогноза курса на месяцы: {fc[fc.isna().any(axis=1)].index.date.tolist()}")
    return pd.concat([fact, fc]).round(4), first_fc


def write(series, first_fc, mtd_to=None):
    last_fact = first_fc - pd.offsets.MonthEnd(1)
    data = {"dates": [d.strftime("%Y-%m-%d") for d in series.index],
            "last_fact": last_fact.strftime("%Y-%m-%d"),
            "mtd_to": mtd_to,  # дата последнего курса ЦБ, если последний фактический месяц неполный
            **{key: [float(v) for v in series[alias]] for key, alias in
               {"usd": "USD/RUB", "eur": "EUR/RUB", "cny": "CNY/RUB"}.items()}}
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True, help="Russia Mega Model.xlsx")
    a = p.parse_args()
    series, first_fc = build(a.model)
    today = pd.Timestamp(date.today())
    mtd_to = today.strftime("%Y-%m-%d") if first_fc > today + pd.offsets.MonthEnd(0) else None
    write(series, first_fc, mtd_to)
    last_fact = first_fc - pd.offsets.MonthEnd(1)
    print(f"{OUT_FILE} обновлён: факт ЦБ по {last_fact:%m.%Y}, прогноз модели с {first_fc:%m.%Y} по 12.2027")
    print(series.loc[last_fact - pd.offsets.MonthEnd(2):].round(2).to_string())
