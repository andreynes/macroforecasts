"""Правило КПМ ДДКП так, как его применяет Банк России: калибровка на собственных прогнозах ЦБ.

Шаг 1. Для каждого среднесрочного прогноза ЦБ (с апреля 2021 г. ЦБ публикует траекторию средней
ключевой ставки) прогоняем правило на входах самого ЦБ:
  * πᵉ — инфляция через 3 кв. по прогнозу ЦБ (дек/дек на текущий и следующие годы, далее 4%);
  * разрыв выпуска — известный на дату (односторонний HP), далее меняется на (рост ВВП по прогнозу ЦБ −
    потенциальный рост), потенциальный рост = прогноз ЦБ на последний год горизонта;
  * сглаживание и реакции — из кода КПМ (ρ = 0,75 в квартал, φπ = 1,5, φy = 0,5).
Неизвестна только нейтральная ставка. ЦБ публикует долгосрочную (8%), а в КПМ она меняется во времени.
Задаём r̄ₖ = r̄_LR + (r̄₀ − r̄_LR)·λᵏ и подбираем r̄₀ и λ так, чтобы правило воспроизвело
опубликованные ЦБ средние ставки (остаток текущего года и следующие годы).
→ подразумеваемая краткосрочная нейтральная ставка ЦБ и скорость её схождения к долгосрочной.

Шаг 2. Входы как у ЦБ: прогноз ЦБ по инфляции и ВВП, оценка потенциального роста ЦБ, остаток года по сноске.

Шаг 3. Наш прогноз: те же параметры и нейтральная ставка из последнего прогноза ЦБ, а инфляция и ВВП —
наши (FORECAST_FILE). Траектория по правилу сравнивается с нашим прогнозом ключевой ставки.

Модель считается помесячно: месячное сглаживание ρ_м = 0,75^(1/3) (та же скорость, что 0,75 в квартал).

Запуск: python src/download_data.py && python src/cbr_rule_calibration.py
Результат: outputs/cbr_calibration.xlsx, outputs/cbr_calibration*.png, outputs/our_forecast_vs_cbr_rule.png
"""
from __future__ import annotations

import datetime as dt

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from backtest_realtime import GDP_PUBLISH_LAG_DAYS, cbr_headline_path, cbr_mix_path, last_known_month, realtime_gap
from taylor_rule import (GDP_YOY_FORECAST, OUT, RAW, TARGET, W_CORE, extend_gdp, key_rate_daily, load_core_m, load_cpi_m, load_forecast, load_gdp_sa,
                         monthly_inflation, neutral_nominal)

MEASURE = "mix"  # инфляция правила: 0,75·БИПЦ + 0,25·ИПЦ, как в КПМ ДДКП

RHO_Q, PHI_PI, PHI_Y = 0.75, 1.5, 0.5
RHO_M = RHO_Q ** (1 / 3)
HORIZON_M = 9                       # инфляция через 3 квартала
LAMBDA_GRID = np.round(np.arange(0.30, 0.9951, 0.01), 3)    # месячная инерция надбавки к нейтральной ставке
R0_BOUNDS = (-5.0, 25.0)
CALIB_FROM = pd.Timestamp("2023-01-01")  # основной режим; 2021–2022 считаются, но в выводах отдельно


# ---------------------------------------------------------------- пути входов

def gap_path(gap0: float, q0: pd.Period, growth: dict[int, float], g_pot: float, until: pd.Period) -> pd.Series:
    """Помесячный разрыв: от известного значения (последний месяц квартала q0) меняется на (рост − потенциал)/12."""
    start = q0.asfreq("M", "e")
    idx = pd.period_range(start, until, freq="M")
    vals, g = [], gap0
    for p in idx:
        if p > start:
            g += (growth.get(p.year, g_pot) - g_pot) / 12
        vals.append(g)
    return pd.Series(vals, index=idx)


