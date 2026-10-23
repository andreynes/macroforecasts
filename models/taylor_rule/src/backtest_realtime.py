"""Бэктест правила Тейлора КПМ ДДКП в реальном времени — по заседаниям Банка России.

На каждую дату заседания D правило знает только:
  * ключевую ставку, действующую на D;
  * ИПЦ и БИПЦ г/г за месяцы, опубликованные до D (месяц m считается известным с 15-го числа m+1);
  * прогноз инфляции ЦБ (дек/дек), опубликованный на этом заседании (опорные заседания);
  * ВВП (SA) за кварталы, опубликованные до D (квартал известен через 75 дней после окончания),
    разрыв — односторонний HP по этим данным;
  * оценку нейтральной ставки ЦБ, действовавшую на D.
Будущие данные не используются. Параметры не оцениваются — калибровка КПМ ДДКП:
    ставка по правилу = 0,75·i_тек + 0,25·[ r̄ + πᵉ + 1,5·(πᵉ − 4) + 0,5·разрыв ]
πᵉ — ожидаемая инфляция г/г через 3 квартала, линейная интерполяция между последним фактом
и прогнозами ЦБ на конец текущего и следующих лет.

Ограничения: ряды ИПЦ/ВВП — текущей версии (без исторических ревизий); прогноз ЦБ есть только по
общему ИПЦ; нейтральная ставка — по датам пересмотра из NEUTRAL_NOMINAL (до 2023 г. приблизительно).

Запуск: python src/download_data.py && python src/backtest_realtime.py
Результат: outputs/backtest_realtime.png, outputs/backtest_realtime.xlsx
"""
from __future__ import annotations

import datetime as dt

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from taylor_rule import GDP_START, OUT, RAW, TARGET, W_CORE, load_core_m, load_cpi_m, load_gdp_sa, neutral_nominal

RHO, PHI_PI, PHI_Y, HORIZON_Q = 0.75, 1.5, 0.5, 3
CPI_PUBLISH_DAY = 15       # ИПЦ за месяц m известен с 15-го числа месяца m+1
GDP_PUBLISH_LAG_DAYS = 45  # первая оценка ВВП Росстата — примерно через 45 дней после конца квартала
# Разрыв выпуска — оценка ЦБ из «Основных направлений ДКП на 2026–2028 гг.» (рис. 10.8, см. extract_cbr_gap.py),
# 2003Q2–2025Q2. Дальше — продление по фактическому ВВП: потенциал ЦБ растёт на GAP_EXT_POT_GROWTH % в год
# (наклон линии потенциала ЦБ в 2025 г.). Это ретроспективная оценка ЦБ (выпуск 2025 г.), а не оценка на дату заседания.
GAP_EXT_POT_GROWTH = 2.5
NO_CHANGE = 0.25           # |Δ ставки| меньше — «без изменений»
NONCORE_FROM = pd.Timestamp("2018-01-01")  # неопорные заседания (без нового прогноза ЦБ) — с этой даты

# Прогнозы инфляции ЦБ (дек/дек, середина диапазона) до появления PDF «Среднесрочный прогноз» —
# из пресс-релизов по ключевой ставке (опорные заседания 2018 г.)
PRESS_RELEASE_FORECASTS = [
    # дата,       факт пр. года, текущий год, след. год, через год, цитата
    ("2018-04-27", 2.5, 3.5, 4.0, 4.0, "2018: 3–4%; вблизи 4% в 2019"),
    ("2018-07-27", 2.5, 3.75, 4.25, 4.0, "2018: 3,5–4%; временное превышение 4% в 2019 (принято 4,25)"),
    ("2018-10-26", 2.5, 4.0, 5.25, 4.0, "2018: 3,8–4,2%; 2019: 5,0–5,5%; 4% в 2020"),
]


def load_cbr_forecasts() -> pd.DataFrame:
    pdf = pd.read_csv(RAW / "cbr_inflation_forecasts.csv", parse_dates=["date"])
    pr = pd.DataFrame(PRESS_RELEASE_FORECASTS, columns=["date", "fact_prev_year", "fc_y", "fc_y1", "fc_y2", "raw"])
    pr["date"] = pd.to_datetime(pr["date"])
    pr["source"] = "пресс-релиз"
    out = pd.concat([pr, pdf], ignore_index=True).sort_values("date")
    return out.drop_duplicates("date").reset_index(drop=True)  # один прогноз бывает выложен по двум ссылкам


def last_known_month(d: pd.Timestamp) -> pd.Period:
    m = pd.Period(d, "M") - 1
    return m if d.day >= CPI_PUBLISH_DAY else m - 1


def cbr_mix_path(headline: pd.Series, m_last: pd.Period, cpi_last: float, core_last: float,
                 fade_until: pd.Period) -> pd.Series:
    """ЦБ публикует прогноз только общего ИПЦ. Инфляция правила КПМ (0,75·БИПЦ + 0,25·ИПЦ) на прогнозе ЦБ:
    ИПЦ ЦБ − 0,75·спред, где спред (ИПЦ − БИПЦ) от последнего факта линейно сходится к нулю к fade_until
    (к концу следующего года ЦБ ожидает обе инфляции у цели)."""
    k = np.array([(p - m_last).n for p in headline.index])
    spread = (cpi_last - core_last) * np.clip(1 - k / max((fade_until - m_last).n, 1), 0, 1)
    return headline - W_CORE * spread


