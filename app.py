# app.py
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
from dateutil.relativedelta import relativedelta
from flask import (
    Flask, jsonify, render_template, request, send_from_directory,
    Response, abort
)

from utils import yield_forecast as yf
from utils import analyst_forecasts as af

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _pick_first_existing(paths: List[str]) -> Optional[str]:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None

_env_path = os.getenv("DATA_FILE", "").strip()
_data_candidates = [
    _env_path,
    os.path.join(BASE_DIR, "macro_main_linked.xlsx"),
    os.path.join(BASE_DIR, "data", "macro_main_linked.xlsx"),
    os.path.join(BASE_DIR, "data", "macro_data.xlsx"),
]
DATA_FILE = _pick_first_existing(_data_candidates)
if not DATA_FILE:
    raise FileNotFoundError(
        "Не найден файл данных. Укажи DATA_FILE или положи 'macro_main_linked.xlsx' рядом/в data/."
    )

MAIN_SHEET = os.getenv("MAIN_SHEET", "main")
EXPORT_FILE = os.getenv("EXPORT_FILE", DATA_FILE)

REQUIRE_BASIC_AUTH = os.getenv("REQUIRE_BASIC_AUTH", "0") == "1"
BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER", "user")
BASIC_AUTH_PASS = os.getenv("BASIC_AUTH_PASS", "pass")

# Жёсткая частота просмотра
NATIVE_FREQ_LOCK: Dict[str, str] = {
    "Инфляция": "M",  # и m/m, и y/y у нас месячные
    "ВВП": "A",
}

FREQ_RULE = {"M": "M", "Q": "Q", "A": "A-DEC"}

def _start_of_current_year() -> date:
    t = date.today()
    return date(t.year, 1, 1)

def _start_of_current_year_str() -> str:
    return _start_of_current_year().strftime("%Y-%m-%d")

app = Flask(__name__)