def simulate(i0: float, start: pd.Period, until: pd.Period, pi: pd.Series, gap: pd.Series,
             rbar_lr: float, r0: float, lam: float, k0: int = 0) -> tuple[pd.Series, pd.Series]:
    """Месячная траектория ставки по правилу с месяца start; i0 — ставка в месяце start−1.
    r̄ₖ = r̄_LR + (r0 − r̄_LR)·λ^(k0+k), k — месяцев от start. Возвращает (ставка, ставка без сглаживания)."""
    idx = pd.period_range(start, until, freq="M")
    rates, notional, i = [], [], i0
    for k, p in enumerate(idx):
        pe = pi[p + HORIZON_M]
        rbar = rbar_lr + (r0 - rbar_lr) * lam ** (k0 + k)
        n = rbar + pe + PHI_PI * (pe - TARGET) + PHI_Y * gap[p]
        i = RHO_M * i + (1 - RHO_M) * n
        rates.append(i)
        notional.append(n)
    return pd.Series(rates, index=idx), pd.Series(notional, index=idx)


# ---------------------------------------------------------------- шаг 1–2: калибровка по прогнозам ЦБ

def vintage_inputs(v: pd.Series, cpi: pd.Series, core: pd.Series, gdp: pd.Series, kr: pd.Series) -> dict:
    d = pd.Timestamp(v["date"])
    y = d.year
    m_last = last_known_month(d)
    until = pd.Period(f"{y + 4}-12", "M")
    # инфляция правила КПМ: 0,75·БИПЦ + 0,25·ИПЦ; прогноз ЦБ по ИПЦ переводится через сходящийся спред
    head = cbr_headline_path(d, cpi[m_last], m_last, v, until + HORIZON_M)
    pi = cbr_mix_path(head, m_last, cpi[m_last], core[m_last], pd.Period(f"{y + 1}-12", "M"))
    gap0, q_gap = realtime_gap(gdp, d)
    growth = {y - 1: v["gdp_prev"], y: v["gdp_y"], y + 1: v["gdp_y1"], y + 2: v["gdp_y2"], y + 3: v["gdp_y3"]}
    growth = {k: g for k, g in growth.items() if not np.isnan(g)}
    g_pot = growth[max(growth)]  # потенциальный рост = прогноз на последний год горизонта
    gap = gap_path(gap0, q_gap, growth, g_pot, until)
    rest_from = pd.Timestamp(v["kr_rest_from"])
    i_new = kr[kr.index <= rest_from].iloc[-1]  # ставка, установленная на заседании
    targets = {"rest": v["kr_rest"], y + 1: v["kr_y1"], y + 2: v["kr_y2"], y + 3: v["kr_y3"]}
    targets = {k: t for k, t in targets.items() if not np.isnan(t)}
    # диапазон прогноза ЦБ (для графиков; калибруем по середине)
    rng = {k: (v.get(f"{c}_lo", np.nan), v.get(f"{c}_hi", np.nan))
           for k, c in [("rest", "kr_rest"), (y + 1, "kr_y1"), (y + 2, "kr_y2"), (y + 3, "kr_y3")] if k in targets}
    return dict(d=d, y=y, until=until, pi=pi, cpi=head, gap=gap, gap0=gap0, q_gap=q_gap, g_pot=g_pot, i_new=i_new,
                rest_from=rest_from, targets=targets, ranges=rng, m_last=m_last,
                rbar_lr=neutral_nominal(pd.PeriodIndex([pd.Period(d, "Q")])).iloc[0] - TARGET)


def period_averages(path: pd.Series, inp: dict) -> dict:
    """Средние ставки, сопоставимые с публикацией ЦБ: остаток текущего года и полные следующие годы."""
    m0 = pd.Period(inp["rest_from"], "M")
    days_m0 = (m0.end_time.normalize() - inp["rest_from"]).days + 1
    rest_months = path[(path.index > m0) & (path.index.year == inp["y"])]
    w = np.r_[days_m0, [p.days_in_month for p in rest_months.index]]
    rest = np.average(np.r_[inp["i_new"], rest_months.values], weights=w)
    out = {"rest": rest}
    for year in range(inp["y"] + 1, inp["y"] + 4):
        out[year] = path[path.index.year == year].mean()
    return out


def _avgs(inp: dict, r0: float, lam: float) -> np.ndarray:
    start = pd.Period(inp["rest_from"], "M") + 1
    path, _ = simulate(inp["i_new"], start, inp["until"], inp["pi"], inp["gap"], inp["rbar_lr"], r0, lam)
    a = period_averages(path, inp)
    return np.array([a[k] for k in inp["targets"]])


