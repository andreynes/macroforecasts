import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
from flask import (
    Flask, jsonify, render_template, request, send_from_directory,
    Response, abort
)

# -----------------------------
# Конфигурация
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Путь к твоему Excel с листом "main"
DATA_FILE = os.getenv("DATA_FILE", os.path.join(BASE_DIR, "data", "macro_data.xlsx"))
MAIN_SHEET = os.getenv("MAIN_SHEET", "main")

# Для экспорта (кнопка "Экспорт в Excel")
EXPORT_FILE = os.getenv("EXPORT_FILE", DATA_FILE)

# (Опционально) базовая авторизация – по умолчанию выключено.
REQUIRE_BASIC_AUTH = os.getenv("REQUIRE_BASIC_AUTH", "0") == "1"
BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER", "user")
BASIC_AUTH_PASS = os.getenv("BASIC_AUTH_PASS", "pass")

# Карта "родной" частоты для принудительной фиксации
# name/alias -> 'M'|'Q'|'A'
NATIVE_FREQ_LOCK = {
    "Инфляция, % м/м": "M",
    "ВВП, % г/г": "A",
}

# Перевод частот Pandas
FREQ_RULE = {
    "M": "M",
    "Q": "Q",
    "A": "A-DEC",  # год заканчивается в декабре
}

# -----------------------------
# Инициализация Flask
# -----------------------------
app = Flask(__name__)

# --------- защита от индексации + robots.txt ----------
@app.after_request
def add_noindex_header(resp):
    # Не позволяем поисковикам индексировать
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp

@app.route("/robots.txt")
def robots_txt():
    text = "User-agent: *\nDisallow: /\n"
    return Response(text, mimetype="text/plain")

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
# -------------------------------------------------------


# -----------------------------
# Кэш данных
# -----------------------------
class DataCache:
    def __init__(self, path: str, sheet: str):
        self.path = path
        self.sheet = sheet
        self.mtime = 0.0
        self.df: Optional[pd.DataFrame] = None

    def _read_excel(self) -> pd.DataFrame:
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Не найден файл данных: {self.path}")

        df = pd.read_excel(self.path, sheet_name=self.sheet)
        # Ожидаем столбцы: Date, Name, Value, Unit, Freq, Method, Alias
        # Приводим к стандартным именам (на случай регистров/языков)
        cols_map = {
            "date": "Date",
            "дата": "Date",
            "name": "Name",
            "indicator": "Name",
            "naimenovanie": "Name",
            "value": "Value",
            "znachenie": "Value",
            "unit": "Unit",
            "единицы": "Unit",
            "freq": "Freq",
            "частота": "Freq",
            "method": "Method",
            "метод": "Method",
            "alias": "Alias",
            "псевдоним": "Alias",
        }
        # нормализуем имена
        std_cols = {}
        for c in df.columns:
            key = str(c).strip()
            k_low = key.lower()
            std_cols[c] = cols_map.get(k_low, key)
        df = df.rename(columns=std_cols)

        required = ["Date", "Name", "Value", "Unit", "Freq", "Method"]
        for r in required:
            if r not in df.columns:
                raise ValueError(f"В листе '{self.sheet}' отсутствует обязательный столбец: {r}")

        # Alias не обязателен
        if "Alias" not in df.columns:
            df["Alias"] = df["Name"]

        # Приводим типы
        df["Date"] = pd.to_datetime(df["Date"])
        # нормализуем Freq в {M,Q,A}
        df["Freq"] = df["Freq"].astype(str).str.upper().str.strip().map(
            {"M": "M", "Q": "Q", "A": "A", "A-DEC": "A"}
        ).fillna("M")
        # Method: avg|eop
        df["Method"] = df["Method"].astype(str).str.lower().str.strip().map(
            {"avg": "avg", "average": "avg", "mean": "avg",
             "eop": "eop", "end": "eop", "last": "eop"}
        ).fillna("avg")

        # Сортировка для корректной агрегaции
        df = df.sort_values(["Alias", "Date"]).reset_index(drop=True)
        return df

    def get_df(self) -> pd.DataFrame:
        try:
            mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            raise
        if self.df is None or mtime != self.mtime:
            self.df = self._read_excel()
            self.mtime = mtime
        return self.df.copy()


