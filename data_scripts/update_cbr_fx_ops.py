"""Операции Банка России на внутреннем валютном рынке по месяцам с декомпозицией — для вкладки «Рубль».

Факт (итог): ЦБ, факторы ликвидности (cbr.ru/statistics/flikvid), столбец «операции Минфина по покупке (продаже)
иностранной валюты на внутреннем валютном рынке» — совокупные операции по бюджетному правилу с учётом собственных
операций Банка России, по дням расчётов; «+» — покупка валюты, «−» — продажа.
Декомпозиция:
  - бюджетное правило — объём операций Минфина за месяц R (НГД/НГД файл Минфина.xlsx), разнесённый поровну по дням
    периода операций: с 5-го рабочего дня R по 4-й рабочий день R+1, расчёты T+1;
  - ЦБ регулярные (зеркалирование инвестиций ФНБ) и нерегулярные (ФНБ на дефицит вне БП, отложенные операции) —
    объявленные ЦБ объёмы в день, лист «Операции_ЦБ» в НГД/НГД_входные_данные.xlsx (с 2025 г.);
  - до появления объявленных объёмов (2023–2024) собственные операции ЦБ = факт − бюджетное правило;
  - «прочее» — расхождение факта и суммы компонент (сдвиги дат расчётов, операции с золотом).
Прогноз (с текущего месяца, ~6 мес.): бюджетное правило — forecast.bp_ops_forecast из «Модели экспортеров»
(модель НГД, Brent — консенсус аналитиков, курс — «Макропрогнозы», цена отсечения $59 в 2026 г. и $50 с 2027 г.;
объявленные Минфином месяцы — как есть), операции месяца R поровну по рабочим дням с 5-го р.д. R по 4-й р.д. R+1;
ЦБ регулярные — объявленные на текущее полугодие, на следующее — зеркалирование инвестиций ФНБ за прошлое полугодие
(FNB_INVEST ниже); нерегулярные — объявленные (в прогнозе — 0, если ЦБ не объявил).
Запуск: python data_scripts/update_cbr_fx_ops.py → data/cbr_fx_ops.json.
"""
import html
import json
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(BASE_DIR, "data", "cbr_fx_ops.json")
NGD_DIR = "/Users/andrejkrylov/Desktop/Совкомбанк/НГД"
START = "2023-01-01"
EXPORTERS_DIR = "/Users/andrejkrylov/Desktop/Совкомбанк/Рубль/Модель экспортеров"
# Инвестиции ФНБ, которые ЦБ будет зеркалировать в следующем полугодии (млрд руб.), и период зеркалирования.
# I пол. 2027: размещения ФНБ в июле–октябре 2026 г. по сообщениям Минфина (июль 127,8; август 93,9; сентябрь 1,1;
# октябрь 26,0) — оценка, пока ЦБ не объявил объём в конце декабря.
FNB_INVEST = [("2027-01-11", "2027-06-30", 248.7)]