def sse_by_lambda(inp: dict) -> pd.DataFrame:
    """Для каждой λ — оптимальная r0 (средние линейны по r0, МНК в явном виде) и сумма квадратов ошибок."""
    tgt = np.array(list(inp["targets"].values()))
    rows = []
    for lam in LAMBDA_GRID:
        a0, a1 = _avgs(inp, 0.0, lam), _avgs(inp, 1.0, lam)
        b = a1 - a0
        r0 = float(np.clip(np.dot(b, tgt - a0) / np.dot(b, b), *R0_BOUNDS))
        rows.append({"lam": lam, "r0": r0, "sse": float(np.sum((a0 + b * r0 - tgt) ** 2))})
    return pd.DataFrame(rows).set_index("lam")


def calibrate(inp: dict, grid: pd.DataFrame, lam: float) -> dict:
    """Подгонка r0 при общей для всех прогнозов λ."""
    r0, sse = grid.loc[lam, "r0"], grid.loc[lam, "sse"]
    keys = list(inp["targets"])
    start = pd.Period(inp["rest_from"], "M") + 1
    path, notional = simulate(inp["i_new"], start, inp["until"], inp["pi"], inp["gap"], inp["rbar_lr"], r0, lam)
    return dict(r0=r0, lam=lam, rmse=np.sqrt(sse / len(keys)), keys=keys,
                targets=np.array([inp["targets"][k] for k in keys]), fit=_avgs(inp, r0, lam),
                no_adj=_avgs(inp, inp["rbar_lr"], lam),  # с долгосрочной нейтральной ЦБ (r0 = r̄_LR)
                path=path, notional=notional)


# ---------------------------------------------------------------- шаг 3: наш прогноз

TAIL_MONTHS = 12  # после конца нашего прогноза инфляция идёт к 4% по прямой за столько месяцев; None — держим последнее значение


def our_gdp_path(gdp: pd.Series, growth: pd.Series, g_pot: float, d: pd.Timestamp, until: pd.Period) -> dict:
    """Наш прогноз ВВП и разрыва выпуска, согласованные между собой.

    Уровень ВВП (SA) продлевается так, чтобы среднегодовой рост совпал с нашим годовым прогнозом (extend_gdp).
    Разрыв: известный на дату d (односторонний HP), далее меняется на рост уровня минус потенциальный рост:
    ŷ_q = ŷ_0 + 100·ln(L_q / L_0) − g_pot·(q − q_0)/4. Помесячно — линейно между серединами кварталов."""
    # на дату расчёта нашего прогноза учитываем все уже известные кварталы, включая предварительные оценки
    gap0, q0 = realtime_gap(gdp, max(d, gdp.index.max().end_time + pd.Timedelta(days=GDP_PUBLISH_LAG_DAYS)))
    lvl = gdp.copy()
    for q, g in sorted(GDP_YOY_FORECAST.items()):  # наши ручные допущения по кварталам
        q = pd.Period(q, "Q")
        if q > lvl.index.max() and q - 4 in lvl.index:
            lvl.loc[q] = lvl[q - 4] * (1 + g / 100)
    level = extend_gdp(lvl.sort_index(), growth, until.year)
    qs = pd.period_range(q0, until.asfreq("Q"), freq="Q")
    gap_q = pd.Series([gap0 + 100 * np.log(level[q] / level[q0]) - g_pot * (q - q0).n / 4 for q in qs], index=qs)
    months = pd.period_range(q0.asfreq("M", "e"), until, freq="M")
    centers = [(q.asfreq("M", "s") + 1).ordinal for q in qs]
    gap_m = pd.Series(np.interp([m.ordinal for m in months], centers, gap_q.values), index=months)
    return {"gap_q": gap_q, "gap_m": gap_m, "level": level, "gap0": gap0, "q0": q0}