cache = DataCache(DATA_FILE, MAIN_SHEET)

# -----------------------------
# Утилиты
# -----------------------------
def _format_date(dt: pd.Timestamp) -> str:
    # ДД.ММ.ГГГГ
    return dt.strftime("%d.%m.%Y")

def _round_val(x: Optional[float]) -> Optional[str]:
    if x is None or pd.isna(x):
        return ""
    if abs(x) >= 1:
        return f"{x:.1f}"
    return f"{x:.2f}"

def _alias_unit_method(df: pd.DataFrame, alias: str) -> Tuple[str, str, str]:
    sub = df[df["Alias"] == alias]
    if sub.empty:
        return alias, "", "avg"
    name = sub["Alias"].iloc[0]
    unit = sub["Unit"].dropna().iloc[0] if sub["Unit"].notna().any() else ""
    method = sub["Method"].iloc[0]  # 'avg' | 'eop'
    return name, unit, method

def _native_freq(df: pd.DataFrame, alias: str) -> str:
    sub = df[df["Alias"] == alias]
    if sub.empty:
        return "M"
    return sub["Freq"].iloc[0]

def _resample_series(s: pd.Series, freq: str, method: str) -> pd.Series:
    """
    s: индекс DatetimeIndex, значения float
    freq: 'M'|'Q'|'A'
    method: 'avg'|'eop'
    """
    rule = FREQ_RULE.get(freq, "M")
    if method == "eop":
        return s.resample(rule).last()
    else:
        return s.resample(rule).mean()

def _apply_period_filter(df: pd.DataFrame, dfrom: Optional[str], dto: Optional[str]) -> pd.DataFrame:
    if dfrom:
        try:
            df = df[df["Date"] >= pd.to_datetime(dfrom)]
        except Exception:
            pass
    if dto:
        try:
            df = df[df["Date"] <= pd.to_datetime(dto)]
        except Exception:
            pass
    return df

def _build_table(df: pd.DataFrame, alias: str, freq_view: str,
                 dfrom: Optional[str], dto: Optional[str]) -> Tuple[List[str], List[List[str]], int, str]:
    """
    Возвращает: (columns, rows, total, native_freq)
    """
    sub = df[df["Alias"] == alias].copy()
    if sub.empty:
        return ["Дата", alias], [], 0, "M"

    # Родная частота
    native = sub["Freq"].iloc[0]
    # Если для этого показателя есть жёсткая фиксация — игнорируем freq_view
    locked = NATIVE_FREQ_LOCK.get(alias)
    if locked:
        freq_use = locked
    else:
        if freq_view == "AUTO":
            freq_use = native
        else:
            freq_use = freq_view

    name, unit, method = _alias_unit_method(df, alias)

    # Строим серию
    s = sub.set_index("Date")["Value"].astype(float).sort_index()

    # Агрегируем при необходимости
    if freq_use != native:
        s = _resample_series(s, freq_use, method)

    # Фильтр по датам (после агрегации)
    s = _apply_period_filter(s.to_frame("Value").reset_index(), dfrom, dto).set_index("Date")["Value"]

    # Таблица
    method_text = "в среднем за период" if method == "avg" else "на конец периода"
    col2 = f"{name} ({method_text}, {unit})" if unit else f"{name} ({method_text})"
    columns = ["Дата", col2]

    rows: List[List[str]] = []
    for dt, val in s.items():
        rows.append([_format_date(pd.to_datetime(dt)), _round_val(val)])

    total = len(rows)
    return columns, rows, total, native

def _build_series(df: pd.DataFrame, alias: str, freq_view: str,
                  dfrom: Optional[str], dto: Optional[str]) -> Tuple[List[str], List[float], Dict]:
    sub = df[df["Alias"] == alias].copy()
    if sub.empty:
        return [], [], {"native": "M"}

    native = sub["Freq"].iloc[0]
    locked = NATIVE_FREQ_LOCK.get(alias)
    if locked:
        freq_use = locked
    else:
        if freq_view == "AUTO":
            freq_use = native
        else:
            freq_use = freq_view

    name, unit, method = _alias_unit_method(df, alias)
    s = sub.set_index("Date")["Value"].astype(float).sort_index()
    if freq_use != native:
        s = _resample_series(s, freq_use, method)

    s = _apply_period_filter(s.to_frame("Value").reset_index(), dfrom, dto).set_index("Date")["Value"]

    # Ось X: ISO (для Plotly), подписи у тебя форматируются на клиенте
    x_iso = [pd.to_datetime(d).strftime("%Y-%m-%d") for d in s.index]
    y = [None if pd.isna(v) else float(v) for v in s.values]

    meta = {"native": native, "freq_used": freq_use, "name": name, "unit": unit, "method": method}
    return x_iso, y, meta

