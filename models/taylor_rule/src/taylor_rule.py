"""Репликация правила Тейлора Банка России (калибровка из опубликованного кода КПМ).

Правило КПМ ДДКП (Орлов 2021; Орлов, Шарафутдинов 2024; model/qpm_model.mod):
    i_t = rho*i_{t-1} + (1-rho)*( rbar_t + pi^e_{t+h} + phi_pi*(pi^e_{t+h} - pi*) + phi_y*gap_t )
    pi = 0.75*базовая + 0.25*общая инфляция г/г; rho = 0.75, phi_pi = 1.5 (1.9 — жёсткая ДКП),
    phi_y = 0.5, h = 3 кв., pi* = 4%

Правило КПМ ДИП (models/ru_model.model): rho = 0.63, phi_pi = 1.5, phi_y = 0, h = 4 кв.

Данные: факт (ЦБ, Росстат) + собственный прогноз (FORECAST_FILE) для ожиданий инфляции,
продления разрыва выпуска и сравнения траектории правила с прогнозом ключевой ставки.

Запуск: python src/download_data.py && python src/taylor_rule.py
Результат: outputs/taylor_rule_results.xlsx, outputs/*.png
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from statsmodels.tsa.filters.hp_filter import hpfilter

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "outputs"
SITE_ROOT = ROOT.parents[1]  # проект лежит в macro_forecast_site/models/taylor_rule
FORECAST_FILE = SITE_ROOT / "macro_main_linked.xlsx"
# выгрузка ЕМИСС «ИПЦ и БИПЦ»; .xls обрезается на 256 столбцах, поэтому .xlsx в приоритете
CORE_FILES = [ROOT / "data" / "ИПЦ и БИПЦ.xlsx", ROOT / "data" / "ИПЦ и БИПЦ.xls"]

TARGET = 4.0
W_CORE = 0.75                              # w_mp_pie_c в КПМ ДДКП
# Прогноз БИПЦ, если его нет в FORECAST_FILE: БИПЦ = прогноз ИПЦ − спред (ИПЦ − БИПЦ г/г).
# Спред до конца года перед SPREAD_CONVERGE_YEAR держится на текущем уровне (среднее за SPREAD_NOW_MONTHS мес.),
# в течение SPREAD_CONVERGE_YEAR линейно возвращается к среднему за SPREAD_LONG_MONTHS мес., дальше — постоянный.
SPREAD_NOW_MONTHS = 3
SPREAD_LONG_MONTHS = 36
SPREAD_CONVERGE_YEAR = 2027
# Наш прогноз ИПЦ г/г на декабри за пределами FORECAST_FILE (месяцы между — по прямой от последнего значения;
# БИПЦ — через спред). Используется, только если в файле нет этих месяцев. Правилу нужна инфляция на 3 кв. вперёд.
EXTRA_DEC_FORECASTS = {2028: 5.0}  # сообщено 15.10.2026
# Рост ВВП г/г по кварталам вне файла Росстата: предварительные оценки (считаются фактом) и наши ручные допущения
# на прогнозе (имеют приоритет над годовым прогнозом из FORECAST_FILE для своих кварталов).
GDP_YOY_PRELIM = {"2026Q3": 0.7}    # предварительная оценка, сообщено 15.10.2026
GDP_YOY_FORECAST = {"2026Q4": 0.3}  # наше допущение, сообщено 15.10.2026
SAMPLE_START = pd.Period("2015Q1", "Q")    # переход к таргетированию инфляции
DATA_START = pd.Period("2013Q4", "Q")
GDP_START = pd.Period("2011Q1", "Q")       # начало сопоставимого ряда в ценах 2021 г.

# Официальные оценки ЦБ номинальной нейтральной ставки (середина диапазона), с какого квартала действует.
# Даты пересмотров до 2023 г. приблизительные — уточнить по Основным направлениям ДКП.
NEUTRAL_NOMINAL = [
    ("2014Q1", 6.5),  # 6–7%  (реальная 2–3%)
    ("2020Q1", 5.5),  # 5–6%  (реальная 1–2%)
    ("2023Q3", 6.0),  # 5,5–6,5% (июль 2023)
    ("2023Q4", 6.5),  # 6–7%
    ("2024Q3", 8.0),  # 7,5–8,5% (июль 2024)
]
NEUTRAL_REAL_QPM = 4.0  # RR_EQ в стационарном состоянии КПМ ДДКП: RRF_SS (~0,8) + PREM_SS (~3,2)

MONTHS_RU = {m: i for i, m in enumerate(
    ["январь", "февраль", "март", "апрель", "май", "июнь",
     "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"], start=1)}


@dataclass(frozen=True)
class Rule:
    name: str
    rho: float
    phi_pi: float
    phi_y: float
    horizon: int  # горизонт ожидаемой инфляции, кварталов


RULES = [
    Rule("КПМ ДДКП", rho=0.75, phi_pi=1.5, phi_y=0.5, horizon=3),
    Rule("КПМ ДДКП жёсткая", rho=0.75, phi_pi=1.9, phi_y=0.5, horizon=3),
    Rule("КПМ ДИП", rho=0.63, phi_pi=1.5, phi_y=0.0, horizon=4),
]


# ---------------------------------------------------------------- загрузка: факт

def load_keyrate_q() -> pd.Series:
    kr = pd.read_csv(RAW / "keyrate_daily.csv", parse_dates=["date"]).set_index("date")["key_rate"]
    daily = kr.resample("D").ffill()  # выходные — по последнему значению
    q = daily.groupby(daily.index.to_period("Q")).mean()
    counts = daily.groupby(daily.index.to_period("Q")).size()
    return q[counts >= 85].rename("key_rate")  # только полные кварталы


def load_cpi_m() -> pd.Series:
    m = pd.read_csv(RAW / "inflation_monthly.csv")
    return pd.Series(m["cpi_yoy"].values, index=pd.PeriodIndex(m["month"], freq="M"), name="cpi_yoy")


def load_emiss_cpi(series: str) -> pd.Series:
    """ИПЦ г/г из выгрузки ЕМИСС «ИПЦ и БИПЦ» (периоды в столбцах: год / месяц / вид индекса).

    Лист может содержать несколько блоков (.xls вмещает 256 столбцов, поэтому периоды
    разбиты на блоки один под другим). В каждом блоке строки «РФ» (до 2022 г.) и
    «РФ без учёта новых субъектов» (с 2023 г.) — склеиваем, при пересечении приоритет у более раннего блока.
    series: «Все товары и услуги» или «Базовый индекс».
    """
    path = next((p for p in CORE_FILES if p.exists()), None)
    if path is None:
        return pd.Series(dtype=float)
    d = pd.read_excel(path, sheet_name=0, header=None)
    headers = [i for i in range(2, len(d)) if any(str(v).startswith("К соответствующему периоду") for v in d.iloc[i])]
    out = pd.Series(dtype=float)
    for n, hdr in enumerate(headers):
        end = headers[n + 1] - 2 if n + 1 < len(headers) else len(d)
        years = pd.to_numeric(d.iloc[hdr - 2].ffill(), errors="coerce")
        months = d.iloc[hdr - 1].ffill().astype(str).str.strip().str.lower().map(MONTHS_RU)
        is_yoy = d.iloc[hdr].astype(str).str.startswith("К соответствующему периоду")
        block = d.iloc[hdr + 1:end]
        for r in block.index[block.iloc[:, 1].astype(str).str.startswith(series)]:
            vals = pd.to_numeric(d.iloc[r], errors="coerce")
            ok = is_yoy & vals.notna() & years.notna() & months.notna()
            s = pd.Series(vals[ok].values - 100, index=pd.PeriodIndex.from_fields(
                year=years[ok].astype(int), month=months[ok].astype(int), freq="M"))
            out = out.combine_first(s)
    return out.sort_index()


def load_core_m() -> pd.Series:
    return load_emiss_cpi("Базовый индекс").rename("core_yoy")


def _rosstat_quarterly_sheet(xl: pd.ExcelFile, *title_parts: str) -> pd.Series:
    """Последний лист файла ВВП Росстата, в заголовке которого есть все title_parts (года/кварталы в столбцах)."""
    titles = {s: str(xl.parse(s, header=None, nrows=2).iloc[1, 0]) for s in xl.sheet_names if s != "Содержание"}
    sheet = [s for s, t in titles.items() if all(p in t for p in title_parts)][-1]
    d = xl.parse(sheet, header=None)
    years = d.iloc[2, :].ffill().astype(str).str[:4].astype(int)
    roman = {"I": 1, "II": 2, "III": 3, "IV": 4}
    quarters = d.iloc[3, :].astype(str).str.split().str[0].str.rstrip("0123456789)").map(roman)
    idx = pd.PeriodIndex.from_fields(year=years, quarter=quarters, freq="Q")
    return pd.Series(pd.to_numeric(d.iloc[4, :], errors="coerce").values, index=idx).dropna()


def load_gdp_sa() -> pd.Series:
    """ВВП в ценах 2021 г. с исключением сезонного фактора (Росстат).

    Сезонно скорректированный ряд Росстат обновляет позже первой оценки ВВП. Кварталы, для которых
    уже есть индекс физического объёма г/г, но ещё нет SA-уровня, достраиваются: SA[q] = SA[q−4]·индекс/100.
    """
    xl = pd.ExcelFile(RAW / "gdp_quarterly.xlsx")
    sa = _rosstat_quarterly_sheet(xl, "2021", "сезон")
    yoy = _rosstat_quarterly_sheet(xl, "Индексы физического объема", "соответствующему кварталу")
    for q in yoy.index[yoy.index > sa.index.max()]:
        if q - 4 in sa.index:
            sa.loc[q] = sa[q - 4] * yoy[q] / 100
    for q, g in sorted(GDP_YOY_PRELIM.items()):  # предварительные оценки, которых ещё нет в файле
        q = pd.Period(q, "Q")
        if q not in sa.index and q - 4 in sa.index:
            sa.loc[q] = sa[q - 4] * (1 + g / 100)
    return sa.sort_index().rename("gdp_sa")


# ---------------------------------------------------------------- загрузка: прогноз

def load_forecast() -> dict[str, pd.Series]:
    m = pd.read_excel(FORECAST_FILE, sheet_name="main").iloc[:, :6]
    m["date"] = pd.to_datetime(m["date"])

    def pick(name: str, units: str, freq: str) -> pd.Series:
        x = m[(m["indicator_name"] == name) & (m["units"] == units)]
        return pd.Series(x["value"].values, index=x["date"].dt.to_period(freq)).sort_index()

    return {
        "cpi_yoy": pick("Инфляция", "% г/г", "M"),
        "core_yoy": pick("Базовая инфляция", "% г/г", "M"),  # пусто, если показателя нет в файле
        "key_rate": pick("Ключевая ставка", "%", "M"),  # на конец месяца
        "gdp_growth": pick("ВВП", "% г/г", "Y"),
    }


def key_rate_daily(fc_eop: pd.Series) -> pd.Series:
    """Ключевая ставка по дням: факт ЦБ, далее наш прогноз, заданный значениями на конец месяца.

    Если значение на конец месяца m отличается от предыдущего, изменение привязывается к заседанию ЦБ в этом месяце
    (календарь cbr_meeting_calendar.csv) и вступает в силу на следующий рабочий день после заседания — как у ЦБ.
    Если заседания в месяце нет, изменение ставится на 15-е число. Средние за месяц/квартал/год считаются по дням."""
    act = pd.read_csv(RAW / "keyrate_daily.csv", parse_dates=["date"]).set_index("date")["key_rate"].sort_index()
    act = act.resample("D").ffill()
    cal = pd.to_datetime(pd.read_csv(RAW / "cbr_meeting_calendar.csv")["date"])
    last_day, rate = act.index.max(), act.iloc[-1]
    changes = []
    for m, v in fc_eop[fc_eop.index >= pd.Period(last_day, "M")].sort_index().items():
        if np.isclose(v, rate):
            continue
        meet = cal[(cal.dt.to_period("M") == m) & (cal > last_day)]
        eff = meet.max() + pd.offsets.BDay(1) if len(meet) else m.start_time + pd.Timedelta(days=14)
        changes.append((eff, v))
        rate = v
    days = pd.date_range(last_day + pd.Timedelta(days=1), fc_eop.index.max().end_time.normalize(), freq="D")
    fc_daily = pd.Series(act.iloc[-1], index=days)
    for eff, v in changes:
        fc_daily[fc_daily.index >= eff] = v
    out = pd.concat([act, fc_daily])
    out.attrs["changes"] = changes
    return out


def extend_gdp(gdp: pd.Series, growth: pd.Series, last_year: int) -> pd.Series:
    """Продлевает квартальный ВВП (SA) годовым прогнозом: недостающие кварталы года растут
    с постоянным темпом кв/кв так, чтобы среднегодовой уровень дал прогнозный рост г/г."""
    s = gdp.copy()
    for year in range(s.index.max().year, last_year + 1):
        q_year = pd.period_range(f"{year}Q1", f"{year}Q4", freq="Q")
        missing = [q for q in q_year if q not in s.index]
        if not missing or year not in growth.index.year:
            continue
        prev_avg = s[s.index.year == year - 1].mean()
        target_sum = 4 * prev_avg * (1 + growth[growth.index.year == year].iloc[0] / 100)
        known_sum = s[s.index.year == year].sum()
        base, k = s.iloc[-1], np.arange(1, len(missing) + 1)
        lo, hi = -0.2, 0.2  # бисекция по темпу кв/кв
        for _ in range(100):
            g = (lo + hi) / 2
            lo, hi = (g, hi) if known_sum + (base * (1 + g) ** k).sum() < target_sum else (lo, g)
        s = pd.concat([s, pd.Series(base * (1 + g) ** k, index=pd.PeriodIndex(missing))])
    return s.sort_index().rename("gdp_sa")


# ---------------------------------------------------------------- сборка квартальных данных

def to_quarter_mean(s: pd.Series) -> pd.Series:
    g = s.groupby(s.index.asfreq("Q"))
    return g.mean()[g.size() == 3]


def hp_gaps(y_log: pd.Series, n_actual: int, lamb: float = 1600) -> pd.DataFrame:
    gap_2s, _ = hpfilter(y_log, lamb=lamb)  # двусторонний, по ряду с продлением прогнозом
    # односторонний: на каждую дату — HP только по фактическим данным, известным на эту дату
    gap_1s = pd.Series(np.nan, index=y_log.index)
    for i in range(20, n_actual + 1):
        c, _ = hpfilter(y_log.iloc[:i], lamb=lamb)
        gap_1s.iloc[i - 1] = c.iloc[-1]
    return pd.DataFrame({"gap_hp2": gap_2s, "gap_hp1": gap_1s})


def neutral_nominal(index: pd.PeriodIndex) -> pd.Series:
    s = pd.Series(np.nan, index=index)
    for start, val in NEUTRAL_NOMINAL:
        s[s.index >= pd.Period(start, "Q")] = val
    return s.rename("neutral_nominal_cbr")


def _extend_to_december(s: pd.Series, year: int, value: float) -> pd.Series:
    """Продлевает месячный ряд г/г по прямой от последнего значения до заданного декабря."""
    dec = pd.Period(f"{year}-12", "M")
    if s.index.max() >= dec:
        return s
    months = pd.period_range(s.index.max() + 1, dec, freq="M")
    k = np.arange(1, len(months) + 1)
    return pd.concat([s, pd.Series(s.iloc[-1] + (value - s.iloc[-1]) * k / len(months), index=months)])


def monthly_inflation(fc: dict) -> dict:
    """Месячные ряды г/г: факт, после последнего фактического месяца — наш прогноз.
    Возвращает cpi, core, mix (0,75·БИПЦ + 0,25·ИПЦ), mix_eq (БИПЦ = ИПЦ на прогнозе), cpi_act, core_act, core_src."""
    cpi_act = load_cpi_m()
    cpi_m = pd.concat([cpi_act, fc["cpi_yoy"][fc["cpi_yoy"].index > cpi_act.index.max()]])
    for year, dec in sorted(EXTRA_DEC_FORECASTS.items()):
        cpi_m = _extend_to_december(cpi_m, year, dec)
    core_act = load_core_m()
    core_m, core_eq_m, core_src, spread_m = core_act, core_act, "нет данных", pd.Series(dtype=float)
    if len(core_act):
        fc_months = cpi_m.index[cpi_m.index > core_act.index.max()]
        core_fc = fc["core_yoy"].reindex(fc_months)
        if core_fc.notna().all() and len(fc_months):
            core_src = "прогноз из FORECAST_FILE"
        else:
            spread_m = spread_path(cpi_m - core_act, fc_months)
            core_fc = cpi_m[fc_months] - spread_m
            s = spread_m.attrs
            core_src = (f"ИПЦ − спред: {s['now']:.2f} п.п. (среднее за {SPREAD_NOW_MONTHS} мес.) до конца "
                        f"{SPREAD_CONVERGE_YEAR - 1} г., в {SPREAD_CONVERGE_YEAR} г. линейно к {s['long']:.2f} п.п. "
                        f"(среднее за {SPREAD_LONG_MONTHS // 12} года), дальше постоянный")
        core_m = pd.concat([core_act, core_fc])
        core_eq_m = pd.concat([core_act, cpi_m[fc_months]])  # чувствительность: БИПЦ = ИПЦ на прогнозе
    return {
        "cpi": cpi_m, "core": core_m,
        "mix": (W_CORE * core_m + (1 - W_CORE) * cpi_m).dropna(),
        "mix_eq": (W_CORE * core_eq_m + (1 - W_CORE) * cpi_m).dropna(),
        "cpi_act": cpi_act, "core_act": core_act, "core_src": core_src, "spread": spread_m,
    }


def spread_path(spread_act: pd.Series, months: pd.PeriodIndex) -> pd.Series:
    """Спред ИПЦ − БИПЦ на прогнозных месяцах: текущий уровень до конца года перед SPREAD_CONVERGE_YEAR,
    в SPREAD_CONVERGE_YEAR — линейно к среднему за SPREAD_LONG_MONTHS мес., далее постоянный."""
    hist = spread_act.dropna()
    now, long = hist.iloc[-SPREAD_NOW_MONTHS:].mean(), hist.iloc[-SPREAD_LONG_MONTHS:].mean()
    w = np.clip([(p.year - SPREAD_CONVERGE_YEAR) + p.month / 12 if p.year >= SPREAD_CONVERGE_YEAR else 0
                 for p in months], 0, 1)
    out = pd.Series(now + (long - now) * w, index=months)
    out.attrs.update(now=now, long=long)
    return out


def build_dataset() -> tuple[pd.DataFrame, dict]:
    fc = load_forecast()
    infl = monthly_inflation(fc)
    cpi_act, cpi_m, core_act, core_m = infl["cpi_act"], infl["cpi"], infl["core_act"], infl["core"]
    mix_m, mix_eq_m, core_src = infl["mix"], infl["mix_eq"], infl["core_src"]

    # ВВП: факт + продление годовым прогнозом
    gdp_act = load_gdp_sa()
    last_q = cpi_m.index.max().asfreq("Q")
    gdp = extend_gdp(gdp_act, fc["gdp_growth"], last_q.year)
    gdp = gdp[(gdp.index >= GDP_START) & (gdp.index <= last_q)]
    gaps = hp_gaps(100 * np.log(gdp), n_actual=int((gdp_act.index >= GDP_START).sum()))

    kr_fc = fc["key_rate"]
    idx = pd.period_range(DATA_START, last_q, freq="Q")
    df = pd.DataFrame(index=idx)
    df["key_rate"] = load_keyrate_q()
    df["key_rate_fcst"] = to_quarter_mean(kr_fc)  # прогноз ставки (ср. значений на конец месяца)
    df["cpi_yoy"] = to_quarter_mean(cpi_m)
    df["core_yoy"] = to_quarter_mean(core_m) if len(core_m) else np.nan
    df["pi_mix"] = to_quarter_mean(mix_m) if len(mix_m) else np.nan
    df["pi_mix_eq"] = to_quarter_mean(mix_eq_m) if len(mix_eq_m) else np.nan
    df = df.join(gaps)
    # квартал прогнозный, если в нём есть хотя бы один прогнозный месяц
    df["is_fcst_cpi"] = df.index > (cpi_act.index.max() + 1).asfreq("Q") - 1
    df["is_fcst_gdp"] = df.index > gdp_act.index.max()
    df["gap_hp1"] = df["gap_hp1"].ffill()
    # ставка для лага в правиле: факт, где он есть, далее — прогноз
    df["key_rate_path"] = df["key_rate"].combine_first(df["key_rate_fcst"])
    df["neutral_nominal_cbr"] = neutral_nominal(df.index)
    df["rbar_cbr"] = df["neutral_nominal_cbr"] - TARGET
    df["rbar_qpm"] = NEUTRAL_REAL_QPM

    info = {
        "cpi_actual_to": str(cpi_act.index.max()),
        "core_actual_to": str(core_act.index.max()) if len(core_act) else "нет данных",
        "core_forecast": core_src,
        "gdp_actual_to": str(gdp_act.index.max()),
        "forecast_to": str(last_q),
    }
    return df, info


# ---------------------------------------------------------------- правило

def expected_inflation(df: pd.DataFrame, horizon: int, kind: str, measure: str) -> pd.Series:
    pi = df[measure]
    if kind == "fwd":  # инфляция через h кварталов: факт, далее — собственный прогноз
        return pi.shift(-horizon)
    if kind == "cur":  # текущая инфляция (адаптивные ожидания)
        return pi
    raise ValueError(kind)


def apply_rule(df: pd.DataFrame, rule: Rule, expect: str, measure: str, rbar_col: str, gap_col: str) -> pd.DataFrame:
    pi_e = expected_inflation(df, rule.horizon, expect, measure)
    notional = df[rbar_col] + pi_e + rule.phi_pi * (pi_e - TARGET) + rule.phi_y * df[gap_col]
    one_step = rule.rho * df["key_rate_path"].shift(1) + (1 - rule.rho) * notional

    def simulate(start: pd.Period) -> pd.Series:
        out = pd.Series(np.nan, index=df.index)
        prev = df["key_rate_path"].shift(1).get(start)
        for t in df.index[df.index >= start]:
            if np.isnan(notional[t]) or np.isnan(prev):
                break
            prev = rule.rho * prev + (1 - rule.rho) * notional[t]
            out[t] = prev
        return out

    first_fcst = df.index[df["key_rate"].isna() & (df.index > SAMPLE_START)].min()
    return pd.DataFrame({
        "pi_e": pi_e, "notional": notional, "one_step": one_step,
        "dynamic": simulate(SAMPLE_START),          # с 2015Q1
        "path_fcst": simulate(first_fcst),          # траектория по правилу от последней фактической ставки
    })


def fit_stats(actual: pd.Series, fitted: pd.Series) -> dict:
    m = (actual.index >= SAMPLE_START) & fitted.notna() & actual.notna()
    e = (actual - fitted)[m]
    return {"N": int(m.sum()), "с": str(actual.index[m].min()) if m.any() else "", "по": str(actual.index[m].max()) if m.any() else "",
            "ME (факт − правило)": e.mean(), "RMSE": np.sqrt((e**2).mean()), "corr": actual[m].corr(fitted[m])}


# ---------------------------------------------------------------- вывод

def shade_forecast(ax, df: pd.DataFrame) -> None:
    fc = df.index[df["key_rate"].isna()]
    if len(fc):
        ax.axvspan(fc.min().to_timestamp(how="start"), fc.max().to_timestamp(how="end"), color="grey", alpha=0.12, lw=0)


def plot_main(df: pd.DataFrame, res: pd.DataFrame, title: str, path: Path, pi_label: str) -> None:
    x0 = df.index.to_timestamp(how="start")  # ступенька ставки покрывает весь квартал
    x = x0 + pd.Timedelta(days=45)             # квартальные точки — в середину квартала
    fig, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    ax = axes[0]
    ax.step(x0, df["key_rate"], where="post", color="black", lw=2, label="Ключевая ставка (ср. за квартал)")
    ax.step(x0, df["key_rate_fcst"].where(df["key_rate"].isna()), where="post", color="black", lw=2, ls="--",
            label="Прогноз ключевой ставки")
    ax.plot(x, res["notional"], color="tab:red", lw=1.2, ls="--", label="Правило без сглаживания")
    ax.plot(x, res["one_step"], color="tab:blue", lw=1.5, label="Правило: на 1 шаг (лаг ставки — факт/прогноз)")
    ax.plot(x, res["dynamic"], color="tab:green", lw=1.5, label="Правило: динамическая симуляция с 2015Q1")
    ax.plot(x, df["neutral_nominal_cbr"], color="grey", lw=1, ls=":", label="Нейтральная ставка (оценка ЦБ)")
    shade_forecast(ax, df)
    ax.set_ylabel("% годовых")
    ax.set_title(title)
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=0.3)

    ax = axes[1]
    ax.plot(x, df["cpi_yoy"], color="tab:orange", label="Инфляция ИПЦ, г/г")
    if df["core_yoy"].notna().any():
        ax.plot(x, df["core_yoy"], color="tab:brown", lw=1, label="Базовая инфляция, г/г")
    ax.plot(x, res["pi_e"], color="tab:orange", ls="--", label=pi_label)
    ax.axhline(TARGET, color="grey", lw=0.8)
    ax.bar(x, df["gap_hp2"], width=60, color="tab:purple", alpha=0.4, label="Разрыв выпуска (HP), %")
    shade_forecast(ax, df)
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_forecast(df: pd.DataFrame, variants: dict[str, pd.DataFrame], path: Path) -> None:
    d = df[df.index >= pd.Period("2023Q1", "Q")]
    x0 = d.index.to_timestamp(how="start")
    x = x0 + pd.Timedelta(days=45)
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.step(x0, d["key_rate"], where="post", color="black", lw=2.2, label="Ключевая ставка, факт")
    ax.step(x0, d["key_rate_fcst"].where(d["key_rate"].isna()), where="post", color="black", lw=2.2, ls="--",
            label="Ключевая ставка, наш прогноз")
    colors = ["tab:blue", "tab:green", "tab:red", "tab:purple"]
    for (label, r), c in zip(variants.items(), colors):
        r = r.loc[d.index]
        ax.plot(x, r["one_step"], color=c, lw=1.6, label=f"{label}: на 1 шаг")
        ax.plot(x, r["path_fcst"], color=c, lw=1.6, ls=":", marker="o", ms=3, label=f"{label}: траектория от последнего факта")
        ax.plot(x, r["notional"], color=c, lw=0.9, ls="--", alpha=0.6, label=f"{label}: без сглаживания")
    shade_forecast(ax, d)
    ax.set_title("Ключевая ставка: прогноз vs правило Тейлора КПМ ДДКП")
    ax.set_ylabel("% годовых")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=2, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    df, info = build_dataset()

    # pi_mix — основная спецификация КПМ ДДКП; cpi_yoy и pi_mix_eq (БИПЦ = ИПЦ на прогнозе) — для сравнения
    measures = [m for m in ("pi_mix", "cpi_yoy", "pi_mix_eq") if df[m].notna().any()]
    results, stats = {}, []
    for rule in RULES:
        for measure in measures:
            for expect in ("fwd", "cur"):
                for rbar_col in ("rbar_cbr", "rbar_qpm"):
                    for gap_col in ("gap_hp2", "gap_hp1"):
                        if rule.phi_y == 0 and gap_col == "gap_hp1":
                            continue
                        key = f"{rule.name} | {measure} | ожид={expect} | {rbar_col} | {gap_col}"
                        res = apply_rule(df, rule, expect, measure, rbar_col, gap_col)
                        results[key] = res
                        for kind in ("one_step", "dynamic", "notional"):
                            stats.append({"правило": rule.name, "инфляция": measure, "ожидания": expect,
                                          "нейтр. ставка": rbar_col, "разрыв": gap_col, "ряд": kind,
                                          **fit_stats(df["key_rate"], res[kind])})
    stats.append({"правило": "Наивный: i(t-1)", "ряд": "one_step",
                  **fit_stats(df["key_rate"], df["key_rate"].shift(1))})
    stats = pd.DataFrame(stats)

    def key(measure: str, expect: str = "fwd", rule: str = "КПМ ДДКП", rbar: str = "rbar_cbr") -> str:
        return f"{rule} | {measure} | ожид={expect} | {rbar} | gap_hp2"

    base_key = key("pi_mix")
    plot_main(df, results[base_key],
              "Правило КПМ ДДКП (ρ=0,75; φπ=1,5; φy=0,5); 0,75·БИПЦ + 0,25·ИПЦ через 3 кв.: факт → прогноз",
              OUT / "rule_qpm_ddkp_fwd.png", "0,75·БИПЦ + 0,25·ИПЦ через 3 кв. (факт/прогноз)")
    plot_main(df, results[key("pi_mix", "cur")], "Правило КПМ ДДКП — текущая инфляция (0,75·БИПЦ + 0,25·ИПЦ)",
              OUT / "rule_qpm_ddkp_cur.png", "Текущая 0,75·БИПЦ + 0,25·ИПЦ")
    plot_main(df, results[key("cpi_yoy")], "Правило КПМ ДДКП — общий ИПЦ через 3 кв.",
              OUT / "rule_qpm_ddkp_cpi_fwd.png", "ИПЦ через 3 кв. (факт/прогноз)")
    plot_main(df, results[key("cpi_yoy", rule="КПМ ДИП")],
              "Правило КПМ ДИП (ρ=0,63; φπ=1,5; без разрыва; ИПЦ через 4 кв.)", OUT / "rule_qpm_dip_fwd.png",
              "ИПЦ через 4 кв. (факт/прогноз)")
    (OUT / "rule_qpm_ddkp_mix_fwd.png").unlink(missing_ok=True)  # заменён rule_qpm_ddkp_fwd.png

    fc_variants = {
        "БИПЦ/ИПЦ через 3 кв.": results[base_key],
        "БИПЦ/ИПЦ текущая": results[key("pi_mix", "cur")],
        "ИПЦ через 3 кв.": results[key("cpi_yoy")],
        "БИПЦ=ИПЦ на прогнозе, через 3 кв.": results[key("pi_mix_eq")],
    }
    plot_forecast(df, dict(list(fc_variants.items())[:3]), OUT / "forecast_vs_rule.png")

    # ---- Excel
    fc_tab = df[["key_rate", "key_rate_fcst", "cpi_yoy", "core_yoy", "pi_mix", "gap_hp2", "neutral_nominal_cbr",
                 "is_fcst_cpi", "is_fcst_gdp"]].copy()
    for label, r in fc_variants.items():
        for c in ("pi_e", "notional", "one_step", "path_fcst"):
            fc_tab[f"{label} :: {c}"] = r[c]
    fc_tab = fc_tab[fc_tab.index >= pd.Period("2024Q1", "Q")]
    params = pd.DataFrame([r.__dict__ for r in RULES])
    neutral = pd.DataFrame(NEUTRAL_NOMINAL, columns=["с квартала", "номинальная нейтральная, %"])
    wide = pd.concat([df] + [r.add_prefix(f"{k} :: ") for k, r in results.items()], axis=1)
    with pd.ExcelWriter(OUT / "taylor_rule_results.xlsx") as xw:
        for name, t in [("Прогноз vs правило", fc_tab), ("Базовый вариант", df.join(results[base_key].add_prefix("base_")))]:
            t = t.copy()
            t.index = t.index.astype(str)
            t.to_excel(xw, sheet_name=name)
        stats.round(3).to_excel(xw, sheet_name="Качество подгонки", index=False)
        params.to_excel(xw, sheet_name="Параметры правил", index=False)
        neutral.to_excel(xw, sheet_name="Нейтральная ставка", index=False)
        pd.Series(info).to_frame("значение").to_excel(xw, sheet_name="Данные")
        wide.index = wide.index.astype(str)
        wide.to_excel(xw, sheet_name="Все варианты")

    pd.set_option("display.width", 250)
    print("Данные:", info)
    print("\nПрогноз vs правило (КПМ ДДКП, нейтр. ЦБ):")
    show = df[["key_rate", "key_rate_fcst", "cpi_yoy", "core_yoy", "gap_hp2"]].copy()
    for n, (label, r) in enumerate(fc_variants.items(), start=1):
        print(f"  V{n} = {label}")
        show[f"V{n} πe"] = r["pi_e"]
        show[f"V{n} 1шаг"] = r["one_step"]
        show[f"V{n} траект"] = r["path_fcst"]
        show[f"V{n} без сгл"] = r["notional"]
    print(show[show.index >= pd.Period("2025Q3", "Q")].round(2).to_string())
    print("\nКачество подгонки (вся выборка), ряд one_step:")
    sel = (stats["ряд"] == "one_step") & stats["инфляция"].isin(["pi_mix", "cpi_yoy", np.nan])
    print(stats[sel].sort_values("RMSE").round(2).head(14).to_string(index=False))


if __name__ == "__main__":
    main()