def our_forecast_rule(last: dict, last_cal: dict, gdp: pd.Series, kr: pd.Series, measure: str,
                      tail_months: int | None = TAIL_MONTHS) -> pd.DataFrame:
    fc = load_forecast()
    infl = monthly_inflation(fc)
    today = pd.Timestamp(dt.date.today())
    m_now = pd.Period(today, "M")
    until = fc["key_rate"].index.max()
    # инфляция: наш ряд (факт + прогноз); правилу нужна инфляция на 9 мес. вперёд, поэтому после конца
    # нашего прогноза ряд продлевается допущением (tail_months)
    pi = infl[measure]
    tail = pd.period_range(pi.index.max() + 1, until + HORIZON_M + 12, freq="M")
    k = np.arange(1, len(tail) + 1)
    ext = pi.iloc[-1] + (TARGET - pi.iloc[-1]) * np.minimum(k / tail_months, 1) if tail_months else np.full(len(k), pi.iloc[-1])
    pi = pd.concat([pi, pd.Series(ext, index=tail)])
    gap = our_gdp_path(gdp, fc["gdp_growth"], last["g_pot"], today, until)["gap_m"]
    # нейтральная ставка — из последнего прогноза ЦБ, продолжаем её схождение с того же места
    k0 = (m_now - pd.Period(last["rest_from"], "M")).n
    i_now = kr.iloc[-1]
    daily_kr = key_rate_daily(fc["key_rate"])
    path, notional = simulate(i_now, m_now + 1, until, pi, gap, last["rbar_lr"], last_cal["r0"], last_cal["lam"], k0=k0)
    rbar = last["rbar_lr"] + (last_cal["r0"] - last["rbar_lr"]) * last_cal["lam"] ** (k0 + np.arange(len(path)))
    return pd.DataFrame({"правило": path, "без сглаживания": notional, "πᵉ через 3 кв.": [pi[p + HORIZON_M] for p in path.index],
                         "разрыв выпуска": gap.reindex(path.index).values, "нейтр. реальная r̄": rbar,
                         # наш прогноз ставки: значения на конец месяца → по дням (по календарю ЦБ) → среднее за месяц
                         "наш прогноз КС": daily_kr.groupby(daily_kr.index.to_period("M")).mean().reindex(path.index).values})


# ---------------------------------------------------------------- правило по заседаниям ЦБ (прогноз)

DAYS_Q = 365.25 / 4