# -----------------------------
# Вьюхи
# -----------------------------
@app.route("/")
def index():
    # Параметры запроса (лендинг открывается с любыми)
    qs_names = request.args.get("names", default="", type=str)
    qs_from = request.args.get("from", default="", type=str)
    qs_to = request.args.get("to", default="", type=str)
    qs_freq = request.args.get("freq_view", default="AUTO", type=str).upper()

    df = cache.get_df()

    # Если не передали показатель – берём первый по алфавиту (но стараемся «USD/RUB»)
    indicators = sorted(df["Alias"].unique().tolist())
    if not qs_names:
        prefer = ["USD/RUB", "Ключевая ставка"]
        chosen = None
        for p in prefer:
            if p in indicators:
                chosen = p
                break
        qs_names = chosen or indicators[0]

    # Собираем стартовую таблицу
    columns, rows, total, native = _build_table(df, qs_names, qs_freq, qs_from, qs_to)

    last_updated = f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    source = "Источник: Собственные расчёты на основе файла пользователя"

    return render_template(
        "index.html",
        columns=columns,
        data=rows,
        total_rows=total,
        source=source,
        last_updated=last_updated,
        # для JS
        qs_names=qs_names,
        qs_from=qs_from,
        qs_to=qs_to,
        qs_freq=qs_freq
    )

@app.route("/api/indicators")
def api_indicators():
    df = cache.get_df()
    inds = df["Alias"].dropna().unique().tolist()
    # Отсортируем, но популярные в начало
    prefer = ["USD/RUB", "Инфляция, % м/м", "Ключевая ставка", "ВВП, % г/г"]
    ordered = sorted(inds, key=lambda x: (prefer.index(x) if x in prefer else 999, x))
    return jsonify({"indicators": ordered})

@app.route("/table_data")
def table_data():
    name = request.args.get("names", type=str, default="")
    dfrom = request.args.get("from", type=str, default="")
    dto = request.args.get("to", type=str, default="")
    freq_view = request.args.get("freq_view", type=str, default="AUTO").upper()

    df = cache.get_df()
    if not name:
        # Если не указали, берём первый
        name = df["Alias"].iloc[0]

    columns, rows, total, native = _build_table(df, name, freq_view, dfrom, dto)
    return jsonify({
        "columns": columns,
        "rows": rows,
        "total": total,
        "native": native,
        "freq_view": freq_view
    })

@app.route("/api/series_multi")
def api_series_multi():
    name = request.args.get("names", type=str, default="")
    dfrom = request.args.get("from", type=str, default="")
    dto = request.args.get("to", type=str, default="")
    freq_view = request.args.get("freq_view", type=str, default="AUTO").upper()

    df = cache.get_df()
    if not name:
        name = df["Alias"].iloc[0]

    x_iso, y, meta = _build_series(df, name, freq_view, dfrom, dto)
    series = [{"name": meta.get("name", name), "y": y}]
    return jsonify({
        "meta": {"x": x_iso, **meta},
        "series": series
    })

@app.route("/export")
def export_excel():
    """Кнопка «Экспорт в Excel» — отдаем исходный файл."""
    directory = os.path.dirname(EXPORT_FILE)
    fname = os.path.basename(EXPORT_FILE)
    if not os.path.exists(EXPORT_FILE):
        abort(404, "Файл не найден на сервере")
    return send_from_directory(directory=directory, path=fname, as_attachment=True)

# --------------- dev helper ---------------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": time.time()})


from flask import send_from_directory
import os

@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(os.path.join(app.root_path), 'robots.txt')

# -----------------------------
# Запуск
# -----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=True)