def liquidity_fact():
    url = ("https://www.cbr.ru/statistics/flikvid/?UniDbQuery.Posted=True&UniDbQuery.From=01.12.2022"
           f"&UniDbQuery.To={date.today():%d.%m.%Y}")
    h = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=90).read().decode("utf-8")
    tbl = re.search(r"<table.*?</table>", h, re.S).group(0)
    out = {}
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
        c = [html.unescape(re.sub("<[^>]+>", "", x)).strip().replace("\xa0", "").replace(" ", "")
             for x in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
        if len(c) == 9 and re.match(r"\d\d\.\d\d\.\d{4}", c[0]):
            v = c[6]
            out[pd.to_datetime(c[0], dayfirst=True)] = float(v.replace(",", ".")) if v not in ("", "—", "-") else 0.0
    return pd.Series(out).sort_index()


def main():
    sys.path.insert(0, NGD_DIR)
    import ngd_model as ng
    fact = liquidity_fact()
    days = fact.index  # дни расчётов (рабочие дни)
    mf = ng.load_minfin(ng.MINFIN_FILE)
    sched = ng.load_cbr_schedule(ng.INPUTS_FILE)

    def bd(y, m, k):  # k-й рабочий день месяца (по календарю дней расчётов ЦБ), 1-based
        d = days[(days.year == y) & (days.month == m)]
        return d[k - 1] if len(d) >= k else None

    # бюджетное правило: объём месяца R — поровну по дням расчётов периода (6-й р.д. R … 5-й р.д. R+1, т.е. сделки T+1)
    bp = pd.Series(0.0, index=days)
    for (y, m), v in mf.items():
        if v.get("fx") is None:
            continue
        a = bd(y, m, 6)
        ny, nm = (y + (m == 12), m % 12 + 1)
        b = bd(ny, nm, 5)
        if a is None:
            continue
        if b is None:  # период ещё не завершён — до последнего дня с данными
            b = days[-1]
            ndays = len(ng.workdays((y, m))) - 4 + 4  # полный период ≈ рабочие дни R
        else:
            ndays = None
        span = days[(days >= a) & (days <= b)]
        n = ndays or len(span)
        bp.loc[span] += v["fx"] / n

    reg = pd.Series(0.0, index=days)
    irr = pd.Series(0.0, index=days)
    for a, b, r_, i_, _ in sched:
        span = days[(days > pd.Timestamp(a)) & (days <= pd.Timestamp(b) + pd.Timedelta(days=1))]  # расчёты T+1
        reg.loc[span] += r_
        irr.loc[span] += i_
    has_sched = pd.Series(False, index=days)
    for a, b, *_ in sched:
        has_sched.loc[(days > pd.Timestamp(a) - pd.Timedelta(days=10)) & (days <= pd.Timestamp(b) + pd.Timedelta(days=1))] = True
    sched_start = min((pd.Timestamp(a) for a, *_ in sched), default=pd.Timestamp("2100-01-01"))

    df = pd.DataFrame({"fact": fact, "bp": bp, "reg": reg, "irr": irr})
    m = df.resample("MS").sum().loc[START:]
    first_sched_month = sched_start.to_period("M").to_timestamp()
    before = m.index < first_sched_month
    m["own"] = 0.0
    m.loc[before, "own"] = m.loc[before, "fact"] - m.loc[before, "bp"]   # 2023–2024: ЦБ без разбивки
    m.loc[before, ["reg", "irr"]] = 0.0
    m["other"] = m["fact"] - m["bp"] - m["reg"] - m["irr"] - m["own"]

    usd = cbr_usd(m.index.min(), pd.Timestamp(date.today()))
    usd_m = usd.resample("MS").mean().reindex(m.index).ffill()
    m["forecast"] = False

    # ---- прогноз с текущего месяца
    sys.path.insert(0, EXPORTERS_DIR)
    import forecast as fc
    cur = pd.Timestamp(date.today()).to_period("M").to_timestamp()
    a, _, _ = fc.assumptions(fc.horizon(cur - pd.offsets.MonthBegin(1)), cur - pd.offsets.MonthBegin(1))
    # Текущий (неполный) месяц — налоговый месяц для операций следующего месяца: вместо пути к консенсусу берём
    # фактические средние с начала месяца (Urals и Brent — лист «Нефть_дневная», курс — ЦБ), как в модели НГД.
    # (В ноябре 2026 г. путь к консенсусу давал Urals ~89 $/барр при фактических ~74.)
    _, ur_d = ng.load_inputs(ng.INPUTS_FILE)
    ur_cur = [v for d, v in ur_d if (d.year, d.month) == (cur.year, cur.month)]
    br_cur = [v for d, v in ng.BRENT_DAILY if (d.year, d.month) == (cur.year, cur.month)]
    usd_cur = cbr_usd(cur, pd.Timestamp(date.today()))
    if len(ur_cur) >= 5 and cur in a.index:
        print(f"{cur:%Y-%m} было: Urals {a.loc[cur, 'Urals, $/барр']:.2f}, Brent {a.loc[cur, 'Brent, $/барр']:.2f}, "
              f"USD/RUB {a.loc[cur, 'USD/RUB']:.2f}")
        a.loc[cur, "Urals, $/барр"] = sum(ur_cur) / len(ur_cur)
        if br_cur:
            a.loc[cur, "Brent, $/барр"] = sum(br_cur) / len(br_cur)
        if len(usd_cur):
            a.loc[cur, "USD/RUB"] = float(usd_cur.mean())
        print(f"{cur:%Y-%m} стало (факт с начала месяца, {len(ur_cur)} дн.): Urals {a.loc[cur, 'Urals, $/барр']:.2f}, "
              f"Brent {a.loc[cur, 'Brent, $/барр']:.2f}, USD/RUB {a.loc[cur, 'USD/RUB']:.2f}")
    bp_tab, k_non = fc.bp_ops_forecast(a, cur - pd.offsets.MonthBegin(1))
    fdays = {}  # прогнозные дни операций: дата → (bp, reg, irr)
    for R, row in bp_tab.iterrows():
        Rk = (R.year, R.month)
        wd = ng.workdays(Rk)[4:] + ng.workdays(ng.nxt(Rk))[:4]
        for d in wd:
            fdays.setdefault(pd.Timestamp(d), [0.0, 0.0, 0.0])[0] += row["Операции месяца R"] / len(wd)
    for d in list(fdays):
        r_ = i_ = 0.0
        for a_, b_, rr, ii, _ in sched:
            if pd.Timestamp(a_) <= d <= pd.Timestamp(b_):
                r_, i_ = rr, ii
        for a_, b_, total in FNB_INVEST:
            if pd.Timestamp(a_) <= d <= pd.Timestamp(b_) and not any(pd.Timestamp(x) <= d <= pd.Timestamp(y) for x, y, *_ in sched):
                n_ = len([x for x in pd.bdate_range(a_, b_) if x.date() not in ng.HOLIDAYS])
                r_ = -total / n_
        fdays[d][1], fdays[d][2] = r_, i_
    fd = pd.DataFrame(fdays, index=["bp", "reg", "irr"]).T.sort_index()
    fm = fd.resample("MS").sum()
    a_usd = a["USD/RUB"]
    last_full_fc = bp_tab.index.max()  # месяц операций R_max покрывает календарь только до 4-го р.д. R_max+1
    fm = fm.loc[cur:last_full_fc]
    fm["own"] = fm["other"] = 0.0
    fm["fact"] = fm[["bp", "reg", "irr"]].sum(axis=1)
    fm["forecast"] = True
    m = pd.concat([m.loc[m.index < cur], fm[m.columns.drop("forecast").tolist() + ["forecast"]]])
    usd_m = pd.concat([usd_m.loc[usd_m.index < cur], a_usd.reindex(fm.index)])
    print("Прогноз операций Минфина по месяцам R (млрд руб.):")
    print(bp_tab.round(1).to_string())

    days_in = fact.groupby(fact.index.to_period("M").to_timestamp()).size().reindex(m.index)
    num = lambda s: [round(float(v), 2) for v in s]
    data = {"updated": date.today().isoformat(), "last_day": f"{days[-1]:%Y-%m-%d}",
            "months": [f"{d:%Y-%m-%d}" for d in m.index], "forecast": [bool(v) for v in m["forecast"]],
            "fnb_invest": FNB_INVEST,
            "sched_from": f"{first_sched_month:%Y-%m}",
            **{k: num(m[k]) for k in ["fact", "bp", "reg", "irr", "own", "other"]},
            **{f"{k}_usd": num(m[k] / usd_m) for k in ["fact", "bp", "reg", "irr", "own", "other"]}}
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    pd.set_option("display.width", 200)
    print(m.round(0).tail(14).to_string())
    print(f"{OUT_FILE}: {len(m)} мес., по {days[-1]:%d.%m.%Y}")


def cbr_usd(start, end):
    url = (f"https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1={start:%d/%m/%Y}"
           f"&date_req2={end:%d/%m/%Y}&VAL_NM_RQ=R01235")
    root = ET.fromstring(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=90).read())
    return pd.Series({pd.to_datetime(r.get("Date"), dayfirst=True): float(r.find("Value").text.replace(",", "."))
                      for r in root.findall("Record")}).sort_index()


if __name__ == "__main__":
    main()