def meeting_rule_path(i0: float, prev_meeting: pd.Timestamp, meetings: list[pd.Timestamp], pi_m: pd.Series,
                      gap_q: pd.Series, rbar_lr: float, r0: float, lam: float, rest_from: pd.Timestamp,
                      start_day: pd.Timestamp, end_day: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Траектория правила по заседаниям ЦБ.

    На заседании d: ставка = ρ_d·ставка до заседания + (1−ρ_d)·[r̄ + πᵉ + 1,5·(πᵉ−4) + 0,5·ŷ],
    ρ_d = 0,75^(дней с прошлого заседания / квартал) — квартальное сглаживание КПМ на шаге между заседаниями;
    πᵉ — инфляция г/г, средняя за квартал t+3 от квартала заседания; ŷ — разрыв в квартале заседания;
    r̄ — нейтральная реальная ставка с затухающей надбавкой ЦБ. Новая ставка — со следующего рабочего дня.
    Вклады факторов (относительно нейтральной номинальной ставки) накапливаются тем же сглаживанием.
    Возвращает (таблица по заседаниям, дневные ряды: ставка и вклады)."""
    pi_q = pi_m.groupby(pi_m.index.asfreq("Q")).mean()
    neutral = rbar_lr + TARGET
    i, prev, mult = i0, prev_meeting, 1.0
    c = {"premium": 0.0, "inflation": 0.0, "gap": 0.0}
    rows = []
    for d in meetings:
        rho = RHO_Q ** ((d - prev).days / DAYS_Q)
        q = pd.Period(d, "Q")
        pe, g = float(pi_q[q + 3]), float(gap_q[q])
        rbar = rbar_lr + (r0 - rbar_lr) * lam ** (pd.Period(d, "M") - pd.Period(rest_from, "M")).n
        x = {"premium": rbar - rbar_lr, "inflation": (1 + PHI_PI) * (pe - TARGET), "gap": PHI_Y * g}
        notional = rbar + pe + PHI_PI * (pe - TARGET) + PHI_Y * g
        before, i = i, rho * i + (1 - rho) * notional
        c = {k: rho * c[k] + (1 - rho) * x[k] for k in c}
        mult *= rho
        rows.append({"date": d, "eff": d + pd.offsets.BDay(1), "rho": rho, "pi_e": pe, "gap": g, "rbar": rbar,
                     "notional": notional, "before": before, "rule": i, "c_inertia": mult * (i0 - neutral),
                     **{f"c_{k}": v for k, v in c.items()}})
        prev = d
    tab = pd.DataFrame(rows)
    parts = ["c_inertia", "c_premium", "c_inflation", "c_gap"]
    assert np.allclose(tab["rule"], neutral + tab[parts].sum(axis=1))
    days = pd.date_range(start_day, end_day, freq="D")
    daily = pd.DataFrame({"rule": i0, "c_inertia": i0 - neutral, "c_premium": 0.0, "c_inflation": 0.0, "c_gap": 0.0},
                         index=days)
    for _, r in tab.iterrows():
        daily.loc[daily.index >= r["eff"], ["rule"] + parts] = [r["rule"]] + [r[p] for p in parts]
    return tab, daily


def our_inputs(last: dict, gdp: pd.Series) -> dict:
    """Входы правила при прогнозе СКБ: инфляция 0,75·БИПЦ + 0,25·ИПЦ (факт + прогноз + хвост к цели)
    и разрыв ВВП по кварталам, согласованный с прогнозом ВВП."""
    fc = load_forecast()
    infl = monthly_inflation(fc)
    pi = infl[MEASURE]
    tail = pd.period_range(pi.index.max() + 1, fc["key_rate"].index.max() + HORIZON_M + 12, freq="M")
    k = np.arange(1, len(tail) + 1)
    pi = pd.concat([pi, pd.Series(pi.iloc[-1] + (TARGET - pi.iloc[-1]) * np.minimum(k / TAIL_MONTHS, 1), index=tail)])
    gp = our_gdp_path(gdp, fc["gdp_growth"], last["g_pot"], pd.Timestamp(dt.date.today()), fc["key_rate"].index.max())
    return {"fc": fc, "pi": pi, "gap_q": gp["gap_q"], "core_last": infl["core_act"].index.max()}


def our_meeting_rule(last: dict, last_cal: dict, gdp: pd.Series, kr: pd.Series, d_core: float = 0.0,
                     d_gap: float = 0.0, d_neutral: float = 0.0, premium: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Правило по заседаниям ЦБ при прогнозе СКБ (от текущей ставки). Сдвиги — для чувствительности:
    d_core — БИПЦ на прогнозных месяцах, п.п.; d_gap — разрыв ВВП, п.п.; d_neutral — нейтральная реальная ставка, п.п.
    (надбавка ЦБ сохраняет размер); premium=False — без надбавки ЦБ (r̄ = долгосрочная оценка сразу)."""
    X = our_inputs(last, gdp)
    pi = X["pi"] + W_CORE * d_core * (X["pi"].index > X["core_last"])
    rbar_lr = last["rbar_lr"] + d_neutral
    r0 = last_cal["r0"] + d_neutral if premium else rbar_lr
    cal = sorted(pd.to_datetime(pd.read_csv(RAW / "cbr_meeting_calendar.csv")["date"]))
    today = kr.index.max()
    end_day = X["fc"]["key_rate"].index.max().end_time.normalize()
    return meeting_rule_path(
        float(kr.iloc[-1]), max(d for d in cal if d <= today), [d for d in cal if today < d <= end_day],
        pi, X["gap_q"] + d_gap, rbar_lr, r0, last_cal["lam"], last["rest_from"], today + pd.Timedelta(days=1), end_day)


def quarterly_rule(kr: pd.Series, q_from: pd.Period, q_to: pd.Period, pi_m: pd.Series, gap_q: pd.Series,
                   rbar_lr: float, r0: float, lam: float, rest_from: pd.Timestamp) -> pd.Series:
    """Правило в исходном виде КПМ: квартальное, на средней ставке за квартал.
    i_q = 0,75·i_{q−1} + 0,25·[r̄ + πᵉ + 1,5·(πᵉ − 4) + 0,5·ŷ_q]; старт — фактическая средняя ставка за квартал q_from − 1."""
    pi_q = pi_m.groupby(pi_m.index.asfreq("Q")).mean()
    daily = kr.resample("D").ffill()
    i = float(daily[daily.index.to_period("Q") == q_from - 1].mean())
    out = {}
    for q in pd.period_range(q_from, q_to, freq="Q"):
        n = max((q.asfreq("M", "s") + 1 - pd.Period(rest_from, "M")).n, 0)  # надбавка — на средний месяц квартала
        rbar = rbar_lr + (r0 - rbar_lr) * lam ** n
        pe = float(pi_q[q + 3])
        i = RHO_Q * i + (1 - RHO_Q) * (rbar + pe + PHI_PI * (pe - TARGET) + PHI_Y * float(gap_q[q]))
        out[q] = i
    return pd.Series(out)


def meeting_paths(last: dict, last_cal: dict, gdp: pd.Series, kr: pd.Series) -> dict:
    """Правило по заседаниям ЦБ: при нашем прогнозе (от текущей ставки) и при последнем прогнозе ЦБ (от его заседания)."""
    fc = load_forecast()
    cal = sorted(pd.to_datetime(pd.read_csv(RAW / "cbr_meeting_calendar.csv")["date"]))
    end_day = fc["key_rate"].index.max().end_time.normalize()
    ours, ours_daily = our_meeting_rule(last, last_cal, gdp, kr)
    # прогноз ЦБ: от решения на его заседании, его инфляция и ВВП
    d0 = pd.Timestamp(last["d"])
    cbr, cbr_daily = meeting_rule_path(
        float(last["i_new"]), d0, [d for d in cal if d0 < d <= end_day],
        last["pi"], last["gap"].groupby(last["gap"].index.asfreq("Q")).mean(), last["rbar_lr"], last_cal["r0"],
        last_cal["lam"], last["rest_from"], pd.Timestamp(last["rest_from"]), end_day)
    return {"ours": ours, "ours_daily": ours_daily, "cbr": cbr, "cbr_daily": cbr_daily,
            "skb_daily": key_rate_daily(fc["key_rate"])}


# ---------------------------------------------------------------- main

def run(verbose: bool = True) -> dict:
    """Шаги 1–3: калибровка по всем прогнозам ЦБ и правило на нашем прогнозе."""
    kr = pd.read_csv(RAW / "keyrate_daily.csv", parse_dates=["date"]).set_index("date")["key_rate"]
    cpi, core, gdp = load_cpi_m(), load_core_m(), load_gdp_sa()
    vint = pd.read_csv(RAW / "cbr_inflation_forecasts.csv", parse_dates=["date"]).dropna(subset=["kr_rest"])

    inputs = [vintage_inputs(v, cpi, core, gdp, kr) for _, v in vint.iterrows()]
    grids = [sse_by_lambda(inp) for inp in inputs]
    # общая λ: минимум суммарной ошибки по прогнозам основного режима (с 2023 г.)
    pooled = sum(g["sse"] for inp, g in zip(inputs, grids) if inp["d"] >= CALIB_FROM)
    lam_star = float(pooled.idxmin())
    if verbose:
            print(f"Общая λ = {lam_star} в месяц (полураспад {np.log(0.5) / np.log(lam_star):.1f} мес.), "
              f"суммарная SSE {pooled.min():.2f} по {sum(inp['d'] >= CALIB_FROM for inp in inputs)} прогнозам")

    rows, cal_store = [], {}
    for inp, grid in zip(inputs, grids):
        cal = calibrate(inp, grid, lam_star)
        cal_store[inp["d"]] = (inp, cal)
        row = {"дата прогноза": inp["d"].date(), "ставка после решения": inp["i_new"],
               "πᵉ сейчас (через 3 кв.)": inp["pi"][pd.Period(inp["rest_from"], "M") + HORIZON_M],
               "разрыв (известный)": inp["gap0"], "потенц. рост ЦБ": inp["g_pot"],
               "r̄ долгоср. (ЦБ), реальн.": inp["rbar_lr"],
               "r̄₀ подразумеваемая, реальн.": cal["r0"],
               "нейтр. номинальная краткоср. (r̄₀+4)": cal["r0"] + TARGET,
               "λ в месяц": cal["lam"], "полураспад, мес.": np.log(0.5) / np.log(cal["lam"]),
               "RMSE подгонки, п.п.": cal["rmse"],
               "RMSE без надбавки, п.п.": np.sqrt(np.mean((cal["no_adj"] - cal["targets"]) ** 2))}
        # надбавка ЦБ к правилу: насколько средняя ставка ЦБ выше правила с долгосрочной нейтральной
        for k, f, n in zip(cal["keys"], cal["fit"], cal["no_adj"]):
            row[f"надбавка: {'остаток года' if k == 'rest' else k}"] = f - n
        for k, t, f, n in zip(cal["keys"], cal["targets"], cal["fit"], cal["no_adj"]):
            label = "остаток года" if k == "rest" else str(k)
            row[f"ЦБ: {label}"] = t
            row[f"правило с подобр. r̄: {label}"] = f
            row[f"правило с r̄ долгоср.: {label}"] = n
        rows.append(row)
    table = pd.DataFrame(rows)

    # ---- шаг 3
    last_d = max(cal_store)
    last_inp, last_cal = cal_store[last_d]
    # та же мера инфляции, что и в калибровке, — иначе надбавка ЦБ несопоставима
    ours = {MEASURE: our_forecast_rule(last_inp, last_cal, gdp, kr, MEASURE)}
    meet = meeting_paths(last_inp, last_cal, gdp, kr)
    return dict(kr=kr, table=table, cal_store=cal_store, lam_star=lam_star, last_d=last_d,
                last_inp=last_inp, last_cal=last_cal, ours=ours, meet=meet)


def main() -> None:
    R = run()
    kr, table, cal_store, lam_star = R["kr"], R["table"], R["cal_store"], R["lam_star"]
    last_d, last_inp, last_cal, ours = R["last_d"], R["last_inp"], R["last_cal"], R["ours"]

    # ---- график: насколько прогноз ставки ЦБ отклоняется от правила
    t = table[pd.to_datetime(table["дата прогноза"]) >= CALIB_FROM]
    x = np.arange(len(t))
    labels = pd.to_datetime(t["дата прогноза"]).dt.strftime("%m.%y")
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    ax = axes[0]
    ax.bar(x, t["надбавка: остаток года"], color="tab:red", alpha=0.8)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("Надбавка ЦБ к правилу: средняя ставка ЦБ на остаток года минус правило с долгосрочной нейтральной")
    ax.set_ylabel("п.п.")
    ax.grid(alpha=0.3, axis="y")
    ax = axes[1]
    ax.bar(x - 0.2, t["RMSE без надбавки, п.п."], width=0.4, color="tab:blue", alpha=0.7, label="правило с r̄ долгоср. ЦБ")
    ax.bar(x + 0.2, t["RMSE подгонки, п.п."], width=0.4, color="tab:red", alpha=0.7,
           label=f"правило + затухающая надбавка (полураспад {np.log(.5)/np.log(lam_star):.1f} мес.)")
    ax.set_title("Насколько правило воспроизводит опубликованные средние ставки ЦБ (RMSE по годам горизонта)")
    ax.set_ylabel("п.п.")
    ax.set_xticks(x, labels)
    ax.grid(alpha=0.3, axis="y")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "cbr_calibration_premium.png", dpi=150)
    plt.close(fig)
    (OUT / "cbr_calibration_neutral.png").unlink(missing_ok=True)

    # ---- график: последние 4 прогноза ЦБ — опубликовано vs правило
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharey=True)
    for ax, d in zip(axes.flat, sorted(cal_store)[-4:]):
        inp, cal = cal_store[d]
        p = cal["path"]
        p = p[p.index.year <= inp["y"] + 3]
        ax.plot(p.index.to_timestamp(), p.values, color="tab:red", lw=2, label="Правило + надбавка ЦБ")
        pb, _ = simulate(inp["i_new"], p.index[0], inp["until"], inp["pi"], inp["gap"], inp["rbar_lr"], inp["rbar_lr"],
                         cal["lam"])
        pb = pb[pb.index.year <= inp["y"] + 3]
        ax.plot(pb.index.to_timestamp(), pb.values, color="tab:blue", lw=1.5, ls="--", label="Правило с r̄ = долгоср. ЦБ")
        for k, tval in zip(cal["keys"], cal["targets"]):
            if k == "rest":
                a, b = inp["rest_from"], pd.Timestamp(f"{inp['y']}-12-31")
            else:
                a, b = pd.Timestamp(f"{k}-01-01"), pd.Timestamp(f"{k}-12-31")
            ax.hlines(tval, a, b, color="black", lw=3, label="Прогноз ЦБ: средняя ставка" if k == "rest" else None)
        ax.set_title(f"Прогноз ЦБ от {d:%d.%m.%Y}", fontsize=10)
        ax.grid(alpha=0.3)
    axes.flat[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "cbr_calibration_paths.png", dpi=150)
    plt.close(fig)

    # ---- график: наш прогноз vs правило ЦБ
    fig, ax = plt.subplots(figsize=(11, 5))
    hist = kr[kr.index >= pd.Timestamp("2025-01-01")]
    ax.step(hist.index, hist.values, where="post", color="black", lw=2, label="Ключевая ставка, факт")
    o = ours[MEASURE]
    xo = o.index.to_timestamp()
    ax.step(xo, o["наш прогноз КС"], where="post", color="black", lw=2, ls="--", label="Наш прогноз КС")
    ax.plot(xo, o["правило"], color="tab:red", lw=2,
            label="Правило ЦБ на нашем прогнозе (0,75·БИПЦ + 0,25·ИПЦ, наш ВВП, r̄ из прогноза ЦБ)")
    inp, cal = last_inp, last_cal
    cb = cal["path"]
    cb = cb[cb.index <= o.index.max()]
    ax.plot(cb.index.to_timestamp(), cb.values, color="tab:blue", lw=1.5, ls="--",
            label=f"Правило на входах прогноза ЦБ от {last_d:%d.%m.%Y}")
    for k, tval in zip(cal["keys"], cal["targets"]):
        a = inp["rest_from"] if k == "rest" else pd.Timestamp(f"{k}-01-01")
        b = pd.Timestamp(f"{inp['y'] if k == 'rest' else k}-12-31")
        if a <= o.index.max().end_time:
            ax.hlines(tval, a, min(b, o.index.max().end_time), color="tab:blue", lw=4, alpha=0.5,
                      label="Прогноз ЦБ: средняя ставка за период" if k == "rest" else None)
    ax.set_title("Наш прогноз ключевой ставки vs правило ЦБ, откалиброванное на последнем прогнозе ЦБ")
    ax.set_ylabel("% годовых")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper right")
    fig.tight_layout()
    fig.savefig(OUT / "our_forecast_vs_cbr_rule.png", dpi=150)
    plt.close(fig)

    with pd.ExcelWriter(OUT / "cbr_calibration.xlsx") as xw:
        table.round(2).to_excel(xw, sheet_name="Калибровка по прогнозам ЦБ", index=False)
        for m, o in ours.items():
            oo = o.copy()
            oo.index = oo.index.astype(str)
            oo.round(2).to_excel(xw, sheet_name=f"Наш прогноз ({m})")
        inp, cal = last_inp, last_cal
        cbp = pd.DataFrame({"правило": cal["path"], "без сглаживания": cal["notional"]})
        cbp.index = cbp.index.astype(str)
        cbp.round(2).to_excel(xw, sheet_name="Последний прогноз ЦБ")

    pd.set_option("display.width", 260)
    cols = ["дата прогноза", "ставка после решения", "πᵉ сейчас (через 3 кв.)", "разрыв (известный)",
            "RMSE без надбавки, п.п.", "RMSE подгонки, п.п.", "надбавка: остаток года"]
    print(table[cols].round(2).to_string(index=False))
    print()
    for d in sorted(cal_store)[-3:]:
        inp, cal = cal_store[d]
        print(f"{d:%Y-%m-%d}: " + "; ".join(
            f"{'ост.года' if k == 'rest' else k}: ЦБ {t:.2f} | правило {n:.2f} | с надбавкой {f:.2f}"
            for k, t, f, n in zip(cal["keys"], cal["targets"], cal["fit"], cal["no_adj"])))
    for m, o in ours.items():
        print(f"\nШаг 3 ({m}): квартальные средние")
        q = o.groupby(o.index.asfreq("Q")).mean()
        print(q.round(2).to_string())


if __name__ == "__main__":
    main()
