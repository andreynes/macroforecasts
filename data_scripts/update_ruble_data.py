"""Пересчитывает данные вкладки «Рубль» и пишет их в data/ruble_page.json (страница загружает его через
/api/ruble/page; на сайт файл попадает командой publish.sh ruble).

Источники:
  - Russia Mega Model.xlsx, лист RUB — «Модель 2»: факторы (стр. 210–223), коэффициенты (стр. 226–265),
    факт и модель dUSDRUB (стр. 270–271), среднемесячный USD/RUB (стр. 6);
  - сводный файл ОРФР «Операции на валютном рынке (ОРФР, сводно).xlsx» — продажи/покупки валюты
    нефинансовыми компаниями (для графика «Баланс потоков»).

Запуск:
  python data_scripts/update_ruble_data.py --model "<путь>/Russia Mega Model.xlsx" --orfr "<путь>/Операции ... .xlsx"
  Снимок прогноза факторов (таблица «Снимок» и база для графика пересмотра) не трогается; чтобы зафиксировать
  новый снимок после обновления прогнозов, добавьте --new-snapshot.
"""
import argparse
import json
import math
import os
from datetime import date

import numpy as np
import openpyxl
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(BASE_DIR, "data", "ruble_page.json")

# Ключ на сайте -> (строка фактора, строка коэффициента) на листе RUB
FLOWS = {
    "export": (210, 226),
    "cbr_ops": (211, 229),
    "debt_conv": (212, 232),
    "imports": (213, 235),
    "debt_repay": (214, 238),
    "households": (215, 241),
    "other_flows": (216, 244),
}
DXY = (217, 247)
ONE_OFF = [(218, 250), (219, 253), (220, 256), (221, 259), (222, 262), (223, 265)]  # D3–D6, COVID, санкции 2018
ROW_DATES, ROW_USDRUB_AVG, ROW_FACT, ROW_MODEL = 3, 6, 270, 271
FIRST_COL = 3  # столбец D — январь 2018
# Горизонт прогноза — конец 2027 года; правее на листе RUB стоят итоговые суммы, а не месяцы
START, END = "2018-01-01", "2027-12-01"

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return np.nan
    return x if math.isfinite(x) else np.nan


def load_model(path):
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True)["RUB"]
    rows = {i: r for i, r in enumerate(ws.iter_rows(min_row=1, max_row=ROW_MODEL, values_only=True), 1)}
    dates = [pd.Timestamp(v) for v in rows[ROW_DATES][FIRST_COL:] if v is not None]
    idx = pd.DatetimeIndex(dates)
    series = lambda i: pd.Series([num(v) for v in rows[i][FIRST_COL:FIRST_COL + len(idx)]], index=idx)
    coef = lambda i: num(rows[i][FIRST_COL])
    x = pd.DataFrame({k: series(r) for k, (r, _) in FLOWS.items()})
    b = {k: coef(c) for k, (_, c) in FLOWS.items()}
    x["dxy"], b["dxy"] = series(DXY[0]), coef(DXY[1])
    one_off = sum(series(r) * coef(c) for r, c in ONE_OFF) * 100
    return x, b, one_off, series(ROW_FACT) * 100, series(ROW_MODEL) * 100, series(ROW_USDRUB_AVG)


def load_orfr_monthly(path, sheet, value_col):
    ws = openpyxl.load_workbook(path, data_only=True)[sheet]
    out, year, col = {}, None, None
    for r in ws.iter_rows(values_only=True):
        r = list(r)
        if col is None:
            col = r.index(value_col) if value_col in r else None
            continue
        for v in r[:col]:
            if isinstance(v, int) and 2000 < v < 2100:
                year = v
        month = next((v for v in r[:col] if v in MONTHS), None)
        if month and r[col] is not None:
            out[pd.Timestamp(year, MONTHS.index(month) + 1, 1)] = float(r[col])
    return pd.Series(out)


def records(df, digits=4):
    df = df.loc[START:END]
    out = []
    for d, row in df.iterrows():
        rec = {"date": d.strftime("%Y-%m-%d")}
        for k, v in row.items():
            rec[k] = None if pd.isna(v) else round(float(v), digits)
        out.append(rec)
    return out


