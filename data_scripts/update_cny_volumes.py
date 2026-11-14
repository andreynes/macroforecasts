"""Дневной объём торгов юанем на Мосбирже: CNY000000TOD (расчёты «сегодня») и CNYRUB_TOM («завтра»), млрд $.

Источник — ALGOPACK (подписка Мосбиржи), datashop/algopack/fx/tradestats: 5-минутные интервалы, val — объём в рублях;
сумма за день переводится в доллары по официальному курсу USD/RUB ЦБ на дату. Ключ подписки —
~/.config/moex/algopack_token или переменная MOEX_ALGOPACK_TOKEN. Прошлые месяцы кэшируются в data/cache/cny_volumes/.
Запуск: python data_scripts/update_cny_volumes.py → data/cny_volumes.json (вкладка «Рубль»).
"""
import json
import os
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(BASE_DIR, "data", "cny_volumes.json")
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache", "cny_volumes")
SECS = {"tod": "CNY000000TOD", "tom": "CNYRUB_TOM"}
START = "2023-01-01"
TOKEN_FILE = os.path.expanduser("~/.config/moex/algopack_token")


def token():
    tok = os.getenv("MOEX_ALGOPACK_TOKEN", "").strip()
    if not tok and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    if not tok:
        raise SystemExit("Нет ключа ALGOPACK: ~/.config/moex/algopack_token")
    return tok


def algopack(path, tok):
    """Через системный curl (сертификат российского УЦ у apim.moex.com); ключ — через stdin, не в аргументах."""
    for attempt in range(5):
        r = subprocess.run(["curl", "-sS", "--fail", "-m", "90", "-H", "@-", f"https://apim.moex.com/{path}"],
                           input=f"Authorization: Bearer {tok}\n".encode(), capture_output=True, timeout=120)
        if r.returncode == 0:
            return json.loads(r.stdout)
        time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"ALGOPACK {path}: {r.stderr.decode(errors='ignore')[:200]}")


def month_daily(sec, m0, m1, tok):
    """Объём в рублях по дням за период (пагинация по 1000 строк)."""
    acc, start = {}, 0
    while True:
        d = algopack(f"iss/datashop/algopack/fx/tradestats/{sec}.json?from={m0}&till={m1}&start={start}&iss.meta=off", tok)
        cols, rows = d["data"]["columns"], d["data"]["data"]
        i_d, i_v = cols.index("tradedate"), cols.index("val")
        for r in rows:
            acc[r[i_d]] = acc.get(r[i_d], 0.0) + (r[i_v] or 0)
        total = d.get("data.cursor", {}).get("data", [[0, 0, 0]])[0][1]
        start += len(rows)
        if not rows or start >= total:
            return acc


def load(sec, tok):
    os.makedirs(CACHE_DIR, exist_ok=True)
    today = pd.Timestamp(date.today())
    out = {}
    for m in pd.date_range(START, today, freq="MS"):
        m_end = m + pd.offsets.MonthEnd(0)
        path = os.path.join(CACHE_DIR, f"{sec}_{m:%Y-%m}.json")
        current = m_end >= today - pd.Timedelta(days=3)  # текущий и только что закончившийся месяц — всегда заново
        if os.path.exists(path) and not current:
            with open(path, encoding="utf-8") as f:
                acc = json.load(f)
        else:
            acc = month_daily(sec, f"{m:%Y-%m-%d}", f"{min(m_end, today):%Y-%m-%d}", tok)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(acc, f)
        out.update(acc)
    return pd.Series(out, dtype=float).rename(index=pd.Timestamp).sort_index()


def cbr_usd(start, end):
    url = (f"https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1={start:%d/%m/%Y}"
           f"&date_req2={end:%d/%m/%Y}&VAL_NM_RQ=R01235")
    root = ET.fromstring(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=90).read())
    return pd.Series({pd.to_datetime(r.get("Date"), dayfirst=True): float(r.find("Value").text.replace(",", "."))
                      for r in root.findall("Record")}).sort_index()


def build():
    tok = token()
    rub = pd.DataFrame({k: load(sec, tok) for k, sec in SECS.items()}).fillna(0)
    rub = rub[(rub.sum(axis=1) > 0)]
    usd = cbr_usd(rub.index.min() - pd.Timedelta(days=10), pd.Timestamp(date.today()))
    rate = usd.reindex(usd.index.union(rub.index)).ffill().reindex(rub.index)
    bn = rub.div(rate, axis=0) / 1e9
    num = lambda s: [round(float(v), 3) for v in s]
    return {"updated": date.today().isoformat(), "dates": [f"{d:%Y-%m-%d}" for d in bn.index],
            "tod": num(bn["tod"]), "tom": num(bn["tom"]),
            "tod_rub": num(rub["tod"] / 1e9), "tom_rub": num(rub["tom"] / 1e9)}


if __name__ == "__main__":
    data = build()
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"{OUT_FILE}: {len(data['dates'])} дней, {data['dates'][0]}…{data['dates'][-1]}; "
          f"последний день TOD {data['tod'][-1]} + TOM {data['tom'][-1]} млрд $")