@app.after_request
def add_noindex_header(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp

def _check_auth(u, p):
    return u == BASIC_AUTH_USER and p == BASIC_AUTH_PASS

@app.before_request
def _maybe_basic_auth():
    if not REQUIRE_BASIC_AUTH:
        return None
    auth = request.authorization
    if not auth or not _check_auth(auth.username, auth.password):
        return Response("Нужна авторизация", 401, {"WWW-Authenticate": 'Basic realm="Login Required"'})
    return None

class DataCache:
    def __init__(self, path: str, sheet: str):
        self.path = path
        self.sheet = sheet
        self.mtime = 0.0
        self.df: Optional[pd.DataFrame] = None

    def _read_excel(self) -> pd.DataFrame:
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Нет файла: {self.path}")

        df = pd.read_excel(self.path, sheet_name=self.sheet)

        cols_map = {
            "indicator_code": "Code", "code": "Code", "код": "Code",
            "indicator_name": "Alias", "name": "Alias", "наименование": "Alias", "alias": "Alias", "series": "Alias", "ряд": "Alias",
            "date": "Date", "дата": "Date",
            "value": "Value", "значение": "Value",
            "frequency": "Freq", "freq": "Freq", "частота": "Freq",
            "aggregation": "Method", "aggregate": "Method", "агрегация": "Method", "method": "Method",
            "unit": "Unit", "единица": "Unit", "единицы": "Unit", "units": "Unit",
            "scenario": "Scenario", "source": "Source", "источник": "Source",
            "notes": "Notes", "примечания": "Notes",
        }
        df = df.rename(columns={c: cols_map.get(str(c).strip().lower(), c) for c in df.columns})

        req = ["Alias", "Date", "Value"]
        for c in req:
            if c not in df.columns:
                raise ValueError(f"В листе нет обязательного столбца {c}")

        if "Freq" not in df.columns: df["Freq"] = "M"
        if "Method" not in df.columns: df["Method"] = "avg"
        if "Unit" not in df.columns: df["Unit"] = ""

        df["Alias"] = df["Alias"].astype(str).str.strip()
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df[df["Date"].notna()]
        df["Value"] = pd.to_numeric(df["Value"], errors="coerce")

        freq_map = {
            "m":"M","month":"M","monthly":"M","месяц":"M","ежемесячно":"M","mth":"M",
            "q":"Q","quarter":"Q","quarterly":"Q","квартал":"Q","ежеквартально":"Q",
            "a":"A","y":"A","year":"A","yearly":"A","annual":"A","год":"A","ежегодно":"A"
        }
        df["Freq"] = df["Freq"].astype(str).str.strip().str.lower().map(freq_map).fillna("M")

        method_map = {
            "avg":"avg","average":"avg","mean":"avg","среднее":"avg","среднее за период":"avg",
            "eop":"eop","end":"eop","end_of_period":"eop","на конец периода":"eop","конец периода":"eop","last":"eop"
        }
        df["Method"] = df["Method"].astype(str).str.strip().str.lower().map(method_map).fillna("avg")
        df["Unit"] = df["Unit"].astype(str).fillna("").str.strip()

        df = df.sort_values(["Alias", "Date"]).reset_index(drop=True)
        return df

    def get_df(self) -> pd.DataFrame:
        mtime = os.path.getmtime(self.path)
        if self.df is None or mtime != self.mtime:
            self.df = self._read_excel()
            self.mtime = mtime
        return self.df.copy()

cache = DataCache(DATA_FILE, MAIN_SHEET)

def _format_date(dt: pd.Timestamp) -> str:
    return pd.to_datetime(dt).strftime("%d.%m.%Y")

def _round_val(x: Optional[float]) -> str:
    if x is None or pd.isna(x): return ""
    return f"{x:.1f}" if abs(x) >= 1 else f"{x:.2f}"

def _resample_series(s: pd.Series, freq: str, method: str) -> pd.Series:
    rule = FREQ_RULE.get(freq, "M")
    return s.resample(rule).last() if method == "eop" else s.resample(rule).mean()

_KEY_RATE_DAILY_CACHE: Dict[str, pd.Series] = {}

def _key_rate_daily_series() -> pd.Series:
    """Ключевая ставка день-в-день (из того же ступенчатого ряда, что и на
    вкладках «Прогноз доходностей»/ОФЗ/Рубль — учитывает реальные даты
    вступления в силу решений СД, включая правило «со среды» с 2027 года).
    Кэшируется на время жизни процесса — пересчёт дешёвый, но незачем
    гонять на каждый запрос таблицы/графика."""
    cached = _KEY_RATE_DAILY_CACHE.get("s")
    if cached is not None:
        return cached
    key_lookup = yf.key_rate_lookup()
    start_d, end_d = date(2013, 9, 1), date(2029, 12, 31)
    idx = pd.date_range(start_d, end_d, freq="D")
    s = pd.Series([key_lookup(d.date()) for d in idx], index=idx)
    s.index.name = "Date"
    _KEY_RATE_DAILY_CACHE["s"] = s
    return s

def _resample_key_rate(freq: str, dfrom: Optional[str], dto: Optional[str]) -> pd.Series:
    """Ключевая ставка на любой частоте — ВСЕГДА среднее по дням периода
    (по методологии ЦБ), а не конец периода: тег Method='eop' у «Ключевая
    ставка» в исходном файле относится к месячным данным и исторически не
    подразумевал квартальную/годовую агрегацию — делать её через .last()
    давало бы ставку на конкретный день вместо честного среднего за период."""
    s = _key_rate_daily_series()
    rule = FREQ_RULE.get(freq, "M")
    out = s.resample(rule).mean()
    if dfrom:
        try: out = out[out.index >= pd.to_datetime(dfrom)]
        except: pass
    if dto:
        try: out = out[out.index <= pd.to_datetime(dto)]
        except: pass
    return out

def _apply_period_filter_df(df: pd.DataFrame, dfrom: Optional[str], dto: Optional[str]) -> pd.DataFrame:
    if dfrom:
        try: df = df[df["Date"] >= pd.to_datetime(dfrom)]
        except: pass
    if dto:
        try: df = df[df["Date"] <= pd.to_datetime(dto)]
        except: pass
    return df

def _period_years_from_df(df: pd.DataFrame) -> float:
    if df is None or df.empty: return 0.0
    d0 = pd.to_datetime(df["Date"].iloc[0]); d1 = pd.to_datetime(df["Date"].iloc[-1])
    return float((d1 - d0).days) / 365.25 if d1 >= d0 else 0.0

# ---------- Таблица: с поддержкой нескольких вариантов (по Unit) ----------
def _build_table(df: pd.DataFrame, alias: str, freq_view: str,
                 dfrom: Optional[str], dto: Optional[str]):
    sub = df[df["Alias"] == alias].copy()
    if sub.empty:
        return ["Дата", alias], [], 0, "M", 0.0, []

    native = sub["Freq"].iloc[0] if "Freq" in sub.columns else "M"
    locked = NATIVE_FREQ_LOCK.get(alias)
    freq_use = locked or (native if freq_view == "AUTO" else freq_view)

    # варианты ряда: группируем по Unit (+Method, чтобы подпись была корректной)
    variants = []
    for (unit, method), grp in sub.groupby(["Unit", "Method"]):
        if alias == "Ключевая ставка" and freq_use == "A":
            # Годовое значение — честное среднее по дням года (а не .last()
            # из тега Method='eop'). Квартальное и месячное — на конец
            # периода, как и для всех остальных показателей (без изменений).
            s = _resample_key_rate(freq_use, None, None)
        else:
            s = grp.set_index("Date")["Value"].astype(float).sort_index()
            if freq_use != native:
                s = _resample_series(s, freq_use, method)
        dfv = _apply_period_filter_df(s.to_frame("Value").reset_index(), dfrom, dto)
        variants.append((unit, method, dfv))

    # Собираем общий каркас дат (outer join по всем вариантам)
    dates = sorted(set().union(*[v[2]["Date"].tolist() for v in variants]))
    out = pd.DataFrame({"Date": dates}).set_index("Date")

    cols_meta = []
    for unit, method, dfv in variants:
        # «Ключевая ставка»: годовое значение — честное среднее по дням
        # (см. _resample_key_rate), квартальное/месячное — на конец периода,
        # как у тега файла. Подпись должна отражать то, что реально показано.
        disp_method = "avg" if (alias == "Ключевая ставка" and freq_use == "A") else method
        col_name = f"{alias} ({'в среднем за период' if disp_method=='avg' else 'на конец периода'}, {unit})" if unit else \
                   f"{alias} ({'в среднем за период' if disp_method=='avg' else 'на конец периода'})"
        out[col_name] = dfv.set_index("Date")["Value"]
        cols_meta.append({"unit": unit, "method": disp_method, "col": col_name})

    out = out.reset_index()
    period_years = _period_years_from_df(out.rename(columns={"Date":"Date"}))
    columns = ["Дата"] + [cm["col"] for cm in cols_meta]
    rows = []
    for _, r in out.iterrows():
        row = [_format_date(r["Date"])]
        for cm in cols_meta:
            row.append(_round_val(r.get(cm["col"])))
        rows.append(row)

    return columns, rows, len(rows), native, period_years, cols_meta

# ---------- Серии: отдаём несколько вариантов (по Unit) ----------
def _build_series(df: pd.DataFrame, alias: str, freq_view: str,
                  dfrom: Optional[str], dto: Optional[str]):
    sub = df[df["Alias"] == alias].copy()
    if sub.empty:
        return [], [], {"native": "M"}, 0.0

    native = sub["Freq"].iloc[0] if "Freq" in sub.columns else "M"
    locked = NATIVE_FREQ_LOCK.get(alias)
    freq_use = locked or (native if freq_view == "AUTO" else freq_view)

    series = []
    x_template = None
    period_df_for_len = None

    for (unit, method), grp in sub.groupby(["Unit", "Method"]):
        if alias == "Ключевая ставка" and freq_use == "A":
            s = _resample_key_rate(freq_use, None, None)
        else:
            s = grp.set_index("Date")["Value"].astype(float).sort_index()
            if freq_use != native:
                s = _resample_series(s, freq_use, method)
        dfv = _apply_period_filter_df(s.to_frame("Value").reset_index(), dfrom, dto)
        if x_template is None:
            x_template = [pd.to_datetime(d).strftime("%Y-%m-%d") for d in dfv["Date"]]
            period_df_for_len = dfv.copy()
        y = [None if pd.isna(v) else float(v) for v in dfv["Value"]]
        series.append({
            "name": f"{alias}, {unit}".strip().replace(" ,", ","),
            "unit": unit,
            "method": method,
            "y": y
        })

    meta = {
        "native": native,
        "freq_used": freq_use,
        "name": alias,
        "unit": "",  # базовая подпись на карточке
        "method": "",
        "alternatives": [s["name"] for s in series],
        "default_index": 0
    }
    period_years = _period_years_from_df(period_df_for_len)
    return x_template or [], series, meta, period_years

# ---------------- Routes ----------------
def _build_quarterly_forecast_table(df: pd.DataFrame):
    """Квартальная таблица: курсы (USD/EUR/CNY к RUB) — среднее за квартал
    (среднее трёх месячных avg-значений), Инфляция — г/г на конец квартала
    (значение последнего месяца квартала, т.к. ряд уже eop). С начала
    текущего года по последний квартал, доступный в прогнозе. Квартал
    считается прогнозным, если его последний месяц ещё не наступил (today
    лежит строго до конца квартала) — явного флага факт/прогноз в файле нет."""
    today = date.today()
    start = date(today.year, 1, 1)

    fx_aliases = {"usd_avg": "USD/RUB", "eur_avg": "EUR/RUB", "cny_avg": "CNY/RUB"}
    fx_series = {}
    for key, alias in fx_aliases.items():
        sub = df[(df["Alias"] == alias) & (df["Unit"] == "руб.")].sort_values("Date")
        sub = sub[sub["Date"] >= pd.Timestamp(start)]
        fx_series[key] = sub

    infl = df[(df["Alias"] == "Инфляция") & (df["Unit"] == "% г/г")].sort_values("Date")
    infl = infl[infl["Date"] >= pd.Timestamp(start)]

    all_max_dates = [s["Date"].max() for s in fx_series.values() if not s.empty]
    if not infl.empty:
        all_max_dates.append(infl["Date"].max())
    if not all_max_dates:
        return []
    last_date = max(all_max_dates)
    quarters = pd.period_range(start=pd.Period(start, freq="Q"), end=pd.Period(last_date, freq="Q"), freq="Q")

    fx_by_q = {
        key: s.set_index(pd.PeriodIndex(s["Date"], freq="Q"))["Value"] if not s.empty else pd.Series(dtype=float)
        for key, s in fx_series.items()
    }
    infl_by_q = infl.set_index(pd.PeriodIndex(infl["Date"], freq="Q"))["Value"]

    fx_by_y = {
        key: s.set_index(pd.PeriodIndex(s["Date"], freq="Y"))["Value"] if not s.empty else pd.Series(dtype=float)
        for key, s in fx_series.items()
    }
    infl_by_y = infl.set_index(pd.PeriodIndex(infl["Date"], freq="Y"))["Value"]

    # Ключевая ставка — средняя по ЕЖЕДНЕВНЫМ значениям ступенчатого ряда
    # (та же логика, что в прогнозе доходностей/ОФЗ: заседания СД ЦБ,
    # с 2027 года новая ставка вступает в силу со среды после заседания,
    # до этого — с понедельника, см. _meeting_effective_date)."""
    key_lookup = yf.key_rate_lookup()

    def avg_key_rate(d0: date, d1: date) -> float:
        n = (d1 - d0).days + 1
        total = sum(key_lookup(d0 + timedelta(days=i)) for i in range(n))
        return total / n

    def year_avg_row(year: int):
        y = pd.Period(str(year), freq="Y")
        row = {"label": f"Среднее за {year}", "is_year": True, "is_forecast": today < y.end_time.date()}
        for key, series_by_y in fx_by_y.items():
            vals = series_by_y.loc[[y]] if y in series_by_y.index else pd.Series(dtype=float)
            row[key] = round(float(vals.mean()), 2) if len(vals) else None
        infl_vals = infl_by_y.loc[[y]] if y in infl_by_y.index else pd.Series(dtype=float)
        row["infl_eoq"] = round(float(infl_vals.mean()), 2) if len(infl_vals) else None
        row["key_rate"] = round(avg_key_rate(y.start_time.date(), y.end_time.date()), 2)
        return row

    rows = []
    for i, q in enumerate(quarters):
        row = {"label": f"{q.quarter} кв. {q.year}", "is_year": False}
        for key, series_by_q in fx_by_q.items():
            vals = series_by_q.loc[[q]] if q in series_by_q.index else pd.Series(dtype=float)
            row[key] = round(float(vals.mean()), 2) if len(vals) else None
        infl_vals = infl_by_q.loc[[q]] if q in infl_by_q.index else pd.Series(dtype=float)
        row["infl_eoq"] = round(float(infl_vals.iloc[-1]), 2) if len(infl_vals) else None
        # Квартал — значение на конец квартала (как инфляция выше), год — среднее.
        row["key_rate"] = round(key_lookup(q.end_time.date()), 2)
        q_end = q.end_time.date()
        row["is_forecast"] = today < q_end
        rows.append(row)
        # после последнего квартала года (или конца диапазона) — колонка среднего за год
        is_last_in_range = i == len(quarters) - 1
        is_last_q_of_year = q.quarter == 4
        if is_last_q_of_year or is_last_in_range:
            rows.append(year_avg_row(q.year))
    return rows

@app.route("/")
def index():
    try:
        qs_names = request.args.get("names", default="", type=str)
        qs_from = request.args.get("from", default="", type=str) or _start_of_current_year_str()
        qs_to = request.args.get("to", default="", type=str)
        qs_freq = request.args.get("freq_view", default="AUTO", type=str).upper()

        df = cache.get_df()

        indicators = sorted(df["Alias"].dropna().unique().tolist())
        if not indicators:
            abort(500, "В файле нет данных")

        if not qs_names:
            prefer = ["Инфляция", "ВВП", "USD/RUB", "EUR/RUB", "CNY/RUB", "Ключевая ставка"]
            qs_names = next((p for p in prefer if p in indicators), indicators[0])

        columns, rows, total, native, _period, _cols_meta = _build_table(df, qs_names, qs_freq, qs_from, qs_to)
        quarterly_rows = _build_quarterly_forecast_table(df)

        af_indicators = af.INDICATORS
        af_quarters = af.available_quarters("key_rate")
        af_default_quarter = af.current_quarter_label()
        if af_default_quarter not in af_quarters and af_quarters:
            af_default_quarter = af_quarters[-1]

        last_updated = f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        source = "Источник: собранные данные из пользовательского файла"

        return render_template(
            "index.html",
            columns=columns,
            data=rows,
            total_rows=total,
            source=source,
            last_updated=last_updated,
            qs_names=qs_names,
            qs_from=qs_from,
            qs_to=qs_to,
            qs_freq=qs_freq,
            quarterly_rows=quarterly_rows,
            af_indicators=af_indicators,
            af_quarters=af_quarters,
            af_default_quarter=af_default_quarter,
            active_page="macro",
            page_title="Макропрогнозы",
            page_subtitle="Автообновляемая таблица и график выбранного показателя"
        )
    except Exception as e:
        return Response(f"Ошибка рендеринга: {e}", status=500)

@app.route("/yield-forecast")
def yield_forecast():
    return render_template(
        "yield_forecast.html",
        active_page="yield_forecast",
        page_title="Прогноз доходностей",
        page_subtitle="Ключевая ставка по датам заседаний ЦБ, депозиты и кривая ОФЗ"
    )

@app.route("/ofz")
def ofz():
    return render_template(
        "ofz.html",
        active_page="ofz",
        page_title="ОФЗ",
        page_subtitle=""
    )

@app.route("/api/ofz/history")
def api_ofz_history():
    try:
        curve = yf.load_ofz_curve()
        curve = curve[curve["Date"] >= date(2016, 1, 1)]
        tenors = [1, 5, 10]
        dates = [d.isoformat() for d in curve["Date"]]
        series = {str(t): [round(float(v), 3) for v in curve[f"y{t}"]] for t in tenors}

        live = _moex_live_ofz_curve()
        if live:
            live_date, live_curve = live
            if curve.empty or live_date > curve["Date"].max():
                dates.append(live_date.isoformat())
                for t in tenors:
                    series[str(t)].append(round(live_curve[t], 3))

        return jsonify({"dates": dates, "series": series, "tenors": tenors})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить историю доходности ОФЗ: {e}"}), 500

@app.route("/api/ofz/expectations_vs_fact")
def api_ofz_expectations_vs_fact():
    try:
        data = yf.key_rate_expectation_vs_fact()
        return jsonify({str(t): v for t, v in data.items()})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить сравнение ожиданий по КС с фактом: {e}"}), 500

@app.route("/api/irs/expectations_vs_fact")
def api_irs_expectations_vs_fact():
    try:
        data = yf.irs_expectation_vs_fact()
        return jsonify({str(t): v for t, v in data.items()})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить сравнение ожиданий по свопам с фактом: {e}"}), 500

@app.route("/api/ofz/model1")
def api_ofz_model1():
    try:
        snapshots = yf.model1_ofz_forecast()
        tenors = yf.TENORS
        return jsonify({"tenors": tenors, "snapshots": {label: [vals[t] for t in tenors] for label, vals in snapshots.items()}})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить прогноз ОФЗ (модель 1): {e}"}), 500

@app.route("/api/ofz/model1_quality")
def api_ofz_model1_quality():
    try:
        q = yf.model1_quality()
        return jsonify({src: {str(t): stats for t, stats in tdict.items()} for src, tdict in q.items()})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить оценку точности модели 1: {e}"}), 500

@app.route("/api/ofz/model2")
def api_ofz_model2():
    try:
        snapshots = yf.model2_inflation_forecast()
        tenors = yf.TENORS
        return jsonify({"tenors": tenors, "snapshots": {label: [vals[t] for t in tenors] for label, vals in snapshots.items()}})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить прогноз ОФЗ (модель 2): {e}"}), 500

@app.route("/api/ofz/model2_quality")
def api_ofz_model2_quality():
    try:
        q = yf.model2_quality()
        return jsonify({src: {str(t): stats for t, stats in tdict.items()} for src, tdict in q.items()})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить оценку точности модели 2: {e}"}), 500

@app.route("/api/ofz/model3")
def api_ofz_model3():
    try:
        snapshots = yf.model3_ofz_forecast()
        tenors = yf.TENORS
        return jsonify({"tenors": tenors, "snapshots": {label: [vals[t] for t in tenors] for label, vals in snapshots.items()}})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить прогноз ОФЗ (модель 3): {e}"}), 500

@app.route("/api/ofz/model3_quality")
def api_ofz_model3_quality():
    try:
        q = yf.model3_quality()
        return jsonify({src: {str(t): stats for t, stats in tdict.items()} for src, tdict in q.items()})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить оценку точности модели 3: {e}"}), 500

def _split_history_forecast(points):
    today = date.today()
    hist = [{"date": d.isoformat(), "value": v} for d, v in points if d <= today]
    fcst = [{"date": d.isoformat(), "value": v} for d, v in points if d > today]
    # соединяем линии без разрыва
    if hist and fcst:
        fcst = [hist[-1]] + fcst
    return hist, fcst

KEY_RATE_HORIZONS = [(1, "Средняя КС на 1 год"), (2, "Средняя КС на 2 года"), (5, "Средняя КС на 5 лет"), (10, "Средняя КС на 10 лет")]

@app.route("/api/yield/key_rate")
def api_yield_key_rate():
    try:
        points = yf.build_key_rate_series()
        hist, fcst = _split_history_forecast(points)
        meetings_2026 = {m: d.isoformat() for m, d in yf.MEETINGS_2026.items()}

        today = date.today()
        band = [
            {"date": d.isoformat(), "low": round(lo, 3), "high": round(hi, 3)}
            for d, lo, hi in yf.build_key_rate_band() if d >= today
        ]

        key_lookup = yf.key_rate_lookup()
        current_rate = key_lookup(today)
        avg_horizons = []
        for years, label in KEY_RATE_HORIZONS:
            avg = yf.forward_average_key_rate(key_lookup, today, days=years * 365)
            avg_horizons.append({
                "label": label, "years": years, "value": round(avg, 2),
                "delta": round(avg - current_rate, 2),
            })

        try:
            irs_date, irs_values = yf.market_implied_key_rate()
            irs_horizons = [
                {
                    "label": label.replace("Средняя КС", "IRS"), "years": years,
                    "value": round(irs_values[years], 2), "delta": round(irs_values[years] - current_rate, 2),
                }
                for years, label in KEY_RATE_HORIZONS if years in irs_values
            ]
            irs_date_str = irs_date.isoformat()
        except Exception:
            irs_horizons, irs_date_str = [], None

        return jsonify({
            "history": hist, "forecast": fcst, "meetings_2026": meetings_2026, "band": band,
            "current_rate": round(current_rate, 2), "avg_horizons": avg_horizons,
            "irs_horizons": irs_horizons, "irs_date": irs_date_str,
        })
    except Exception as e:
        return jsonify({"error": f"Не удалось построить прогноз ключевой ставки: {e}"}), 500

@app.route("/api/yield/deposit")
def api_yield_deposit():
    try:
        hist, fut, spread_info = yf.forecast_deposit_rate()
        hist_pts = [{"date": d.isoformat(), "value": round(float(v), 3)} for d, v in zip(hist["Date"], hist["Rate"])]
        fut_pts = [
            {"date": d.isoformat(), "value": round(float(v), 3), "p20": round(float(lo), 3), "p80": round(float(hi), 3)}
            for d, v, lo, hi in zip(fut["Date"], fut["Rate"], fut["RateLo"], fut["RateHi"])
        ]
        if hist_pts and fut_pts:
            join = dict(hist_pts[-1])
            join["p20"] = join["value"]
            join["p80"] = join["value"]
            fut_pts = [join] + fut_pts
        return jsonify({"history": hist_pts, "forecast": fut_pts, **spread_info})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить прогноз ставки по депозитам: {e}"}), 500

@app.route("/api/yield/ofz")
def api_yield_ofz():
    try:
        live = _moex_live_ofz_curve()
        live_date, live_curve = live if live else (None, None)
        tenors = yf.TENORS

        curve = yf.load_ofz_curve()
        if live_date and live_curve:
            curve = pd.concat([curve, pd.DataFrame([{"Date": live_date, **{f"y{t}": live_curve[t] for t in tenors}}])], ignore_index=True)
        monthly_hist = (
            curve.set_index(pd.to_datetime(curve["Date"]))[[f"y{t}" for t in tenors]]
            .resample("ME").last().reset_index()
        )
        monthly_hist["Date"] = monthly_hist["Date"].dt.date
        monthly_hist = monthly_hist[monthly_hist["Date"] >= date(2016, 1, 1)].reset_index(drop=True)

        snapshots, monthly = yf.ensemble_ofz_forecast(tenors, live_date=live_date, live_curve=live_curve)
        snap_out = {label: [vals[t] for t in tenors] for label, vals in snapshots.items()}
        snap_p20 = {label: [monthly[m][t]["p20"] for t in tenors] for label, m in yf.ENSEMBLE_SNAPSHOT_MONTHS.items()}
        snap_p80 = {label: [monthly[m][t]["p80"] for t in tenors] for label, m in yf.ENSEMBLE_SNAPSHOT_MONTHS.items()}

        last_date = live_date or curve["Date"].max()
        fut_dates = [last_date] + [last_date + relativedelta(months=m) for m in range(1, 13)]

        def series_points(dfr):
            return {
                "dates": [d.isoformat() for d in dfr["Date"]],
                "series": {str(t): [None if pd.isna(x) else round(float(x), 3) for x in dfr[f"y{t}"]] for t in tenors}
            }

        hist_series = series_points(monthly_hist)
        fut_series = {
            "dates": [d.isoformat() for d in fut_dates],
            "series": {str(t): [monthly[m][t]["avg"] for m in range(13)] for t in tenors},
            "p20": {str(t): [monthly[m][t]["p20"] for m in range(13)] for t in tenors},
            "p80": {str(t): [monthly[m][t]["p80"] for m in range(13)] for t in tenors},
        }

        return jsonify({
            "tenors": tenors,
            "snapshots": snap_out,
            "snapshots_p20": snap_p20,
            "snapshots_p80": snap_p80,
            "history": hist_series,
            "forecast": fut_series
        })
    except Exception as e:
        return jsonify({"error": f"Не удалось построить прогноз кривой ОФЗ: {e}"}), 500

def _nearest_row(sub: pd.DataFrame, target: "date"):
    diffs = (sub["Date"] - pd.Timestamp(target)).abs()
    return sub.loc[diffs.idxmin()]

# Модельное среднемесячное значение курса за текущий месяц отстаёт от
# реального споt-курса — фолбэк на случай, если live-запрос к MOEX не удался.
CNY_RUB_SPOT_FALLBACK = float(os.getenv("CNY_RUB_SPOT", "12.5"))
USD_RUB_SPOT_FALLBACK = float(os.getenv("USD_RUB_SPOT", "87"))

_MOEX_UA = {"User-Agent": "Mozilla/5.0"}
_MOEX_TIMEOUT = 6

# Три выпуска ОФЗ, номинированных в юанях (ISIN, отображаемое имя).
CNY_OFZ_BONDS = [
    ("RU000A10DQB6", "ОФЗ CNY (29)"),
    ("RU000A10DQA8", "ОФЗ CNY (33)"),
    ("RU000A10FAK6", "ОФЗ CNY (36)"),
]

def _moex_fetch_json(url: str):
    import urllib.request
    req = urllib.request.Request(url, headers=_MOEX_UA)
    with urllib.request.urlopen(req, timeout=_MOEX_TIMEOUT) as resp:
        return json.loads(resp.read())

def _moex_fx_last(secid: str) -> Optional[float]:
    """Live споt-курс с MOEX (LAST на борде CETS), или None при сбое."""
    try:
        data = _moex_fetch_json(f"https://iss.moex.com/iss/engines/currency/markets/selt/securities/{secid}.json")
        md = data["marketdata"]
        cols = list(md["metadata"].keys())
        for row in md["data"]:
            rec = dict(zip(cols, row))
            if rec.get("BOARDID") == "CETS" and rec.get("LAST") is not None:
                return float(rec["LAST"])
    except Exception:
        pass
    return None

def _moex_best_cny_ofz() -> Optional[Tuple[str, float, str]]:
    """Доходность к погашению (борд TQOY, расчёт в юанях) по каждой из 3
    юаневых ОФЗ — возвращает бумагу с максимальной доходностью:
    (имя, YTM в CNY %, ISIN), или None если ни одна не получена."""
    best = None
    for isin, name in CNY_OFZ_BONDS:
        try:
            data = _moex_fetch_json(f"https://iss.moex.com/iss/engines/stock/markets/bonds/securities/{isin}.json")
            md = data["marketdata"]
            cols = list(md["metadata"].keys())
            for row in md["data"]:
                rec = dict(zip(cols, row))
                if rec.get("BOARDID") == "TQOY" and rec.get("YIELD") is not None:
                    yld = float(rec["YIELD"])
                    if best is None or yld > best[1]:
                        best = (name, yld, isin)
        except Exception:
            continue
    return best

def _moex_live_ofz_curve() -> Optional[Tuple["date", Dict[int, float]]]:
    """Live-кривая КБД МосБиржи (zcyc.json, секция yearyields) — уже готовые
    точки доходности на стандартных сроках, без статичного Excel-файла.
    MOEX не публикует срок 4 года — линейно интерполируем между 3 и 5."""
    try:
        data = _moex_fetch_json("https://iss.moex.com/iss/engines/stock/zcyc.json?iss.meta=off")
        rows = data["yearyields"]["data"]
        pts: Dict[float, float] = {row[2]: row[3] for row in rows}
        trade_date = datetime.strptime(rows[0][0], "%Y-%m-%d").date()
        curve: Dict[int, float] = {}
        for t in yf.TENORS:
            if t in pts:
                curve[t] = float(pts[t])
            elif t == 4 and 3 in pts and 5 in pts:
                curve[t] = float(pts[3] + (pts[5] - pts[3]) * (4 - 3) / (5 - 3))
            else:
                return None
        return trade_date, curve
    except Exception:
        return None

MMF_SPREAD_FALLBACK = -0.5  # используется только если live-запрос к MOEX не удался
MMF_FUND_SECID = "LQDT"  # БПИФ «Ликвидность» УК ВИМ — прокси фонда денежного рынка
MMF_WINDOW_DAYS = 184  # ~полгода: короткое окно (~1 мес.) даёт неустойчивую аннуализацию

NDFL_RATE = 13.0  # ставка НДФЛ на купон/курсовую разницу/доход от паёв, %
TRADE_COST_PCT = 0.3  # комиссия брокера/спред при покупке-продаже биржевого актива или обмене валюты, % годовых (грубая оценка)

def _net_of_all(gross: float, tradable: bool) -> float:
    """Доходность после вычета брокерской комиссии/спреда (только для биржевых
    активов и обмена валюты — у вклада её нет) и НДФЛ 13% с оставшегося дохода."""
    pretax = gross - TRADE_COST_PCT if tradable else gross
    return round(pretax * (1 - NDFL_RATE / 100), 2)

def _moex_history_paginated(url_base: str, max_rows: int = 400) -> List[dict]:
    """История MOEX ISS постранично (по умолчанию отдаёт только первые 100
    строк с начала диапазона) — докручиваем через `start`, пока не соберём
    весь запрошенный период."""
    all_rows: List[dict] = []
    start = 0
    while len(all_rows) < max_rows:
        data = _moex_fetch_json(f"{url_base}&start={start}")
        hist = data["history"]
        cols = list(hist["metadata"].keys())
        rows_ = [dict(zip(cols, r)) for r in hist["data"] if r[cols.index("CLOSE")] is not None]
        if not rows_:
            break
        all_rows.extend(rows_)
        if len(rows_) < 100:
            break
        start += 100
    return all_rows

def _moex_mmf_spread() -> Optional[float]:
    """Реальный спред фонда денежного рынка (LQDT) к ключевой ставке:
    аннуализированный рост стоимости пая за последние ~184 дня минус
    средняя ключевая ставка за тот же период. Возвращает None при сбое."""
    try:
        till = date.today()
        frm = till - relativedelta(days=MMF_WINDOW_DAYS)
        rows_ = _moex_history_paginated(
            f"https://iss.moex.com/iss/history/engines/stock/markets/shares/securities/{MMF_FUND_SECID}.json"
            f"?from={frm.isoformat()}&till={till.isoformat()}"
        )
        if len(rows_) < 10:
            return None
        d0 = datetime.strptime(rows_[0]["TRADEDATE"], "%Y-%m-%d").date()
        d1 = datetime.strptime(rows_[-1]["TRADEDATE"], "%Y-%m-%d").date()
        p0, p1 = rows_[0]["CLOSE"], rows_[-1]["CLOSE"]
        days = (d1 - d0).days
        if days < 60 or p0 is None or p1 is None:
            return None
        ann = ((p1 / p0) ** (365 / days) - 1) * 100
        key_lookup = yf.key_rate_lookup()
        avg_key = sum(key_lookup(d0 + timedelta(days=n)) for n in range(days)) / days
        return ann - avg_key
    except Exception:
        return None

@app.route("/api/yield/summary")
def api_yield_summary():
    try:
        # Независимые MOEX-запросы (у каждого свой сетевой таймаут ~6с) запускаем
        # ПАРАЛЛЕЛЬНО, а не по очереди — иначе на serverless-контейнере с лимитом
        # исполнения 10с 5 последовательных запросов легко уводят в таймаут, даже
        # если каждый из них в отдельности укладывается в разумное время.
        with ThreadPoolExecutor(max_workers=5) as ex:
            f_mmf = ex.submit(_moex_mmf_spread)
            f_live = ex.submit(_moex_live_ofz_curve)
            f_cny_fx = ex.submit(_moex_fx_last, "CNYRUB_TOM")
            f_usd_fx = ex.submit(_moex_fx_last, "USD000UTSTOM")
            f_cny_ofz = ex.submit(_moex_best_cny_ofz)
            mmf_spread_live = f_mmf.result()
            live = f_live.result()
            cny_today_actual = f_cny_fx.result() or CNY_RUB_SPOT_FALLBACK
            usd_today_actual = f_usd_fx.result() or USD_RUB_SPOT_FALLBACK
            cny_ofz = f_cny_ofz.result()

        live_date, live_curve = live if live else (None, None)
        rows, as_of, mmf_spread = yf.expected_asset_returns(
            mmf_spread=mmf_spread_live if mmf_spread_live is not None else MMF_SPREAD_FALLBACK,
            live_date=live_date, live_curve=live_curve,
        )
        for r in rows:
            tradable = r["asset"] != "Депозит на 1 год"
            r["gross"] = r.pop("value")
            r["net"] = _net_of_all(r["gross"], tradable)
            if "value_p20" in r:
                r["gross_p20"] = r.pop("value_p20")
                r["gross_p80"] = r.pop("value_p80")
                r["net_p20"] = _net_of_all(r["gross_p20"], tradable)
                r["net_p80"] = _net_of_all(r["gross_p80"], tradable)
        df = cache.get_df()

        infl = df[(df["Alias"] == "Инфляция") & (df["Unit"] == "% г/г")].sort_values("Date")
        infl_row = _nearest_row(infl, as_of + relativedelta(years=1))
        expected_inflation = float(infl_row["Value"])

        cny = df[df["Alias"] == "CNY/RUB"].sort_values("Date")
        future_row = _nearest_row(cny, as_of + relativedelta(years=1))
        cny_future_model = float(future_row["Value"])
        cny_return = round((cny_future_model / cny_today_actual - 1) * 100, 2)
        rows.append({"asset": "Юань (спот)", "gross": cny_return, "net": _net_of_all(cny_return, True)})

        usd = df[df["Alias"] == "USD/RUB"].sort_values("Date")
        usd_future_row = _nearest_row(usd, as_of + relativedelta(years=1))
        usd_future_model = float(usd_future_row["Value"])
        usd_return = round((usd_future_model / usd_today_actual - 1) * 100, 2)
        rows.append({"asset": "Доллар (спот)", "gross": usd_return, "net": _net_of_all(usd_return, True)})

        if cny_ofz is not None:
            ofz_name, ofz_ytm_cny, ofz_isin = cny_ofz
            # доходность в CNY компаундируется с ожидаемым движением курса юаня
            combined = round(((1 + ofz_ytm_cny / 100) * (1 + cny_return / 100) - 1) * 100, 2)
            rows.append({"asset": ofz_name, "gross": combined, "net": _net_of_all(combined, True)})

        for r in rows:
            r["real"] = round(((1 + r["net"] / 100) / (1 + expected_inflation / 100) - 1) * 100, 2)
            if "net_p20" in r:
                r["real_p20"] = round(((1 + r["net_p20"] / 100) / (1 + expected_inflation / 100) - 1) * 100, 2)
                r["real_p80"] = round(((1 + r["net_p80"] / 100) / (1 + expected_inflation / 100) - 1) * 100, 2)

        rows.sort(key=lambda r: r["gross"])

        return jsonify({
            "rows": rows, "as_of": as_of.isoformat(), "mmf_spread": mmf_spread,
            "ndfl_rate": NDFL_RATE, "trade_cost_pct": TRADE_COST_PCT,
            "expected_inflation": round(expected_inflation, 2),
            "inflation_date": infl_row["Date"].date().isoformat(),
            "cny_today": round(cny_today_actual, 3),
            "cny_future": round(cny_future_model, 3),
            "cny_future_date": future_row["Date"].date().isoformat(),
            "usd_today": round(usd_today_actual, 3),
            "usd_future": round(usd_future_model, 3),
            "usd_future_date": usd_future_row["Date"].date().isoformat(),
            "cny_ofz": {"name": cny_ofz[0], "ytm_cny": cny_ofz[1], "isin": cny_ofz[2]} if cny_ofz else None,
        })
    except Exception as e:
        return jsonify({"error": f"Не удалось построить таблицу ожидаемых доходностей: {e}"}), 500

@app.route("/inflation/weekly")
def inflation_weekly():
    return render_template(
        "inflation_weekly.html",
        active_page="inflation_weekly",
        page_title="Недельная инфляция",
        page_subtitle="SAAR-дашборд по данным Росстата и Банка России"
    )

@app.route("/inflation/monthly")
def inflation_monthly():
    return render_template(
        "inflation_monthly.html",
        active_page="inflation_monthly",
        page_title="Месячная инфляция",
        page_subtitle=""
    )

@app.route("/ruble")
def ruble():
    return render_template(
        "ruble.html",
        active_page="ruble",
        page_title="Рубль",
        page_subtitle=""
    )

NGD_DATA_FILE = os.path.join(BASE_DIR, "data", "ngd_dashboard.json")


@app.route("/ngd")
def ngd():
    return render_template(
        "ngd.html",
        active_page="ngd",
        page_title="НГД",
        page_subtitle="Нефтегазовые доходы бюджета и валютные операции по бюджетному правилу"
    )

@app.route("/api/ngd")
def api_ngd():
    # Файл пишет модель НГД (ngd_model.py) при каждом запуске
    try:
        with open(NGD_DATA_FILE, encoding="utf-8") as f:
            return app.response_class(f.read(), mimetype="application/json")
    except FileNotFoundError:
        return jsonify({"error": "Нет данных НГД: запустите модель ngd_model.py"}), 404

CBR_MODELS_FILE = os.path.join(BASE_DIR, "data", "cbr_models.json")


@app.route("/cbr-models")
def cbr_models():
    return render_template(
        "cbr_models.html",
        active_page="cbr_models",
        page_title="Модели ЦБ",
        page_subtitle="Правило Тейлора из квартальной прогнозной модели Банка России: калибровка, бэктест, наш прогноз"
    )

@app.route("/api/cbr_models")
def api_cbr_models():
    # Файл пишет проект «Правило Тейлора» (src/export_site.py) при каждом запуске
    try:
        with open(CBR_MODELS_FILE, encoding="utf-8") as f:
            return app.response_class(f.read(), mimetype="application/json")
    except FileNotFoundError:
        return jsonify({"error": "Нет данных: запустите src/export_site.py в проекте «Правило Тейлора»"}), 404

@app.route("/api/ruble/fx")
def api_ruble_fx():
    try:
        df = cache.get_df()
        as_of = date.today()
        aliases = {"usd": "USD/RUB", "eur": "EUR/RUB", "cny": "CNY/RUB"}
        series = {}
        dates = None
        for key, alias in aliases.items():
            sub = df[df["Alias"] == alias].sort_values("Date")
            d = [row.date().isoformat() for row in sub["Date"]]
            if dates is None:
                dates = d
            series[key] = [round(float(v), 4) for v in sub["Value"]]
        return jsonify({"dates": dates, "as_of": as_of.isoformat(), **series})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить курс валют: {e}"}), 500

def _af_fact_value(df: pd.DataFrame, indicator: str, quarter_label: str) -> Optional[Dict]:
    """Факт для серой линии на графике прогнозов аналитиков.
    Возвращает {"value":.., "preliminary": bool} или None, если факта ещё
    совсем нет. preliminary=True — квартал ещё не завершился, это среднее
    по уже прошедшей части (для usdrub — по уже закрывшимся месяцам внутри
    квартала; для brent — по тем месяцам, что уже попали в таблицу
    последних фактических месяцев в файле аналитиков). Ставка, инфляция и
    ВВП предварительного факта не дают — только окончательный, по итогам
    всего квартала/года."""
    q_end = af.quarter_end_date(quarter_label)
    today = date.today()
    is_final_quarter = q_end <= today

    if indicator == "key_rate":
        if not is_final_quarter:
            return None
        return {"value": round(yf.key_rate_lookup()(q_end), 2), "preliminary": False}

    if indicator == "usdrub":
        year, q = af.quarter_sort_key(quarter_label)
        q_start = date(year, (q - 1) * 3 + 1, 1)
        sub = df[(df["Alias"] == "USD/RUB") & (df["Unit"] == "руб.")]
        sub = sub[(sub["Date"] >= pd.Timestamp(q_start)) & (sub["Date"] <= pd.Timestamp(q_end))]
        sub = sub[sub["Date"] <= pd.Timestamp(today)]  # только уже закрывшиеся месяцы — иначе это ещё прогноз, не факт
        if sub.empty:
            return None
        value = round(float(sub["Value"].astype(float).mean()), 2)
        return {"value": value, "preliminary": not is_final_quarter}

    if indicator == "inflation":
        if not is_final_quarter:
            return None
        sub = df[(df["Alias"] == "Инфляция") & (df["Unit"] == "% г/г") & (df["Date"] == pd.Timestamp(q_end))]
        return {"value": round(float(sub["Value"].iloc[-1]), 2), "preliminary": False} if not sub.empty else None

    if indicator == "gdp":
        if not is_final_quarter:
            return None
        sub = df[(df["Alias"] == "ВВП") & (df["Unit"] == "% г/г") & (df["Date"].dt.year == q_end.year)]
        return {"value": round(float(sub["Value"].iloc[-1]), 2), "preliminary": False} if not sub.empty else None

    if indicator == "brent":
        if is_final_quarter:
            value = af.top_block_fact("brent", quarter_label)
            if value is not None:
                return {"value": value, "preliminary": False}
        value = af.recent_actual_quarter_avg("brent", quarter_label)
        return {"value": value, "preliminary": True} if value is not None else None

    return None

@app.route("/api/analyst_forecasts")
def api_analyst_forecasts():
    try:
        indicator = request.args.get("indicator", default="key_rate", type=str).strip()
        if indicator not in {i["key"] for i in af.INDICATORS}:
            indicator = "key_rate"

        quarter = request.args.get("quarter", default="", type=str).strip()
        quarters = af.available_quarters(indicator)
        if not quarter or quarter not in quarters:
            quarter = af.current_quarter_label()
            if quarter not in quarters and quarters:
                quarter = quarters[-1]

        payload = af.quarter_series(indicator, quarter) if quarter else {
            "indicator": indicator, "quarter": quarter, "quarter_end": None,
            "analysts": [], "series": {}, "consensus_name": None, "consensus_series": [],
        }

        fact = None
        if quarter:
            df = cache.get_df()
            result = _af_fact_value(df, indicator, quarter)
            if result is not None:
                fact = {"date": payload["quarter_end"], "value": result["value"], "preliminary": result["preliminary"]}

        return jsonify({
            **payload, "available_quarters": quarters,
            "indicators": af.INDICATORS, "fact": fact,
        })
    except Exception as e:
        return jsonify({"error": f"Не удалось построить прогноз аналитиков: {e}"}), 500

@app.route("/api/indicators")
def api_indicators():
    try:
        df = cache.get_df()
        inds = sorted(df["Alias"].dropna().unique().tolist())
        prefer = ["Инфляция", "ВВП", "USD/RUB", "EUR/RUB", "CNY/RUB", "Ключевая ставка"]
        ordered = sorted(inds, key=lambda x: (prefer.index(x) if x in prefer else 999, x))
        return jsonify({"indicators": ordered, "count": len(ordered)})
    except Exception as e:
        return jsonify({"error": f"Не удалось получить индикаторы: {e}"}), 500

@app.route("/table_data")
def table_data():
    try:
        name = request.args.get("names", type=str, default="")
        dfrom = request.args.get("from", type=str, default="") or _start_of_current_year_str()
        dto = request.args.get("to", type=str, default="")
        freq_view = request.args.get("freq_view", type=str, default="AUTO").upper()

        df = cache.get_df()
        if not name:
            name = df["Alias"].iloc[0]

        columns, rows, total, native, period_years, _cols_meta = _build_table(df, name, freq_view, dfrom, dto)
        return jsonify({
            "columns": columns, "rows": rows, "total": total,
            "native": native, "freq_view": freq_view,
            "period_years": period_years
        })
    except Exception as e:
        return jsonify({"error": f"Не удалось построить таблицу: {e}"}), 500

@app.route("/api/series_multi")
def api_series_multi():
    try:
        name = request.args.get("names", type=str, default="")
        dfrom = request.args.get("from", type=str, default="") or _start_of_current_year_str()
        dto = request.args.get("to", type=str, default="")
        freq_view = request.args.get("freq_view", type=str, default="AUTO").upper()

        df = cache.get_df()
        if not name:
            name = df["Alias"].iloc[0]

        x_iso, series, meta, period_years = _build_series(df, name, freq_view, dfrom, dto)
        return jsonify({"meta": {"x": x_iso, **meta, "period_years": period_years}, "series": series})
    except Exception as e:
        return jsonify({"error": f"Не удалось построить серию: {e}"}), 500

@app.route("/export")
def export_excel():
    directory = os.path.dirname(EXPORT_FILE)
    fname = os.path.basename(EXPORT_FILE)
    if not os.path.exists(EXPORT_FILE):
        abort(404, "Файл не найден")
    return send_from_directory(directory=directory, path=fname, as_attachment=True)

@app.route("/robots.txt")
def robots_txt():
    robots_path = os.path.join(BASE_DIR, "robots.txt")
    if os.path.exists(robots_path):
        return send_from_directory(BASE_DIR, "robots.txt")
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": time.time()})

@app.route("/diag")
def diag():
    try:
        df = cache.get_df()
        info = {
            "file": DATA_FILE,
            "sheet": MAIN_SHEET,
            "columns": list(df.columns),
            "rows_total": int(len(df)),
            "aliases_total": int(df["Alias"].nunique()) if "Alias" in df.columns else None,
            "alias_samples": df["Alias"].dropna().unique().tolist()[:20] if "Alias" in df.columns else [],
        }
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=True)