def build(model_path, orfr_path, today, new_snapshot=False):
    x, b, one_off, fact, model, usdrub = load_model(model_path)
    flow_keys = list(FLOWS)

    contrib = pd.DataFrame({k: x[k] * b[k] * 100 for k in flow_keys + ["dxy"]})
    decomp = contrib.assign(one_off=one_off, model=model, fact=fact)

    # Отклонение от «нормы» — среднего за предыдущие 12 месяцев, без заглядывания вперёд
    dev = pd.DataFrame({k: (x[k] - x[k].shift(1).rolling(12).mean()) * b[k] * 100 for k in flow_keys + ["dxy"]})
    dev = dev.assign(one_off=one_off, fact=fact)

    yoy = pd.DataFrame({k: (x[k] - x[k].shift(12)) * np.sign(b[k]) for k in flow_keys})

    sales = load_orfr_monthly(orfr_path, "РВ Продажи нефинансовых", "Объемы продаж")
    purchases_rub = load_orfr_monthly(orfr_path, "РВ Покупки нефинансовых", "Объем покупок")
    months = sales.index.intersection(purchases_rub.index).intersection(fact.dropna().index)
    balance = pd.DataFrame({
        "fx_sales": sales,
        "fx_purchases": -purchases_rub / usdrub,
        **{k: x[k] for k in ["cbr_ops", "debt_conv", "debt_repay", "households", "other_flows"]},
        "one_off_pct": one_off,
        "fact": fact,
    }).loc[months]

    consts = {
        "RUB_DECOMP": records(decomp),
        "RUB_DECOMP_DEV": records(dev),
        "RUB_YOY": records(yoy),
        "RUB_BALANCE": records(balance),
    }

    # Снимок — прогноз факторов на момент обновления прогнозов; перезаписывается только по --new-snapshot,
    # иначе берётся уже зафиксированный в data/ruble_page.json
    if new_snapshot:
        cur_month = pd.Timestamp(today.year, today.month, 1)
        snapshot = {"snapshot_date": today.isoformat(), "rows": records(x.loc[cur_month:END, flow_keys + ["dxy"]])}
        consts["RUB_SNAPSHOT"] = snapshot
    else:
        snapshot = read_const("RUB_SNAPSHOT")

    # Пересмотр факторов к снимку: «млрд $» со знаком влияния на курс (как в графике г/г),
    # «п.п.» — тот же пересмотр, умноженный на коэффициент регрессии (вклад в прогноз изменения курса)
    snap = pd.DataFrame(snapshot["rows"]).assign(date=lambda d: pd.to_datetime(d["date"])).set_index("date")
    diff = (x.loc[snap.index, flow_keys] - snap[flow_keys])
    consts["RUB_REVISION"] = {
        "snapshot_date": snapshot["snapshot_date"],
        "bn": records(diff.apply(lambda s: s * np.sign(b[s.name]))),
        "pp": records(diff.apply(lambda s: s * b[s.name] * 100)),
    }
    return consts


def read_page():
    with open(OUT_FILE, encoding="utf-8") as f:
        return json.load(f)


def read_const(name):
    page = read_page()
    if name not in page:
        raise SystemExit(f"В {OUT_FILE} нет блока {name}")
    return page[name]


def write_page(consts):
    # Блоки, которые не пересчитывались (снимок без --new-snapshot), остаются прежними
    page = {**read_page(), **consts}
    payload = json.dumps(page, ensure_ascii=False, allow_nan=False)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, OUT_FILE)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True, help="Russia Mega Model.xlsx")
    p.add_argument("--orfr", required=True, help="Операции на валютном рынке (ОРФР, сводно).xlsx")
    p.add_argument("--new-snapshot", action="store_true",
                   help="зафиксировать новый снимок прогноза факторов (только при обновлении прогнозов)")
    p.add_argument("--today", default=date.today().isoformat(), help="дата нового снимка, YYYY-MM-DD")
    a = p.parse_args()
    consts = build(a.model, a.orfr, date.fromisoformat(a.today), a.new_snapshot)
    write_page(consts)
    last_fact = [r["date"] for r in consts["RUB_DECOMP"] if r["fact"] is not None][-1]
    print(f"ruble_page.json обновлён: последний факт {last_fact}, баланс потоков по {consts['RUB_BALANCE'][-1]['date']}, "
          f"снимок {'новый' if a.new_snapshot else 'прежний'} на {consts['RUB_REVISION']['snapshot_date']}")