def cbr_headline_path(d: pd.Timestamp, pi_last: float, m_last: pd.Period, fc: pd.Series,
                      until: pd.Period) -> pd.Series:
    """Помесячный ИПЦ г/г по прогнозу ЦБ: линейно от последнего факта через прогнозы на декабри, дальше — 4%."""
    pts = {m_last: pi_last}
    for k, col in enumerate(["fc_y", "fc_y1", "fc_y2", "fc_y3"]):
        dec = pd.Period(f"{d.year + k}-12", "M")
        if col in fc and dec > m_last and not pd.isna(fc[col]):
            pts[dec] = fc[col]
    pts[max(pts) + 12] = TARGET
    idx = pd.period_range(m_last, max(until, max(pts)), freq="M")
    return pd.Series(np.interp([p.ordinal for p in idx], [p.ordinal for p in pts], list(pts.values())), index=idx)


def expected_inflation(d: pd.Timestamp, cpi_last: float, core_last: float, m_last: pd.Period,
                       fc: pd.Series) -> tuple[float, float, pd.Period]:
    """Ожидаемая инфляция г/г в середине квартала t+3 по прогнозу ЦБ: (0,75·БИПЦ + 0,25·ИПЦ, ИПЦ, месяц)."""
    target = (pd.Period(d, "Q") + HORIZON_Q).asfreq("M", "s") + 1
    head = cbr_headline_path(d, cpi_last, m_last, fc, target)
    mix = cbr_mix_path(head, m_last, cpi_last, core_last, pd.Period(f"{d.year + 1}-12", "M"))
    return float(mix[target]), float(head[target]), target


def cbr_gap_series(gdp: pd.Series) -> pd.Series:
    """Разрыв выпуска ЦБ (ОНДКП-2026) + продление на кварталы после его конца по данным gdp."""
    c = pd.read_csv(RAW / "cbr_output_gap.csv")
    cbr = pd.Series(c["gap"].values, index=pd.PeriodIndex(c["quarter"], freq="Q"))
    last = cbr.index.max()
    ext = {q: cbr[last] + 100 * np.log(gdp[q] / gdp[last]) - GAP_EXT_POT_GROWTH / 4 * (q - last).n
           for q in gdp.index if q > last}
    return pd.concat([cbr, pd.Series(ext)]).sort_index() if ext else cbr


def realtime_gap(gdp: pd.Series, d: pd.Timestamp) -> tuple[float, pd.Period]:
    """Разрыв выпуска в последнем квартале, ВВП за который опубликован к дате d."""
    known = gdp[[q.end_time + pd.Timedelta(days=GDP_PUBLISH_LAG_DAYS) <= d for q in gdp.index]]
    return float(cbr_gap_series(known)[known.index[-1]]), known.index[-1]


def direction(x: pd.Series) -> pd.Series:
    return np.sign(x.where(x.abs() >= NO_CHANGE, 0.0))


