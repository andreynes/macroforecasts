"""Нетто-позиция населения по валютным фьючерсам Мосбиржи — данные для вкладки «Рубль».

Источники:
  - ISS Мосбиржи, analyticalproducts/futoi (ALGOPACK): открытые позиции физлиц и юрлиц на конец дня
    (последняя 5-минутка дня; сегодня — последняя доступная). С ключом подписки ALGOPACK
    (~/.config/moex/algopack_token или переменная MOEX_ALGOPACK_TOKEN) — без задержки через apim.moex.com,
    без ключа — бесплатный доступ с задержкой 14 дней;
  - официальные курсы ЦБ (USD, EUR, CNY).

Тикеры: Si, USDRUBF (доллар), Eu, EURRUBF (евро), CR, CNYRUBF (юань); 1 контракт = 1000 ед. валюты.
Пишет data/futoi_households.json: нетто-позиция физлиц (млрд $ по кросс-курсам ЦБ; net_rub — млрд руб.) и сумма её
изменений за последние 4 недели (изменение в контрактах × курс ЦБ на дату — без переоценки).

Запуск: python data_scripts/update_futoi.py
"""
import json
import os
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(BASE_DIR, "data", "futoi_households.json")

TICKERS = {"Si": "USD", "USDRUBF": "USD", "Eu": "EUR", "EURRUBF": "EUR", "CR": "CNY", "CNYRUBF": "CNY"}
CBR_CODES = {"USD": "R01235", "EUR": "R01239", "CNY": "R01375"}
FIRST_YEAR = 2020
LOT = 1000
FREE_LAG_DAYS = 15  # бесплатный доступ Мосбиржи закрывает последние 14 дней
TOKEN_FILE = os.path.expanduser("~/.config/moex/algopack_token")


def algopack_token():
    tok = os.getenv("MOEX_ALGOPACK_TOKEN", "").strip()
    if not tok and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    return tok or None


TOKEN = algopack_token()
ISS_BASE = "https://apim.moex.com/iss" if TOKEN else "https://iss.moex.com/iss"


def http_get(url, auth=False):
    """Через системный curl: у apim.moex.com сертификат российского УЦ, которого нет в хранилище Python,
    а системное хранилище его знает (проверка сертификата не отключается). Заголовок с ключом передаётся
    curl через stdin (-H @-), чтобы ключ не попадал в аргументы процесса."""
    cmd = ["curl", "-sS", "--fail", "-m", "60", "-A", "Mozilla/5.0"]
    stdin = None
    if auth and TOKEN:
        cmd += ["-H", "@-"]
        stdin = f"Authorization: Bearer {TOKEN}\n".encode()
    for attempt in range(5):
        r = subprocess.run(cmd + [url], input=stdin, capture_output=True, timeout=90)
        if r.returncode == 0:
            return r.stdout
        if attempt == 4:
            raise RuntimeError(f"curl {r.returncode}: {r.stderr.decode(errors='ignore')[:200]}")
        time.sleep(2 + attempt * 3)


def load_futoi():
    last_open = date.today() if TOKEN else date.today() - timedelta(days=FREE_LAG_DAYS)
    rows = []
    for ticker in TICKERS:
        for year in range(FIRST_YEAR, last_open.year + 1):
            for a, b in [(date(year, 1, 1), date(year, 6, 30)), (date(year, 7, 1), date(year, 12, 31))]:
                b = min(b, last_open)
                if a > b:
                    continue
                url = (f"{ISS_BASE}/analyticalproducts/futoi/securities/{ticker.lower()}.json"
                       f"?latest=1&from={a}&till={b}")
                d = json.loads(http_get(url, auth=True))["futoi"]
                if d["columns"][0] == "ERROR_MESSAGE":
                    raise SystemExit(f"Мосбиржа вернула ошибку для {ticker} {a}–{b}: {d['data']}")
                rows += [dict(zip(d["columns"], r)) for r in d["data"]]
    df = pd.DataFrame(rows)
    df = df[df.clgroup == "FIZ"]
    df["tradedate"] = pd.to_datetime(df.tradedate)
    return df.pivot_table(index="tradedate", columns="ticker", values="pos", aggfunc="last")


def load_cbr_rates(start, end):
    out = {}
    for cur, code in CBR_CODES.items():
        url = (f"https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1={start:%d/%m/%Y}"
               f"&date_req2={end:%d/%m/%Y}&VAL_NM_RQ={code}")
        root = ET.fromstring(http_get(url))
        out[cur] = pd.Series({
            pd.to_datetime(r.get("Date"), dayfirst=True):
                float(r.find("Value").text.replace(",", ".")) / float(r.find("Nominal").text)
            for r in root.findall("Record")
        })
    return pd.DataFrame(out).sort_index()


def num(s, digits):
    """NaN → null: JSON с NaN браузер не разбирает."""
    return [None if pd.isna(v) else round(float(v), digits) for v in s]


def build():
    pos = load_futoi()
    # Пропуски по тикеру (дни без данных по вечным фьючерсам, выходные сессии) — последним значением,
    # иначе сумма по тикерам в эти дни проседает
    first = {t: pos[t].first_valid_index() for t in pos.columns}
    for t in pos.columns:
        pos.loc[first[t]:, t] = pos.loc[first[t]:, t].ffill()
    pos = pos.fillna(0)

    days = pos.index
    rates = load_cbr_rates(days.min().date(), date.today())
    rates = rates.reindex(rates.index.union(days)).ffill().bfill().loc[days]  # bfill — дни до первого курса ЦБ в году
    rate = pd.DataFrame({t: rates[TICKERS[t]] for t in pos.columns})

    net = (pos * LOT / 1e9 * rate).sum(axis=1)
    flow = (pos.diff() * LOT / 1e9 * rate).sum(axis=1)
    flow_4w = flow.rolling("28D").sum()
    # то же в млрд $: рублевая оценка по курсу ЦБ на дату / USD/RUB на дату (EUR и CNY — через кросс-курс ЦБ)
    usd = rates["USD"]
    net_usd = net / usd
    flow_4w_usd = (flow / usd).rolling("28D").sum()

    return {
        "updated": date.today().isoformat(),
        "last_date": days.max().strftime("%Y-%m-%d"),
        "dates": [d.strftime("%Y-%m-%d") for d in days],
        "net": num(net_usd, 3),
        "flow_4w": num(flow_4w_usd, 3),
        "net_rub": num(net, 2),
        "flow_4w_rub": num(flow_4w, 2),
    }


if __name__ == "__main__":
    data = build()
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"{OUT_FILE}: {len(data['dates'])} дней, по {data['last_date']}, "
          f"нетто {data['net'][-1]} млрд $, за 4 недели {data['flow_4w'][-1]:+} млрд $")
