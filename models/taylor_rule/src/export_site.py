"""Экспорт результатов по правилу Тейлора ЦБ для вкладки «Модели ЦБ» сайта макропрогнозов.

Пересчитывает калибровку по прогнозам ЦБ (cbr_rule_calibration), бэктест в реальном времени
(backtest_realtime) и пишет один JSON, который отдаёт сайт (маршрут /api/cbr_models).

Запуск: python src/download_data.py && python src/export_site.py
Путь к JSON: переменная окружения CBR_SITE_JSON или SITE_JSON ниже.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

import backtest_realtime as bt
import cbr_rule_calibration as cal
from taylor_rule import (SITE_ROOT, EXTRA_DEC_FORECASTS, GDP_YOY_FORECAST, GDP_YOY_PRELIM, NEUTRAL_NOMINAL, SPREAD_CONVERGE_YEAR, SPREAD_LONG_MONTHS,
                         SPREAD_NOW_MONTHS, TARGET, W_CORE, key_rate_daily, load_forecast, load_gdp_sa,
                         monthly_inflation)

GDP_SHOW_FROM = pd.Period("2019Q1", "Q")

SITE_JSON = Path(os.getenv("CBR_SITE_JSON", SITE_ROOT / "data" / "cbr_models.json"))
CALIB_SHOW_FROM = pd.Timestamp("2023-01-01")


def num(x, d: int = 2):
    if x is None or (isinstance(x, float) and math.isnan(x)) or pd.isna(x):
        return None
    return round(float(x), d)


def nums(s, d: int = 2) -> list:
    return [num(v, d) for v in s]


def months(idx) -> list[str]:
    return [p.strftime("%Y-%m") for p in idx]


def key_rate_steps(kr: pd.Series, since: str) -> dict:
    """Дневная ставка → только даты изменений (для ступенчатого графика)."""
    k = kr[kr.index >= since]
    chg = k[k.ne(k.shift())]
    chg = pd.concat([chg, k.iloc[-1:]])
    return {"x": [d.strftime("%Y-%m-%d") for d in chg.index], "y": nums(chg.values)}


def steps_daily(tab: pd.DataFrame, col: str, index: pd.DatetimeIndex) -> pd.Series:
    """Значение по заседаниям → дневной ряд (действует со следующего рабочего дня после заседания)."""
    out = pd.Series(np.nan, index=index)
    for eff, v in zip(tab["eff"], tab[col]):
        out[out.index >= eff] = v
    return out


def period_bounds(k, inp: dict) -> tuple[str, str]:
    if k == "rest":
        return inp["rest_from"].strftime("%Y-%m-%d"), f"{inp['y']}-12-31"
    return f"{k}-01-01", f"{k}-12-31"


def main() -> None:
    R = cal.run(verbose=False)
    rt, rt_stats, kr = bt.run()
    fc = load_forecast()
    # наш прогноз ставки: значения на конец месяца → по дням по календарю заседаний ЦБ; средние — по дням
    kr_daily = key_rate_daily(fc["key_rate"])
    kr_y = kr_daily.groupby(kr_daily.index.year).mean()
    infl = monthly_inflation(fc)

    # ---- шаг 3: наш прогноз vs правило
    # правило по заседаниям ЦБ (инфляция 0,75·БИПЦ + 0,25·ИПЦ, как в КПМ): при нашем прогнозе и при прогнозе ЦБ
    o = R["ours"][cal.MEASURE]
    last_inp, last_cal = R["last_inp"], R["last_cal"]
    M = R["meet"]
    om, od, cm, cd = M["ours"], M["ours_daily"], M["cbr"], M["cbr_daily"]
    end_day = od.index.max()
    skb_after = lambda e: num(kr_daily[min(e, end_day)])
    cbr_at = lambda e: num(cd["rule"][min(e, end_day)])
    # оценка по инфляции и разрыву ВВП: нейтральная (долгосрочная, без надбавки ЦБ) + 2,5·(πᵉ − 4) + 0,5·ŷ — без инерции
    om = om.assign(level=om["notional"] - (om["rbar"] - last_inp["rbar_lr"]))
    ours = {
        "our_kr_daily": key_rate_steps(kr_daily[kr_daily.index >= R["kr"].index.max()], "1900-01-01"),
        "our_kr_changes": [{"date": e.strftime("%d.%m.%Y"), "v": num(v)} for e, v in kr_daily.attrs["changes"]],
        "rule_steps": key_rate_steps(pd.concat([R["kr"].iloc[-1:], od["rule"]]), "1900-01-01"),
        "rule_pts": {"x": [d.strftime("%Y-%m-%d") for d in om["date"]], "y": nums(om["rule"])},
        "notional_pts": {"x": [d.strftime("%Y-%m-%d") for d in om["date"]], "y": nums(om["level"])},
        "cbr_rule_steps": key_rate_steps(cd["rule"], "1900-01-01"),
        "cbr_rule_pts": {"x": [d.strftime("%Y-%m-%d") for d in cm["date"]], "y": nums(cm["rule"])},
        "cbr_avgs": [{"from": a, "to": b, "v": num(t), "lo": num(last_inp["ranges"][k][0]), "hi": num(last_inp["ranges"][k][1])}
                     for k, t in zip(last_cal["keys"], last_cal["targets"]) for a, b in [period_bounds(k, last_inp)]],
        "cbr_date": R["last_d"].strftime("%d.%m.%Y"),
    }
    ours_meet = [{"date": r["date"].strftime("%d.%m.%Y"), "our": skb_after(r["eff"]), "rule": num(r["rule"]),
                  "before": num(r["before"]), "cbr_rule": cbr_at(r["eff"]), "pi_e": num(r["pi_e"]), "gap": num(r["gap"]),
                  "rbar": num(r["rbar"]), "notional": num(r["level"])} for _, r in om.iterrows()]
    years = sorted({d.year for d in od.index if (od.index.year == d.year).sum() >= 365})
    cbr_by_year = {k: t for k, t in zip(last_cal["keys"], last_cal["targets"]) if k != "rest"}
    ymean = lambda x, y: num(x[x.index.year == y].mean())
    notional_daily = steps_daily(om, "level", od.index)
    ours_y = [{"y": y, "our": num(kr_y.get(y)), "rule": ymean(od["rule"], y), "cbr_rule": ymean(cd["rule"], y),
               "notional": ymean(notional_daily, y),
               "cbr": num(cbr_by_year.get(y)), "cbr_lo": num(last_inp["ranges"].get(y, (None, None))[0]),
               "cbr_hi": num(last_inp["ranges"].get(y, (None, None))[1])} for y in years]

    # ---- декомпозиция ставки по правилу на прогнозе СКБ (по заседаниям) — буквально по формуле:
    # ставка = ρ·ставка до заседания + (1−ρ)·[нейтральная 8% + инфляция 2,5·(πᵉ − 4) + надбавка ЦБ (r̄ − 4) + разрыв ВВП 0,5·ŷ],
    # ρ = 0,75 за квартал (≈0,87–0,88 на шаг между заседаниями в 6–7 недель)
    neutral = last_inp["rbar_lr"] + TARGET
    w = 1 - om["rho"]
    comp = pd.DataFrame({
        "prev": om["rho"] * om["before"], "neutral": w * neutral,
        "inflation": w * (1 + cal.PHI_PI) * (om["pi_e"] - TARGET), "premium": w * (om["rbar"] - last_inp["rbar_lr"]),
        "gap": w * cal.PHI_Y * om["gap"], "rho": om["rho"], "before": om["before"], "rule": om["rule"]})
    assert np.allclose(comp[["prev", "neutral", "inflation", "premium", "gap"]].sum(axis=1), comp["rule"])
    comp_daily = pd.DataFrame(np.nan, index=od.index, columns=comp.columns)
    first = om["eff"].iloc[0]
    for eff, row in zip(om["eff"], comp.itertuples(index=False)):
        comp_daily.loc[comp_daily.index >= eff] = list(row)
    comp_daily = comp_daily[comp_daily.index >= first]
    keys = ["prev", "neutral", "inflation", "premium", "gap", "rho", "before", "rule"]
    decomp = {"neutral": num(neutral), "i0": num(R["kr"].iloc[-1]), "rows":
              [{"label": r["date"].strftime("%d.%m.%y"), "pi_e": num(r["pi_e"]), **{k: num(c[k]) for k in keys},
                "our": skb_after(r["eff"])} for (_, r), (_, c) in zip(om.iterrows(), comp.iterrows())]
              + [{"label": f"{y}, среднее", "is_year": True, **{k: ymean(comp_daily[k], y) for k in keys},
                  "our": num(kr_y.get(y))} for y in years]}

    # ---- чувствительность правила при прогнозе СКБ: ближайшее заседание и средняя за год (по дням)
    gdp_sa = load_gdp_sa()
    y_s = years[0] if years else None
    def scen(label, group, **kw):
        t, d = cal.our_meeting_rule(last_inp, last_cal, gdp_sa, R["kr"], **kw)
        return {"label": label, "group": group, "next": num(t["rule"].iloc[0]), "year": ymean(d["rule"], y_s)}
    sens = [scen("Базовый расчёт", "base"),
            scen("Без надбавки ЦБ (нейтральная сразу 8%)", "premium", premium=False),
            scen("Нейтральная реальная 3,5% (номинальная 7,5%)", "neutral", d_neutral=-0.5),
            scen("Нейтральная реальная 4,5% (номинальная 8,5%)", "neutral", d_neutral=0.5),
            scen("БИПЦ на прогнозе −0,5 п.п.", "core", d_core=-0.5),
            scen("БИПЦ на прогнозе +0,5 п.п.", "core", d_core=0.5),
            scen("Разрыв ВВП −1 п.п.", "gap", d_gap=-1.0),
            scen("Разрыв ВВП +1 п.п.", "gap", d_gap=1.0)]
    # квартальная версия (как в КПМ: средняя ставка за квартал, ρ = 0,75 на квартал)
    X = cal.our_inputs(last_inp, gdp_sa)
    q_now = pd.Period(R["kr"].index.max(), "Q")
    q_end = pd.Period(f"{y_s}Q4", "Q") if y_s else q_now
    qs_skb = cal.quarterly_rule(R["kr"], q_now, q_end, X["pi"], X["gap_q"], last_inp["rbar_lr"], last_cal["r0"],
                                last_cal["lam"], last_inp["rest_from"])
    q_cbr0 = pd.Period(last_inp["d"], "Q")
    qs_cbr = cal.quarterly_rule(R["kr"], q_cbr0, q_end, last_inp["pi"], last_inp["gap"].groupby(last_inp["gap"].index.asfreq("Q")).mean(),
                                last_inp["rbar_lr"], last_cal["r0"], last_cal["lam"], last_inp["rest_from"])
    qavg = lambda q: num(q[q.index.year == y_s].mean())
    daily_q = lambda x: x.groupby(x.index.to_period("Q")).mean()
    quarterly = {"year": y_s, "skb": qavg(qs_skb), "cbr": qavg(qs_cbr),
                 "rows": [{"q": f"{q.quarter} кв. {q.year}", "skb_q": num(v), "skb_m": num(daily_q(od["rule"]).get(q)),
                           "cbr_q": num(qs_cbr.get(q)), "cbr_m": num(daily_q(cd["rule"]).get(q)),
                           "our": num(daily_q(kr_daily).get(q))} for q, v in qs_skb.items()]}
    sens.append({"label": "Квартальная версия правила (как в КПМ)", "group": "quarterly", "next": None, "year": quarterly["skb"]})

    # ---- конструктор на сайте: базовые ряды, из которых браузер пересчитывает правило при своих ИПЦ/БИПЦ/ВВП
    infl_b = monthly_inflation(fc)
    sim_m = pd.period_range("2025-01", f"{y_s + 1}-12", freq="M")
    tail_m = pd.period_range(infl_b["cpi"].index.max() + 1, sim_m.max(), freq="M")
    ext = lambda x: pd.concat([x, pd.Series(x.iloc[-1] + (TARGET - x.iloc[-1]) * np.minimum(np.arange(1, len(tail_m) + 1) / cal.TAIL_MONTHS, 1),
                                            index=tail_m)]) if len(tail_m) else x
    cpi_b, core_b = ext(infl_b["cpi"]).reindex(sim_m), ext(infl_b["core"]).reindex(sim_m)
    gp_b = cal.our_gdp_path(gdp_sa, fc["gdp_growth"], last_inp["g_pot"], pd.Timestamp(dt.date.today()), fc["key_rate"].index.max())
    lvl = gp_b["level"]
    sim_years = list(range(sim_m.min().year + 1, sim_m.max().year + 1))
    gdp_known = lvl[lvl.index <= gdp_sa.index.max()]  # факт и предварительные оценки (load_gdp_sa)
    yavg = lambda y: lvl[lvl.index.year == y].mean()
    vcbr = pd.read_csv(Path(cal.RAW) / "cbr_inflation_forecasts.csv", parse_dates=["date"]).iloc[-1]
    sim = {
        "months": [m.strftime("%Y-%m") for m in sim_m], "cpi": nums(cpi_b, 4), "core": nums(core_b, 4),
        "cpi_last": infl_b["cpi_act"].index.max().strftime("%Y-%m"), "core_last": infl_b["core_act"].index.max().strftime("%Y-%m"),
        "years": sim_years, "gdp_years": [y for y in sim_years if y <= fc["key_rate"].index.max().year],
        "gdp_q": {f"{q.year}Q{q.quarter}": round(float(v), 3) for q, v in gdp_known[gdp_known.index.year >= gp_b["q0"].year - 1].items()},
        "gdp_growth": {str(y): num(100 * (yavg(y) / yavg(y - 1) - 1), 4) for y in sim_years if y <= lvl.index.max().year},
        "gap0": num(gp_b["gap0"], 4), "q0": f"{gp_b['q0'].year}Q{gp_b['q0'].quarter}", "g_pot": num(last_inp["g_pot"], 1),
        "rbar_lr": num(last_inp["rbar_lr"]), "i0": num(R["kr"].iloc[-1]),
        "start": (R["kr"].index.max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), "end": end_day.strftime("%Y-%m-%d"),
        "meetings": [{"date": r["date"].strftime("%Y-%m-%d"), "eff": r["eff"].strftime("%Y-%m-%d"), "rho": round(float(r["rho"]), 5),
                      "q": f"{pd.Period(r['date'], 'Q').year}Q{pd.Period(r['date'], 'Q').quarter}",
                      "prem": round(float(r["rbar"] - last_inp["rbar_lr"]), 4), "base": num(r["rule"])} for _, r in om.iterrows()],
        "cbr_preset": {"date": vcbr["date"].strftime("%d.%m.%Y"),
                       "cpi": {str(int(vcbr["date"].year) + i): num(vcbr[c]) for i, c in enumerate(["fc_y", "fc_y1", "fc_y2"]) if pd.notna(vcbr[c])},
                       "gdp": {str(int(vcbr["date"].year) + i): num(vcbr[c]) for i, c in enumerate(["gdp_y", "gdp_y1", "gdp_y2"]) if pd.notna(vcbr[c])}},
    }
    # самопроверка: из выгруженных рядов браузер должен получить ту же траекторию, что и Python
    _q = lambda s: f"{s.year}Q{s.quarter}"
    _mix = W_CORE * core_b + (1 - W_CORE) * cpi_b
    _pq = _mix.groupby(_mix.index.asfreq("Q")).mean()
    for r in om.itertuples():
        assert abs(_pq[pd.Period(r.date, "Q") + 3] - r.pi_e) < 1e-6, ("πᵉ", r.date)

    # ---- шаг 1: калибровка по прогнозам ЦБ
    vintages = []
    for d, (inp, c) in sorted(R["cal_store"].items()):
        if d < CALIB_SHOW_FROM:
            continue
        path = c["path"][c["path"].index.year <= inp["y"] + 3]
        base, _ = cal.simulate(inp["i_new"], path.index[0], inp["until"], inp["pi"], inp["gap"], inp["rbar_lr"],
                               inp["rbar_lr"], c["lam"])
        base = base[base.index <= path.index.max()]
        periods = []
        for k, t, f, n in zip(c["keys"], c["targets"], c["fit"], c["no_adj"]):
            a, b = period_bounds(k, inp)
            periods.append({"label": "остаток " + str(inp["y"]) if k == "rest" else str(k), "from": a, "to": b,
                            "cbr": num(t), "cbr_lo": num(inp["ranges"][k][0]), "cbr_hi": num(inp["ranges"][k][1]),
                            "rule": num(n), "rule_prem": num(f)})
        vintages.append({
            "date": d.strftime("%d.%m.%Y"), "i_new": num(inp["i_new"]),
            "pi_e": num(inp["pi"][pd.Period(inp["rest_from"], "M") + cal.HORIZON_M]), "gap0": num(inp["gap0"]),
            "g_pot": num(inp["g_pot"]), "neutral": num(inp["rbar_lr"] + TARGET),
            "premium_rest": num(periods[0]["rule_prem"] - periods[0]["rule"]),
            "rmse_rule": num(np.sqrt(np.mean((c["no_adj"] - c["targets"]) ** 2))), "rmse_prem": num(c["rmse"]),
            "periods": periods, "x": months(path.index), "path_prem": nums(path.values), "path_rule": nums(base.values),
        })

    # ---- бэктест в реальном времени
    realtime = [{"date": pd.Timestamp(r["дата заседания"]).strftime("%Y-%m-%d"), "src": r["источник прогноза"],
                 "is_core": bool(r["опорное"]), "kind": r["тип заседания"], "fc_date": pd.Timestamp(r["дата прогноза ЦБ"]).strftime("%d.%m.%Y"),
                 "cbr_fc": r["прогноз ЦБ дек/дек"], "m_last": r["посл. известный месяц ИПЦ"], "cpi": num(r["ИПЦ г/г"]),
                 "pi_e": num(r["πᵉ (прогноз ЦБ)"]), "pi_e_cpi": num(r["πᵉ ИПЦ (прогноз ЦБ)"]),
                 "core": num(r["БИПЦ г/г"]), "gap": num(r["разрыв выпуска"]),
                 "gap_q": r["разрыв: квартал"], "neutral": num(r["нейтр. ставка ЦБ (ном.)"]),
                 "before": num(r["ставка до решения"]), "decision": num(r["решение ЦБ"]),
                 "notional": num(r["правило без сглаживания"]), "rule": num(r["правило"]),
                 "rule_cur": num(r["правило (текущая π)"])} for _, r in rt.iterrows()]
    rt_metrics = [{"sample": s["выборка"], "series": s["ряд"], "n": int(s["N"]), "me": num(s["ME (факт − правило)"]),
                   "mae": num(s["MAE"]), "rmse": num(s["RMSE"]), "hit": num(s["Направление угадано, %"], 0)}
                  for _, s in rt_stats.iterrows()]

    # ---- инфляция: факт за 3 года + наш прогноз
    cpi_m, core_m = infl["cpi"], infl["core"]
    start = infl["cpi_act"].index.max() - 35
    idx = pd.period_range(start, cpi_m.index.max(), freq="M")
    last_act = infl["cpi_act"].index.max()
    inflation = {"x": months(idx), "cpi": nums(cpi_m.reindex(idx)), "core": nums(core_m.reindex(idx)),
                 "mix": nums((W_CORE * core_m + (1 - W_CORE) * cpi_m).reindex(idx)),
                 "last_actual": last_act.strftime("%Y-%m"), "core_src": infl["core_src"],
                 "extra_dec": {str(y): v for y, v in EXTRA_DEC_FORECASTS.items()},
                 "spread_now": num(infl["spread"].attrs.get("now")), "spread_long": num(infl["spread"].attrs.get("long")),
                 "spread_converge_year": SPREAD_CONVERGE_YEAR,
                 "spread_now_months": SPREAD_NOW_MONTHS, "spread_long_years": SPREAD_LONG_MONTHS // 12}

    # ---- ВВП: рост г/г и разрыв выпуска (факт — оценка в реальном времени, прогноз — путь, который идёт в правило)
    gdp_act = load_gdp_sa()
    gap_hist = bt.cbr_gap_series(gdp_act)  # оценка ЦБ (ОНДКП-2026) + продление по факту
    end_q = o.index.max().asfreq("Q")
    gp = cal.our_gdp_path(gdp_act, fc["gdp_growth"], last_inp["g_pot"], pd.Timestamp(dt.date.today()),
                          end_q.asfreq("M", "e"))  # тот же путь, что идёт в правило
    level = gp["level"]
    yoy = 100 * (level / level.shift(4) - 1)
    qs = pd.period_range(GDP_SHOW_FROM, end_q, freq="Q")
    is_fc = [q > gdp_act.index.max() for q in qs]
    growth = {p.year: g for p, g in fc["gdp_growth"].items()}
    gdp = {"q": [f"{q.year} Q{q.quarter}" for q in qs], "x": [q.start_time.strftime("%Y-%m-%d") for q in qs],
           "yoy": nums(yoy.reindex(qs)), "is_fc": is_fc,
           "gap": [num(gp["gap_q"].get(q) if f else gap_hist.get(q)) for q, f in zip(qs, is_fc)],
           "last_actual": f"{gdp_act.index.max().year} Q{gdp_act.index.max().quarter}",
           "g_pot": num(last_inp["g_pot"], 1), "cbr_gap_until": "2025 Q2", "ext_pot": bt.GAP_EXT_POT_GROWTH,
           "prelim": {k.replace("Q", " Q"): v for k, v in GDP_YOY_PRELIM.items()},
           "manual": {k.replace("Q", " Q"): v for k, v in GDP_YOY_FORECAST.items()},
           "growth_fc": {str(k): num(v, 1) for k, v in growth.items() if k >= gdp_act.index.max().year},
           "annual": {str(y): num(100 * (level[level.index.year == y].mean() / level[level.index.year == y - 1].mean() - 1), 1)
                      for y in range(gdp_act.index.max().year, end_q.year + 1)}}

    # ---- пример расчёта: последнее заседание (реальное время)
    ex = realtime[-1]
    example = {**ex, "rbar": num(ex["neutral"] - TARGET),
               "infl_term": num(cal.PHI_PI * (ex["pi_e"] - TARGET)), "gap_term": num(cal.PHI_Y * ex["gap"])}

    nxt = ours_meet[0]
    # ИПЦ и БИПЦ г/г, средние за квартал через 3 кв. от квартала ближайшего заседания (входят в правило 0,25 / 0,75)
    q_next = pd.Period(om["date"].iloc[0], "Q") + 3
    q3 = lambda x: num(x[x.index.asfreq("Q") == q_next].mean())
    # ближайшее заседание при макропрогнозе ЦБ — от фактической ставки сегодня (как и для СКБ)
    cal_days = sorted(pd.to_datetime(pd.read_csv(cal.RAW / "cbr_meeting_calendar.csv")["date"]))
    today_kr = R["kr"].index.max()
    cbr_now, _ = cal.meeting_rule_path(
        float(R["kr"].iloc[-1]), max(d for d in cal_days if d <= today_kr), [d for d in cal_days if d > today_kr][:1],
        last_inp["pi"], last_inp["gap"].groupby(last_inp["gap"].index.asfreq("Q")).mean(), last_inp["rbar_lr"],
        last_cal["r0"], last_cal["lam"], last_inp["rest_from"], today_kr + pd.Timedelta(days=1), end_day)
    y27 = next((r for r in ours_y if r["y"] == 2027), ours_y[0] if ours_y else {})
    out = {
        "generated": dt.datetime.now().strftime("%d.%m.%Y %H:%M"),
        "params": {"rho": cal.RHO_Q, "phi_pi": cal.PHI_PI, "phi_y": cal.PHI_Y, "target": TARGET, "w_core": W_CORE,
                   "horizon_q": 3, "lam": R["lam_star"], "half_life": num(np.log(0.5) / np.log(R["lam_star"]), 1),
                   "neutral_history": [{"from": a, "v": v} for a, v in NEUTRAL_NOMINAL]},
        "kpi": {"key_rate": num(kr.iloc[-1]), "key_rate_date": kr.index[-1].strftime("%d.%m.%Y"),
                "next_date": nxt["date"], "next_rule": nxt["rule"], "next_cbr_rule": nxt["cbr_rule"],
                "next_our": nxt["our"], "next_cbr_now": num(cbr_now["rule"].iloc[0]),
                "next_cbr_cpi": q3(last_inp["cpi"]), "next_cbr_core": q3((last_inp["pi"] - (1 - W_CORE) * last_inp["cpi"]) / W_CORE),
                "next_cpi": q3(infl["cpi"]), "next_core": q3(infl["core"]), "next_no_prem": sens[1]["next"], "year_notional": y27.get("notional"), "year": y27.get("y"), "year_our": y27.get("our"),
                "year_rule": y27.get("rule"), "year_cbr_rule": y27.get("cbr_rule"), "year_cbr": y27.get("cbr"),
                "year_cbr_lo": y27.get("cbr_lo"), "year_cbr_hi": y27.get("cbr_hi"),
                "cbr_date": ours["cbr_date"], "stance": num(ex["decision"] - ex["rule"]), "stance_date": ex["date"]},
        "key_rate": key_rate_steps(kr, "2018-01-01"),
        "ours": ours, "ours_meet": ours_meet, "ours_y": ours_y, "decomp": decomp, "sens": sens, "quarterly": quarterly, "sim": sim,
        "vintages": vintages, "realtime": realtime, "rt_metrics": rt_metrics,
        "inflation": inflation, "gdp": gdp, "example": example,
    }
    SITE_JSON.parent.mkdir(parents=True, exist_ok=True)
    SITE_JSON.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"Записано: {SITE_JSON} ({SITE_JSON.stat().st_size / 1024:.0f} КБ)")


if __name__ == "__main__":
    main()