def run() -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """Расчёт по заседаниям: (таблица по заседаниям, метрики, дневная ключевая ставка)."""
    kr = pd.read_csv(RAW / "keyrate_daily.csv", parse_dates=["date"]).set_index("date")["key_rate"]
    rate_on = lambda d: kr[kr.index <= d].iloc[-1]
    cpi, core, gdp = load_cpi_m(), load_core_m(), load_gdp_sa()
    gap_all = cbr_gap_series(gdp)
    fcs = load_cbr_forecasts()

    # заседания: опорные (дата прогноза ЦБ) + неопорные с NONCORE_FROM; на неопорных — последний прогноз ЦБ
    mt = pd.read_csv(RAW / "cbr_meetings.csv", parse_dates=["date"])
    extraordinary = set(mt.loc[mt["source"].str.startswith("внеочередное"), "date"])
    mt = mt["date"]
    meetings = sorted(set(fcs["date"]) | set(mt[(mt >= NONCORE_FROM) & (mt >= fcs["date"].min())]))
    rows = []
    for d in meetings:
        fc = fcs[fcs["date"] <= d].iloc[-1]
        is_core = fc["date"] == d
        m_last = last_known_month(d)
        pi_last, core_last = cpi[m_last], core[m_last]
        pi_e, pi_e_cpi, target = expected_inflation(d, pi_last, core_last, m_last, fc)
        # разрыв выпуска — в квартале заседания (как Y_GAP в правиле КПМ: текущий квартал, наукаст ЦБ);
        # берётся из ретроспективной оценки ЦБ; если квартала заседания ещё нет — ближайший известный
        # (включая предварительную оценку ВВП), как наукаст текущего квартала
        q_now = pd.Period(d, "Q")
        gap_q = min(q_now, gap_all.index.max())
        gap = float(gap_all[gap_q])
        n_nom = neutral_nominal(pd.PeriodIndex([pd.Period(d, "Q")])).iloc[0]
        rbar = n_nom - TARGET
        # ставка до решения — накануне заседания (внеочередные решения вступали в силу в тот же день)
        i_before, i_after = rate_on(d - dt.timedelta(days=1)), rate_on(d + dt.timedelta(days=7))
        pi_now_mix = W_CORE * core_last + (1 - W_CORE) * pi_last

        def rule(pi: float) -> tuple[float, float]:
            notional = rbar + pi + PHI_PI * (pi - TARGET) + PHI_Y * gap
            return notional, RHO * i_before + (1 - RHO) * notional

        notional, one_step = rule(pi_e)
        notional_cur, one_step_cur = rule(pi_now_mix)
        rows.append({
            "дата заседания": d.date(), "опорное": is_core, "дата прогноза ЦБ": fc["date"].date(),
            "тип заседания": "опорное" if is_core else ("внеочередное" if d in extraordinary else "неопорное"),
            "источник прогноза": fc["source"], "прогноз ЦБ дек/дек": fc["raw"],
            "посл. известный месяц ИПЦ": str(m_last), "ИПЦ г/г": pi_last, "БИПЦ г/г": core_last,
            "πᵉ: месяц": str(target), "πᵉ (прогноз ЦБ)": pi_e, "πᵉ ИПЦ (прогноз ЦБ)": pi_e_cpi,
            "разрыв: квартал": str(gap_q), "разрыв выпуска": gap,
            "нейтр. ставка ЦБ (ном.)": n_nom,
            "ставка до решения": i_before, "решение ЦБ": i_after,
            "правило без сглаживания": notional, "правило": one_step,
            "правило без сгл. (текущая π)": notional_cur, "правило (текущая π)": one_step_cur,
        })
    res = pd.DataFrame(rows)

    # ---- метрики
    act, prev = res["решение ЦБ"], res["ставка до решения"]
    years = pd.to_datetime(res["дата заседания"]).dt.year
    stats = []
    for sample, mask in [("все заседания", years > 0), ("без 2022 г.", years != 2022), ("2023–2026", years >= 2023)]:
        for name, col in [("Правило (прогноз ЦБ)", "правило"), ("Правило (текущая π, без прогноза)", "правило (текущая π)"),
                          ("Правило без сглаживания (прогноз ЦБ)", "правило без сглаживания"),
                          ("Наивный: ставка не меняется", "ставка до решения")]:
            e = (act - res[col])[mask]
            hit = (direction(act - prev) == direction(res[col] - prev))[mask]
            stats.append({"выборка": sample, "ряд": name, "N": int(mask.sum()), "ME (факт − правило)": e.mean(),
                          "MAE": e.abs().mean(), "RMSE": np.sqrt((e**2).mean()), "Направление угадано, %": 100 * hit.mean()})
    stats = pd.DataFrame(stats)
    return res, stats, kr


def main() -> None:
    res, stats, kr = run()

    # ---- график
    x = pd.to_datetime(res["дата заседания"])
    k = kr[kr.index >= x.min() - pd.Timedelta(days=30)]
    fig, axes = plt.subplots(2, 1, figsize=(12, 9), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    ax = axes[0]
    ax.step(k.index, k.values, where="post", color="black", lw=2, label="Ключевая ставка, факт")
    ax.plot(x, res["правило"], "o-", color="tab:blue", lw=1.5, ms=5, label="Правило (на дату заседания, прогноз ЦБ)")
    ax.plot(x, res["правило без сглаживания"], "s--", color="tab:blue", lw=0.9, ms=3, alpha=0.6,
            label="Правило без сглаживания")
    ax.plot(x, res["нейтр. ставка ЦБ (ном.)"], ":", color="grey", label="Нейтральная ставка (оценка ЦБ)")
    ax.set_ylabel("% годовых")
    ax.set_title("Правило КПМ ДДКП в реальном времени: только факт на дату заседания + прогноз ЦБ")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper left")
    ax = axes[1]
    ax.plot(x, res["ИПЦ г/г"], "o-", color="tab:orange", ms=3, label="ИПЦ г/г, последний известный")
    ax.plot(x, res["πᵉ (прогноз ЦБ)"], "o--", color="tab:red", ms=3, label="πᵉ через 3 кв. по прогнозу ЦБ")
    ax.bar(x, res["разрыв выпуска"], width=40, color="tab:purple", alpha=0.4, label="Разрыв выпуска (известный), %")
    ax.axhline(TARGET, color="grey", lw=0.8)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "backtest_realtime.png", dpi=150)
    plt.close(fig)

    with pd.ExcelWriter(OUT / "backtest_realtime.xlsx") as xw:
        res.round(2).to_excel(xw, sheet_name="По заседаниям", index=False)
        stats.round(2).to_excel(xw, sheet_name="Метрики", index=False)

    pd.set_option("display.width", 250)
    cols = ["дата заседания", "ИПЦ г/г", "πᵉ (прогноз ЦБ)", "разрыв выпуска", "нейтр. ставка ЦБ (ном.)",
            "ставка до решения", "решение ЦБ", "правило без сглаживания", "правило"]
    print(res[cols].round(2).to_string(index=False))
    print()
    print(stats.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
