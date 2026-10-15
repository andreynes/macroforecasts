"""Прогноз доходностей: ключевая ставка (по датам заседаний ЦБ), депозиты, кривая ОФЗ."""
import os
import bisect
import time
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YIELD_FILE = os.getenv("YIELD_FILE", os.path.join(BASE_DIR, "yield_forecast_data.xlsx"))
IRS_FILE = os.getenv("IRS_FILE", os.path.join(BASE_DIR, "Денежный рынок – Ставки_исправлено_v2.xlsx"))
IRS_TENORS = [1, 2, 5, 10]
# "Avg next NY" — подразумеваемая кривой IRS СРЕДНЯЯ ключевая ставка от сегодня
# до горизонта N лет (капитализация/сложный процент свопа вычищены, term
# premium сохранён) — в отличие от сырой пар-ставки свопа (колонки "NY"),
# это МЕТОДОЛОГИЧЕСКИ то же самое, что наш forward_average_key_rate (простое
# среднее) — единственно корректная база для сравнения с нашим прогнозом.
# Есть только для эры KEYRATE (с 2022-10-24, но фактически с 2023-01-09 —
# требуется глубина кривой на весь горизонт) и только на этих 5 сроках.
IRS_AVG_TENORS = [1, 2, 3, 5, 10]
MAIN_DATA_FILE = os.getenv("DATA_FILE", os.path.join(BASE_DIR, "macro_main_linked.xlsx"))

_excel_cache: Dict[tuple, Tuple[float, pd.DataFrame]] = {}


def _cached_read_excel(path: str, **kwargs) -> pd.DataFrame:
    """pd.read_excel с кэшем по мтайму файла — модели 1-3 читают одни и те
    же файлы (ОФЗ, IRS, ИБК, план Минфина, CPI/GDP) по многу раз за один
    вызов ансамбля (openpyxl на файл в тысячи строк — сотни мс каждый раз),
    а "Прогноз доходностей" вызывает ансамбль дважды за рендер страницы
    (график + таблица) — без кэша легко упереться в таймаут serverless-
    контейнера."""
    def _hashable(v):
        return tuple(v) if isinstance(v, list) else v

    key = (path, tuple(sorted((k, _hashable(v)) for k, v in kwargs.items())))
    mtime = os.path.getmtime(path)
    cached = _excel_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1].copy()
    df = pd.read_excel(path, **kwargs)
    _excel_cache[key] = (mtime, df)
    return df.copy()


_memo_cache: Dict[tuple, Tuple[float, object]] = {}
MEMO_TTL_SECONDS = 120.0


def _memoize(name: str, args: tuple, compute_fn):
    """Короткий TTL-кэш (2 мин) для дорогих, но СЦЕНАРНО-НЕЗАВИСИМЫХ
    промежуточных вычислений (регрессии/исторические спреды на нескольких
    сотнях строк) — с добавлением сценариев ставки (оптимист/пессимист) в
    ансамбль (см. ensemble_ofz_forecast) Модели 1 и 3 внутри одного вызова
    ансамбля считаются 3 раза подряд (база + 2 сценария), хотя вся их
    "тяжёлая" историческая часть (не зависящая от того, какой сценарий
    ставки подставлен для будущего) одна и та же — без этого кэша 3-кратный
    пересчёт этой части ощутимо приближал таймаут serverless-контейнера."""
    def _hashable(v):
        return tuple(v) if isinstance(v, list) else v

    key = (name, tuple(_hashable(a) for a in args))
    now = time.time()
    cached = _memo_cache.get(key)
    if cached and now - cached[0] < MEMO_TTL_SECONDS:
        return cached[1]
    result = compute_fn()
    _memo_cache[key] = (now, result)
    return result


# Официальный график заседаний Совета директоров Банка России на 2026 год
# (https://www.cbr.ru, подтверждено независимыми источниками). Заседания —
# всегда по пятницам, 8 раз в год (нет заседаний в январе/мае/августе/ноябре).
MEETINGS_2026: Dict[int, date] = {
    2: date(2026, 2, 13),
    3: date(2026, 3, 20),
    4: date(2026, 4, 24),
    6: date(2026, 6, 19),
    7: date(2026, 7, 24),
    9: date(2026, 9, 11),
    10: date(2026, 10, 23),
    12: date(2026, 12, 18),
}
# Порядковая пятница месяца (1-я/2-я/3-я/4-я), выведенная из графика на 2026 год.
# Используется как шаблон для проекции дат заседаний на 2027+ годы.
MEETING_ORDINAL_FRIDAY: Dict[int, int] = {
    m: (d.day - 1) // 7 + 1 for m, d in MEETINGS_2026.items()
}
MEETING_MONTHS = set(MEETING_ORDINAL_FRIDAY.keys())
CALENDAR_BASE_YEAR = 2026
FRIDAY = 4  # date.weekday(): Monday=0 ... Friday=4


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return date(year, month, 1 + delta + (n - 1) * 7)


def meeting_dates_for_year(year: int) -> Dict[int, date]:
    if year == CALENDAR_BASE_YEAR:
        return dict(MEETINGS_2026)
    return {
        m: _nth_weekday(year, m, FRIDAY, n)
        for m, n in MEETING_ORDINAL_FRIDAY.items()
    }


def meeting_calendar(start_year: int, end_year: int) -> Dict[int, Dict[int, date]]:
    return {y: meeting_dates_for_year(y) for y in range(start_year, end_year + 1)}


def _make_step_lookup(points: List[Tuple[date, float]]):
    dates = [p[0] for p in points]
    values = [p[1] for p in points]

    def lookup(d: date) -> float:
        i = bisect.bisect_right(dates, d) - 1
        return values[0] if i < 0 else values[i]

    return lookup


def _meeting_effective_date(meeting_date: date) -> date:
    """Дата, с которой новая ставка реально начинает действовать после
    решения СД (дата самого заседания — всегда пятница). До 2027 года новая
    ставка действует с понедельника после заседания (+3 дня), с 2027 года —
    со среды (+5 дней)."""
    offset_days = 3 if meeting_date.year < 2027 else 5
    return meeting_date + timedelta(days=offset_days)


def _repin_forecast_dates(dates: pd.Series, cal: Dict[int, Dict[int, date]]) -> List[date]:
    """Точка месяца, в котором есть заседание (2026+), переносится с конца
    месяца на реальную/проецируемую дату вступления в силу новой ставки
    (заседание + эффективный сдвиг, см. _meeting_effective_date). Остальные
    месяцы остаются на конец месяца."""
    out: List[date] = []
    for d in dates:
        if d.year >= CALENDAR_BASE_YEAR and d.month in MEETING_MONTHS:
            meeting_d = cal.get(d.year, {}).get(d.month, d)
            d = _meeting_effective_date(meeting_d)
        out.append(d)
    return out


def _fill_monthly_gaps(points: List[tuple]) -> List[tuple]:
    """Гарантирует хотя бы одну точку на каждый календарный месяц. Сдвиг
    даты на реальный день вступления в силу (см. _meeting_effective_date)
    может унести позднемесячное заседание в НАЧАЛО следующего месяца (напр.
    заседание в пятницу 28 апреля + 5 дней = среда 3 мая) — тогда исходный
    календарный месяц (апрель) остаётся вообще без точки. Заполняем такие
    "пустые" месяцы точкой на конец месяца со значением, действовавшим на
    тот момент (перенос последнего известного значения) — иначе наведение
    мышью не покажет значение за этот месяц."""
    if not points:
        return points
    out = [points[0]]
    for curr in points[1:]:
        y, m = out[-1][0].year, out[-1][0].month
        while True:
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            if (y, m) >= (curr[0].year, curr[0].month):
                break
            month_end = date(y, 12, 31) if m == 12 else date(y, m + 1, 1) - timedelta(days=1)
            out.append((month_end,) + out[-1][1:])
        out.append(curr)
    return out


def build_key_rate_series(extend_years: int = 3) -> List[Tuple[date, float]]:
    """Единый ступенчатый ряд ключевой ставки: история как есть (до 2026 года),
    прогноз — точка месяца, в котором есть заседание, перенесена с конца
    месяца на реальную/проецируемую дату вступления в силу новой ставки.
    Точки остальных месяцев остаются на конец месяца (значение не меняется
    до следующего заседания) — это не меняет форму ступеньки, но даёт
    наводку (hover) на каждый месяц, а не только на месяцы заседаний."""
    df = _cached_read_excel(YIELD_FILE, sheet_name="Ключевая ставка", header=0, usecols=[0, 1])
    df.columns = ["Date", "Rate"]
    df = df.dropna()
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    df = df.sort_values("Date").reset_index(drop=True)

    last_year = max(df["Date"].apply(lambda d: d.year).max(), CALENDAR_BASE_YEAR)
    cal = meeting_calendar(CALENDAR_BASE_YEAR, last_year)

    dates = _repin_forecast_dates(df["Date"], cal)
    points: List[Tuple[date, float]] = sorted(zip(dates, df["Rate"].astype(float)), key=lambda p: p[0])
    points = _fill_monthly_gaps(points)

    if points:
        last_d, last_v = points[-1]
        for y in range(last_d.year + 1, last_d.year + extend_years + 1):
            points.append((date(y, 12, 31), last_v))
    return points


QUANTILE_LOW = 0.20
QUANTILE_HIGH = 0.80


def build_key_rate_band(extend_years: int = 3) -> List[Tuple[date, float, float]]:
    """Полоса 20-80% квантиль вокруг базового прогноза ключевой ставки —
    только на прогнозном периоде (в истории сценариев нет). Оптимистичный,
    базовый и пессимистичный сценарии трактуются как условные 0%, 50% и
    100% процентили, а P20/P80 находятся линейной интерполяцией ВНУТРИ
    каждой половины отдельно (P20 между оптимистом и базовым, P80 между
    базовым и пессимистом) — это гарантирует, что базовая линия всегда
    строго внутри полосы, даже если она сильно смещена к одному из краёв
    диапазона (простая обрезка по общей ширине диапазона такого не
    гарантирует: напр. Base=13.75, Optimistic=13.50, Pessimistic=16.00 —
    обрезка по ширине дала бы нижнюю границу 14.00, выше самого базового
    прогноза).

    Даты репинятся к тем же датам вступления в силу, что и базовый прогноз
    (см. build_key_rate_series), чтобы полоса и основная линия были
    синхронны. Оптимистичный сценарий — НИЖНЯЯ граница диапазона (более
    быстрое смягчение ДКП), пессимистичный — ВЕРХНЯЯ (ставка дольше
    остаётся высокой)."""
    df = _cached_read_excel(YIELD_FILE, sheet_name="Ключевая ставка", header=1, usecols=[0, 1, 2, 3])
    df.columns = ["Date", "Base", "Optimistic", "Pessimistic"]
    df = df.dropna(subset=["Date"])
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    df = df.sort_values("Date").reset_index(drop=True)
    df = df.dropna(subset=["Optimistic", "Pessimistic"])

    last_year = max(df["Date"].apply(lambda d: d.year).max(), CALENDAR_BASE_YEAR)
    cal = meeting_calendar(CALENDAR_BASE_YEAR, last_year)

    dates = _repin_forecast_dates(df["Date"], cal)
    base = df["Base"].astype(float)
    opt = df["Optimistic"].astype(float)
    pes = df["Pessimistic"].astype(float)
    frac_lo = QUANTILE_LOW / 0.5
    frac_hi = (QUANTILE_HIGH - 0.5) / 0.5
    lo = opt + frac_lo * (base - opt)
    hi = base + frac_hi * (pes - base)
    points: List[Tuple[date, float, float]] = sorted(zip(dates, lo, hi), key=lambda p: p[0])
    points = _fill_monthly_gaps(points)

    if points:
        last_d, last_lo, last_hi = points[-1]
        for y in range(last_d.year + 1, last_d.year + extend_years + 1):
            points.append((date(y, 12, 31), last_lo, last_hi))
    return points


def key_rate_lookup_scenario(scenario: str, extend_years: int = 3):
    """Ступенчатый lookup ключевой ставки для сценария 'Optimistic' или
    'Pessimistic' — история (до начала прогноза, где сценариев нет)
    берётся из базового прогноза, дальше — заданный сценарий. Нужен, чтобы
    прогнать модели ОФЗ 1 и 3 не только на базовом прогнозе ставки, но и на
    крайних сценариях — иначе доверительный интервал ансамбля отражает
    только несогласие между 3 моделями при ОДНОМ и том же прогнозе ставки,
    а не саму неопределённость этого прогноза (см. ensemble_ofz_forecast)."""
    if scenario == "Base":
        return key_rate_lookup()
    df = _cached_read_excel(YIELD_FILE, sheet_name="Ключевая ставка", header=1, usecols=[0, 1, 2, 3])
    df.columns = ["Date", "Base", "Optimistic", "Pessimistic"]
    df = df.dropna(subset=["Date"])
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    df = df.sort_values("Date").reset_index(drop=True)

    last_year = max(df["Date"].apply(lambda d: d.year).max(), CALENDAR_BASE_YEAR)
    cal = meeting_calendar(CALENDAR_BASE_YEAR, last_year)
    dates = _repin_forecast_dates(df["Date"], cal)
    vals = [s if pd.notna(s) else b for s, b in zip(df[scenario].astype(float), df["Base"].astype(float))]
    points: List[Tuple[date, float]] = sorted(zip(dates, vals), key=lambda p: p[0])
    points = _fill_monthly_gaps(points)

    if points:
        last_d, last_v = points[-1]
        for y in range(last_d.year + 1, last_d.year + extend_years + 1):
            points.append((date(y, 12, 31), last_v))
    return _make_step_lookup(points)


def load_irs_curve() -> pd.DataFrame:
    """Кривая IRS RUB vs RUB KEYRATE (fixed-float своп на ключевую ставку,
    источник — Cbonds, репозиторий ОТС-свопов Московской Биржи (СПФИ)) —
    рыночно-подразумеваемая средняя ключевая ставка на срок свопа. В отличие
    от нашего модельного прогноза это цена реальных внебиржевых сделок,
    публикуемая ежедневно; файл обновляется вручную (нет открытого API)."""
    df = _cached_read_excel(IRS_FILE, sheet_name=0, header=0)
    df = df.rename(columns={"Дата": "Date"})
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    for col in df.columns:
        if col != "Date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("Date").reset_index(drop=True)


def market_implied_key_rate() -> Tuple[date, Dict[int, float]]:
    """Последние доступные значения кривой IRS на срока 1/2/5/10 лет —
    рыночный аналог наших модельных средних КС на те же горизонты. Берётся
    очищенная от капитализации "Avg next NY" (см. IRS_AVG_TENORS), а не сырая
    пар-ставка свопа — она сопоставима с нашим средним прогнозом напрямую."""
    df = load_irs_curve()
    cols = [f"Avg next {t}Y" for t in IRS_TENORS]
    latest = df.dropna(subset=cols, how="all").iloc[-1]
    values = {t: float(latest[f"Avg next {t}Y"]) for t in IRS_TENORS if pd.notna(latest[f"Avg next {t}Y"])}
    return latest["Date"], values


KEYRATE_SWAP_START = date(2022, 10, 24)  # первая дата IRS vs KEYRATE в файле; до неё — только MOSPRIME


def irs_expectation_vs_fact(tenors: Tuple[int, ...] = (1, 2, 5, 10)) -> Dict[int, Dict[str, list]]:
    """То же самое, что key_rate_expectation_vs_fact, но 'ожидание' — это
    котировка свопа на срок N (а не доходность ОФЗ). У свопа на ставку нет
    кредитного/ликвидностного риска гособлигации и премии за дюрацию тела
    бумаги — поэтому разрыв с фактом здесь должен быть чище и точнее
    отражать именно ошибку ожиданий по траектории КС, а не суммарную премию
    ОФЗ.

    До 24.10.2022 своп на ключевую ставку не существовал — единственный
    актуально котировавшийся бенчмарк тогда был IRS RUB vs 3M MOSPRIME
    (колонки *Y_MOSPRIME, сырая пар-ставка — очищенной версии для этой эры
    нет); с 24.10.2022 — IRS RUB vs RUB KEYRATE, и для этой эры берётся уже
    очищенная от капитализации "Avg next NY" (см. IRS_AVG_TENORS) — она
    сопоставима с "фактом" (простым средним по forward_average_key_rate)
    напрямую, в отличие от сырой пар-ставки свопа. Эти два ряда НЕ
    склеиваются в одну линию: на дату перехода между ними обнаружен разрыв
    уровня почти в 2 п.п. (MOSPRIME торговался с премией к ключевой ставке
    из-за стресса на межбанке в конце 2022 г.) — склейка исказила бы
    картину, выдав смену бенчмарка за скачок ожиданий. Поэтому возвращаются
    ДВЕ отдельные серии ожиданий (expected_mosprime до даты перехода,
    expected_keyrate — после), которые нужно рисовать как разные линии."""
    key_lookup = key_rate_lookup()
    irs = load_irs_curve()
    today = date.today()

    result: Dict[int, Dict[str, list]] = {}
    for t in tenors:
        col_keyrate = f"Avg next {t}Y"
        col_mosprime = f"{t}Y_MOSPRIME"
        cols = [c for c in (col_keyrate, col_mosprime) if c in irs.columns]
        monthly = (
            irs.set_index(pd.to_datetime(irs["Date"]))[cols]
            .resample("ME").last().dropna(how="all")
        )
        monthly.index = monthly.index.date

        dates, exp_mosprime, exp_keyrate, fact = [], [], [], []
        for d in monthly.index:
            v_mp = monthly.loc[d, col_mosprime] if col_mosprime in monthly.columns else None
            v_kr = monthly.loc[d, col_keyrate] if col_keyrate in monthly.columns else None
            if pd.isna(v_mp) and pd.isna(v_kr):
                continue
            dates.append(d.isoformat())
            if d < KEYRATE_SWAP_START:
                exp_mosprime.append(round(float(v_mp), 3) if pd.notna(v_mp) else None)
                exp_keyrate.append(None)
            else:
                exp_mosprime.append(None)
                exp_keyrate.append(round(float(v_kr), 3) if pd.notna(v_kr) else None)
            end = d + relativedelta(years=t)
            if end <= today:
                fact.append(round(forward_average_key_rate(key_lookup, d, days=t * 365), 3))
            else:
                fact.append(None)
        result[t] = {
            "dates": dates, "expected_mosprime": exp_mosprime, "expected_keyrate": exp_keyrate, "fact": fact,
        }
    return result


def key_rate_lookup():
    return _make_step_lookup(build_key_rate_series())


def forward_average_key_rate(key_lookup, t: date, days: int = 365, step_days: int = 5) -> float:
    """Средняя ожидаемая ключевая ставка за год вперёд от даты t (по нашей же
    ступенчатой истории+прогнозу) — то, что реально закладывается в ставку
    годового вклада, а не значение ставки на конкретный день t."""
    vals = [key_lookup(t + timedelta(days=n)) for n in range(0, days, step_days)]
    return float(sum(vals) / len(vals))


def forward_average_key_rate_band(band_points: List[Tuple[date, float, float]], t: date, days: int = 365,
                                   step_days: int = 5) -> Tuple[float, float]:
    """То же самое, что forward_average_key_rate, но для полосы 20-80%
    (build_key_rate_band) — средние P20/P80 ставки за год вперёд от даты t."""
    lo_lookup = _make_step_lookup([(d, lo) for d, lo, _hi in band_points])
    hi_lookup = _make_step_lookup([(d, hi) for d, _lo, hi in band_points])
    lo_vals = [lo_lookup(t + timedelta(days=n)) for n in range(0, days, step_days)]
    hi_vals = [hi_lookup(t + timedelta(days=n)) for n in range(0, days, step_days)]
    return float(sum(lo_vals) / len(lo_vals)), float(sum(hi_vals) / len(hi_vals))


# ---------------- Депозиты ----------------
_ROMAN_DECADE = {"I": 1, "II": 2, "III": 3}
_DECADE_DAY = {1: 1, 2: 11, 3: 21}


def _parse_decade_label(label: str) -> Tuple[date, int]:
    roman, month, year = str(label).strip().split(".")
    decade = _ROMAN_DECADE[roman]
    return date(int(year), int(month), _DECADE_DAY[decade]), decade


def _next_decade(d: date) -> date:
    if d.day == 1:
        return d.replace(day=11)
    if d.day == 11:
        return d.replace(day=21)
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def load_deposit_history() -> pd.DataFrame:
    df = _cached_read_excel(YIELD_FILE, sheet_name="Ставка по депозитам", header=0, usecols=[0, 1])
    df.columns = ["Decade", "Rate"]
    df = df.dropna()
    parsed = df["Decade"].apply(_parse_decade_label)
    df["Date"] = parsed.apply(lambda x: x[0])
    df = df.sort_values("Date").reset_index(drop=True)
    return df[["Date", "Rate"]]


def _ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    X1 = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X1, y, rcond=None)
    return beta


def _ar1_fit(series: np.ndarray) -> Tuple[float, float]:
    y, x = series[1:], series[:-1]
    beta = _ols(x.reshape(-1, 1), y)
    return float(beta[0]), float(beta[1])  # const, phi


def forecast_deposit_rate(horizon_end: Optional[date] = None):
    """Ставка годового вклада = средняя ОЖИДАЕМАЯ ключевая ставка на год вперёд
    плюс спред банка, а не текущая (спот) ключевая ставка плюс спред.
    Проверено на истории: спред к среднеожидаемой ставке вдвое стабильнее
    (std ~0.09 п.п.), чем спред к спот-ставке (std ~0.17 п.п.) — ставка по
    вкладам явно закладывает ожидания по будущим решениям ЦБ, а не только
    текущий уровень.

    Сам спред моделируется как возврат к среднему AR(1) — сегодняшний (спот)
    спред затухает к своему долгосрочному среднему по мере роста горизонта, а
    не фиксируется навсегда на недавнем среднем. Проверено на бэктесте
    (2018-2026, прогноз на 3/6/12/24 мес вперёд по декадам, без заглядывания
    в будущее): AR(1)-проекция даёт RMSE на 11-32% ниже фиксированного
    недавнего среднего спреда на ВСЕХ протестированных горизонтах, и разрыв
    растёт с горизонтом — особенно важно для долгосрочного (до 2028 года)
    прогноза, где спред успевает почти полностью вернуться к среднему."""
    horizon_end = horizon_end or date(2028, 12, 31)
    key_lookup = key_rate_lookup()

    hist = load_deposit_history()
    hist = hist.assign(FwdKeyRate=hist["Date"].apply(lambda d: forward_average_key_rate(key_lookup, d)))
    hist = hist.assign(Spread=hist["Rate"] - hist["FwdKeyRate"])

    ar_const, ar_phi = _ar1_fit(hist["Spread"].values)
    long_run_spread = ar_const / (1 - ar_phi) if abs(ar_phi) < 1 else float(hist["Spread"].mean())
    current_spread = float(hist["Spread"].iloc[-1])

    last_date = hist["Date"].max()
    future_dates = []
    d = last_date
    while True:
        d = _next_decade(d)
        if d > horizon_end:
            break
        future_dates.append(d)

    future = pd.DataFrame({"Date": future_dates})
    future["FwdKeyRate"] = future["Date"].apply(lambda d: forward_average_key_rate(key_lookup, d))
    steps = np.arange(1, len(future_dates) + 1)
    projected_spread = long_run_spread + (ar_phi ** steps) * (current_spread - long_run_spread)
    future["Rate"] = future["FwdKeyRate"].values + projected_spread

    # Доверительный интервал 20-80%: та же полоса сценариев ключевой ставки
    # (оптимист/пессимист, build_key_rate_band), что и у тайла "Фонд денежного
    # рынка" в сводной таблице — банковский спред считается одинаковым для
    # всех сценариев (это про поведение банка, а не про траекторию ставки),
    # разница целиком идёт от неопределённости самого прогноза ставки.
    band = build_key_rate_band()
    lo_hi = future["Date"].apply(lambda d: forward_average_key_rate_band(band, d))
    future["RateLo"] = [lh[0] for lh in lo_hi] + projected_spread
    future["RateHi"] = [lh[1] for lh in lo_hi] + projected_spread

    spread_info = {"current": round(current_spread, 3), "long_run": round(long_run_spread, 3), "phi": round(ar_phi, 4)}
    return hist[["Date", "Rate"]], future[["Date", "Rate", "RateLo", "RateHi"]], spread_info


# ---------------- Кривая доходности ОФЗ ----------------
TENORS = [1, 2, 3, 4, 5, 7, 10]


def load_ofz_curve() -> pd.DataFrame:
    df = _cached_read_excel(YIELD_FILE, sheet_name="Доходности КБД", header=0)
    rename = {"tradedate": "Date"}
    for t in TENORS:
        rename[f"period_{t}.0"] = f"y{t}"
    df = df.rename(columns=rename)
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    df = df.sort_values("Date").reset_index(drop=True)
    cols = ["Date"] + [f"y{t}" for t in TENORS]
    return df[cols]


def _ofz_irs_spread_by_era(tenors: Tuple[int, ...]) -> Dict[int, Dict[str, float]]:
    return _memoize("_ofz_irs_spread_by_era", (tenors,), lambda: _ofz_irs_spread_by_era_impl(tenors))


def _ofz_irs_spread_by_era_impl(tenors: Tuple[int, ...]) -> Dict[int, Dict[str, float]]:
    """Средний наблюдаемый спред (доходность ОФЗ минус котировка IRS того же
    срока), отдельно за эру MOSPRIME (до 24.10.2022) и эру KEYRATE (после) —
    считается на пересечении дат, где есть и ОФЗ, и своп. Это НЕ модель
    (пробовали полную ACM-декомпозицию — она дала перевёрнутую кривую и
    неправдоподобный рост "чистых" ожиданий на длинном конце, см. историю
    правок), а прямая, проверяемая рыночная величина: разница между
    гособлигацией и свопом на тот же срок. Спред отличается по эрам не
    случайно — до 2022 г. плавающая нога свопа была привязана к MOSPRIME
    (межбанковская ставка с кредитной премией банков), после — напрямую к
    ключевой ставке, поэтому уровень и даже знак спреда на длинных сроках
    меняется между эрами (в среднем -0.27 п.п. на 10 лет при MOSPRIME против
    +0.53 п.п. при KEYRATE)."""
    curve = load_ofz_curve()
    irs = load_irs_curve()
    ofz_m = curve.set_index(pd.to_datetime(curve["Date"]))[[f"y{t}" for t in tenors]].resample("ME").last()
    ofz_m.index = ofz_m.index.date
    irs_cols = [c for t in tenors for c in (f"{t}Y", f"{t}Y_MOSPRIME") if c in irs.columns]
    irs_m = irs.set_index(pd.to_datetime(irs["Date"]))[irs_cols].resample("ME").last()
    irs_m.index = irs_m.index.date

    result: Dict[int, Dict[str, float]] = {}
    for t in tenors:
        col_kr, col_mp = f"{t}Y", f"{t}Y_MOSPRIME"
        spreads = {"mosprime": [], "keyrate": []}
        for d in ofz_m.index:
            if d not in irs_m.index:
                continue
            ofz_val = ofz_m.loc[d, f"y{t}"]
            era = "keyrate" if d >= KEYRATE_SWAP_START else "mosprime"
            irs_col = col_kr if era == "keyrate" else col_mp
            if irs_col not in irs_m.columns:
                continue  # для этого срока нет котировки в данную эру (напр. 4Y/7Y у MOSPRIME)
            irs_val = irs_m.loc[d, irs_col]
            if pd.isna(ofz_val) or pd.isna(irs_val):
                continue
            spreads[era].append(float(ofz_val) - float(irs_val))
        result[t] = {era: (float(np.mean(vals)) if vals else 0.0) for era, vals in spreads.items()}
    return result


def key_rate_expectation_vs_fact(tenors: Tuple[int, ...] = (1, 2, 5, 10)) -> Dict[int, Dict[str, list]]:
    """Для каждого срока N сравниваем:
    - "ожидание" на дату T = доходность ОФЗ срока N на эту дату МИНУС
      средний наблюдаемый спред ОФЗ к свопу (IRS) за соответствующую эру
      (см. _ofz_irs_spread_by_era) — это очищенная от премии оценка, а не
      сырая доходность ОФЗ (полную модельную декомпозицию, ACM, пробовали —
      не сработала на нашей короткой нестационарной истории);
    - "факт" на дату T = реализованная средняя ключевая ставка за период
      [T, T+N лет] — доступна только там, где этот период уже полностью
      прошёл к сегодняшнему дню (иначе часть среднего пришлось бы брать из
      прогноза, а не из факта).
    Месячная выборка с начала 2016 года (первая дата в КБД МосБиржи)."""
    key_lookup = key_rate_lookup()
    curve = load_ofz_curve()
    monthly = (
        curve.set_index(pd.to_datetime(curve["Date"]))[[f"y{t}" for t in tenors]]
        .resample("ME").last().dropna()
    )
    monthly.index = monthly.index.date
    today = date.today()
    spread_by_era = _ofz_irs_spread_by_era(tenors)

    result: Dict[int, Dict[str, list]] = {}
    for t in tenors:
        dates, expected, fact = [], [], []
        for d in monthly.index:
            dates.append(d.isoformat())
            era = "keyrate" if d >= KEYRATE_SWAP_START else "mosprime"
            clean = float(monthly.loc[d, f"y{t}"]) - spread_by_era[t][era]
            expected.append(round(clean, 3))
            end = d + relativedelta(years=t)
            if end <= today:
                fact.append(round(forward_average_key_rate(key_lookup, d, days=t * 365), 3))
            else:
                fact.append(None)
        result[t] = {"dates": dates, "expected": expected, "fact": fact}
    return result


def _irs_curve_row_today(window: int = 5) -> Tuple[date, Dict[float, float]]:
    return _memoize("_irs_curve_row_today", (window,), lambda: _irs_curve_row_today_impl(window))


def _irs_curve_row_today_impl(window: int = 5) -> Tuple[date, Dict[float, float]]:
    """Средняя СЫРАЯ пар-котировка IRS (эра KEYRATE, срочная сетка 3M-10Y) за
    последние `window` торговых дней, а не единственная последняя точка —
    однодневный снимок тонкого, часто малоликвидного на длинных сроках рынка
    свопов слишком легко "запечатывает" в прогноз случайный шум одного дня,
    особенно с учётом того, что этот разрыв фигурирует в модели с большим
    весом ещё много месяцев вперёд (см. model1_ofz_forecast).

    Используется ТОЛЬКО для премии ОФЗ к IRS (спред между двумя рыночными
    котировками в их естественном виде — как в реальном arbitrage/ASW) — не
    для сравнения с нашим прогнозом средней ставки, для этого есть очищенная
    от капитализации версия, см. _irs_avg_curve_row_today.

    Почему 5 дней, а не произвольное число: дневное std изменения котировки
    IRS ≈0.09-0.16 п.п. по срокам. Переход от 1 дня к 5 сокращает
    стандартную ошибку среднего примерно вдвое (с ≈0.14-0.16 до ≈0.06-0.07
    п.п.) — основной выигрыш от сглаживания. Дальнейшее увеличение окна (10,
    20 дней) даёт всё меньше дополнительного снижения шума, но добавляет
    задержку в отражении текущего рынка — а рынок здесь не стоит на месте:
    разрыв IRS с нашим прогнозом за 4 года эры KEYRATE устойчиво и
    монотонно менялся на 0.1-0.2 п.п. в месяц (см.
    _irs_vs_our_forecast_gap_history) — то есть более длинное окно платило
    бы заметным устареванием анкера ради небольшого выигрыша в шуме. 5 дней
    — компромисс, а не случайное число, но и не результат прямого бэктеста
    (архива прошлых версий модели нет)."""
    df = load_irs_curve()
    cols_years: Dict[float, str] = {0.25: "3M", 0.5: "6M", 0.75: "9M"}
    cols_years.update({float(t): f"{t}Y" for t in range(1, 11)})
    valid_cols = [c for c in cols_years.values() if c in df.columns]
    recent = df.dropna(subset=valid_cols, how="all").tail(window)
    rates: Dict[float, float] = {}
    for yrs, col in cols_years.items():
        if col in recent.columns:
            vals = recent[col].dropna()
            if len(vals):
                rates[yrs] = float(vals.mean())
    return recent["Date"].iloc[-1], rates


def _irs_avg_curve_row_today(window: int = 5) -> Tuple[date, Dict[float, float]]:
    return _memoize("_irs_avg_curve_row_today", (window,), lambda: _irs_avg_curve_row_today_impl(window))


def _irs_avg_curve_row_today_impl(window: int = 5) -> Tuple[date, Dict[float, float]]:
    """То же самое, что _irs_curve_row_today, но по очищенной от
    капитализации "Avg next NY" (см. IRS_AVG_TENORS, только 1/2/3/5/10 лет —
    более редкая сетка, чем у сырой кривой). Это то, что нужно сравнивать с
    нашим прогнозом средней ставки (forward_average_key_rate) — методологически
    то же самое (простое среднее), а не сырая пар-ставка свопа, искажённая
    эффектом сложного процента. Используется в market_gap_today модели 1."""
    df = load_irs_curve()
    cols_years = {float(t): f"Avg next {t}Y" for t in IRS_AVG_TENORS}
    valid_cols = [c for c in cols_years.values() if c in df.columns]
    recent = df.dropna(subset=valid_cols, how="all").tail(window)
    rates: Dict[float, float] = {}
    for yrs, col in cols_years.items():
        if col in recent.columns:
            vals = recent[col].dropna()
            if len(vals):
                rates[yrs] = float(vals.mean())
    return recent["Date"].iloc[-1], rates


MODEL1_GAP_TARGET_WINDOW_MONTHS = 18


def _irs_vs_our_forecast_gap_history(tenors: Tuple[int, ...]) -> Dict[int, float]:
    return _memoize("_irs_vs_our_forecast_gap_history", (tenors,), lambda: _irs_vs_our_forecast_gap_history_impl(tenors))


def _irs_vs_our_forecast_gap_history_impl(tenors: Tuple[int, ...]) -> Dict[int, float]:
    """Средний разрыв между рыночной ставкой IRS и НАШИМ прогнозом средней
    ключевой ставки на тот же срок, помесячно, за последние
    MODEL1_GAP_TARGET_WINDOW_MONTHS (18) месяцев — используется как цель
    схождения сегодняшнего разрыва (см. model1_ofz_forecast).

    "Рыночная ставка" здесь — очищенная от капитализации "Avg next NY" (см.
    IRS_AVG_TENORS), а не сырая пар-ставка свопа: наш прогноз (our) — это
    простое среднее по forward_average_key_rate, и сравнивать его нужно с
    методологически тем же самым — средним, а не с пар-ставкой, которая
    искажена эффектом сложного процента свопа. Для сроков вне сетки
    IRS_AVG_TENORS (4, 7 лет) значение на каждую дату интерполируется по
    доступным на эту дату точкам.

    Раньше бралось среднее по ВСЕЙ эре KEYRATE (~4 года). Проверка по
    полугодиям показала, что это не стационарная, колеблющаяся вокруг
    среднего величина, а устойчивый МОНОТОННЫЙ ТРЕНД без единого разворота
    на всех сроках — усреднение по всей эре давало устаревшую цель,
    противоречащую 4 годам наблюдаемого движения рынка. Короткие окна (12,
    18, 24 мес) намного стабильнее друг друга и внутренне, чем полная
    4-летняя выборка — это говорит, что последние 1.5-2 года ближе к
    текущему "нормальному" уровню, чем усреднение с начала эры. 18 месяцев
    — компромисс между устойчивостью оценки и её актуальностью.

    Прежние формальные тесты (ADF, KPSS) были посчитаны на разрыве к сырой
    пар-ставке свопа — они не пересчитаны заново на очищенной серии после
    перехода на "Avg next NY" (переход сделан по прямой просьбе — сырая
    пар-ставка и наше простое среднее методологически несопоставимы, это
    важнее, чем эффект на стационарность), так что численные p-значения
    ниже могли не сохраниться в точности, но качественный вывод (короткое
    окно стабильнее полной эры) их не касается."""
    irs = load_irs_curve()
    key_lookup = key_rate_lookup()
    avg_cols = [f"Avg next {t}Y" for t in IRS_AVG_TENORS if f"Avg next {t}Y" in irs.columns]
    irs_m = irs.set_index(pd.to_datetime(irs["Date"]))[avg_cols].resample("ME").last()
    irs_m.index = irs_m.index.date
    irs_m = irs_m[irs_m.index >= KEYRATE_SWAP_START]

    result: Dict[int, float] = {}
    for t in tenors:
        gaps = []
        for d, row in irs_m.iterrows():
            points = {
                float(yr): float(row[f"Avg next {yr}Y"])
                for yr in IRS_AVG_TENORS
                if f"Avg next {yr}Y" in irs_m.columns and pd.notna(row[f"Avg next {yr}Y"])
            }
            if not points:
                continue
            irs_val = _linear_interp_or_none(points)(float(t))
            if irs_val is None:
                continue
            our = forward_average_key_rate(key_lookup, d, days=t * 365)
            gaps.append(float(irs_val) - our)
        window = gaps[-MODEL1_GAP_TARGET_WINDOW_MONTHS:] if gaps else gaps
        result[t] = float(np.mean(window)) if window else 0.0
    return result


def _ofz_irs_premium_recent(tenors: Tuple[int, ...]) -> Dict[int, float]:
    return _memoize("_ofz_irs_premium_recent", (tenors,), lambda: _ofz_irs_premium_recent_impl(tenors))


def _ofz_irs_premium_recent_impl(tenors: Tuple[int, ...]) -> Dict[int, float]:
    """Средняя премия ОФЗ к IRS (эра KEYRATE) за последние
    MODEL1_GAP_TARGET_WINDOW_MONTHS месяцев — используется как цель
    схождения В МОДЕЛИ 1 вместо среднего по всей эре (_ofz_irs_spread_by_era,
    который по-прежнему используется для ИСТОРИЧЕСКИХ сравнений "ожидание
    vs факт" — там нужен один консистентный спред на весь период сравнения,
    а не гуляющее по времени окно).

    ADF/KPSS-тесты показали неоднозначную, но местами тревожную картину:
    на 1 годе премия похожа на стационарную (ADF p=0.048, KPSS p=0.1 —
    оба теста не против гипотезы стационарности), но на 5 и 10 годах
    видна та же болезнь, что и у разрыва IRS-наш прогноз (ADF не отвергает
    unit root, KPSS отвергает стационарность) — то есть усреднение по всей
    4-летней эре может давать частично устаревшую цель и здесь. Разница
    между полной эрой и последними 18 месяцами умереннее, чем для разрыва
    IRS (0.3-0.7 п.п. против 1.3-3.9 п.п.), но использовать recent-окно
    последовательно логичнее, чем чинить только там, где эффект бросается
    в глаза сильнее. Мощность обоих тестов низкая (n≈46-48 — история
    самого свопа на ключевую ставку короткая), выводы не окончательные."""
    curve = load_ofz_curve()
    irs = load_irs_curve()
    ofz_m = curve.set_index(pd.to_datetime(curve["Date"]))[[f"y{t}" for t in tenors]].resample("ME").last()
    ofz_m.index = ofz_m.index.date
    irs_cols = [f"{t}Y" for t in tenors if f"{t}Y" in irs.columns]
    irs_m = irs.set_index(pd.to_datetime(irs["Date"]))[irs_cols].resample("ME").last()
    irs_m.index = irs_m.index.date
    irs_m = irs_m[irs_m.index >= KEYRATE_SWAP_START]

    result: Dict[int, float] = {}
    for t in tenors:
        col = f"{t}Y"
        if col not in irs_m.columns:
            result[t] = 0.0
            continue
        vals = []
        for d in irs_m.index:
            if d not in ofz_m.index:
                continue
            ofz_val, irs_val = ofz_m.loc[d, f"y{t}"], irs_m.loc[d, col]
            if pd.isna(ofz_val) or pd.isna(irs_val):
                continue
            vals.append(float(ofz_val) - float(irs_val))
        window = vals[-MODEL1_GAP_TARGET_WINDOW_MONTHS:] if vals else vals
        result[t] = float(np.mean(window)) if window else 0.0
    return result


def _linear_interp_or_none(points: Dict[float, float]):
    """Линейная интерполяция по известным точкам; за пределами сетки —
    None (а не экстраполяция), т.к. форвардную ставку за пределами
    наблюдаемой кривой считать не из чего."""
    xs = sorted(points.keys())
    ys = [points[x] for x in xs]

    def f(x: float) -> Optional[float]:
        if x < xs[0] or x > xs[-1]:
            return None
        for i in range(len(xs) - 1):
            if xs[i] <= x <= xs[i + 1]:
                w = (x - xs[i]) / (xs[i + 1] - xs[i])
                return ys[i] + w * (ys[i + 1] - ys[i])
        return ys[-1]

    return f


MODEL1_HORIZONS = [(0.0, "Сейчас"), (0.25, "+3 мес"), (0.5, "+6 мес"), (1.0, "+12 мес")]
MODEL1_HALF_LIFE_MONTHS = 24.0


def model1_ofz_forecast(tenors: Tuple[int, ...] = tuple(TENORS), live_date=None,
                         live_curve: Optional[Dict[int, float]] = None, key_lookup=None) -> Dict[str, Dict[int, float]]:
    """Модель №1 прогноза доходности ОФЗ. Первая версия строила "чистое"
    ожидание из форвардной ставки, подразумеваемой кривой IRS — но это
    означало полностью довериться рынку и отказаться от нашего прогноза
    там, где рынок его "видит" (до 10 лет вперёд). НАШ прогноз ключевой
    ставки считается базовой траекторией — без своего прогноза модели нет,
    -- а сегодняшнее расхождение рынка (IRS, усреднённый за 5 торговых дней,
    а не разовый снимок) с нашим прогнозом — это поправка НА рынок, а не
    повод отказаться от своей траектории.

    Раньше эта поправка линейно гасла до НУЛЯ к произвольной дате
    (01.2029) — то есть предполагалось, что рынок в итоге полностью
    "исчезнет" и наш взгляд станет единственно верным. Это внутренне
    противоречиво: мы обязаны опираться на свой прогноз, но корректировка
    на ожидания рынка не должна пропадать насовсем. Теперь сегодняшний
    разрыв экспоненциально (полураспад 24 мес — тот же темп, что и у
    премии ниже, для единообразия) сходится не к нулю, а к СРЕДНЕМУ
    ИСТОРИЧЕСКОМУ разрыву рынка с нашим прогнозом за эру KEYRATE (см.
    _irs_vs_our_forecast_gap_history) — то есть к устойчивой, откалиброванной
    по данным поправке на рынок, а не к её полному исчезновению.

    Поверх этого — специфичная для ОФЗ премия (спред ОФЗ к IRS), которая
    стартует от сегодняшнего спот-значения и плавно (полураспад 24 мес)
    сходится к среднему за последние 18 месяцев (см. _ofz_irs_premium_recent)
    — не по всей эре KEYRATE: ADF/KPSS-тесты показывают, что на 5 и 10 годах
    премия ведёт себя так же нестационарно, как и разрыв IRS-наш прогноз
    выше, хотя и не так выраженно. Премия не связана с "правотой" рынка или
    нашей моделью, это отдельная, специфичная для облигации надбавка
    (ликвидность/дюрация)."""
    curve = load_ofz_curve()
    key_lookup = key_lookup or key_rate_lookup()
    # Две разные "версии" IRS для двух разных целей: очищенная от капитализации
    # "Avg next NY" — единственно корректная база для сравнения с нашим
    # прогнозом СРЕДНЕЙ ставки (market_gap_today); сырая пар-котировка — для
    # премии ОФЗ к IRS, где сравниваются два рыночных инструмента в их
    # естественном виде (как в реальном ASW), а не что-то, что можно "очистить".
    _irs_avg_date, irs_rates_today_avg = _irs_avg_curve_row_today()
    _irs_raw_date, irs_rates_today_raw = _irs_curve_row_today()
    rate_fn_avg = _linear_interp_or_none(irs_rates_today_avg)
    rate_fn_raw = _linear_interp_or_none(irs_rates_today_raw)

    last_row = pd.Series({f"y{t}": live_curve[t] for t in TENORS}) if live_curve else curve.iloc[-1]
    last_date = live_date or curve["Date"].max()
    premium_target = _ofz_irs_premium_recent(tenors)
    long_run_gap = _irs_vs_our_forecast_gap_history(tenors)

    market_gap_today: Dict[int, float] = {}
    for t in tenors:
        irs_t = rate_fn_avg(float(t))
        our_t = forward_average_key_rate(key_lookup, last_date, days=t * 365)
        market_gap_today[t] = (irs_t - our_t) if irs_t is not None else long_run_gap[t]

    def expectation(t: int, months: int, future_date: date) -> float:
        our = forward_average_key_rate(key_lookup, future_date, days=t * 365)
        w = 0.5 ** (months / MODEL1_HALF_LIFE_MONTHS)
        gap = long_run_gap[t] + w * (market_gap_today[t] - long_run_gap[t])
        return our + gap

    def premium(t: int, months: int) -> float:
        irs_t = rate_fn_raw(float(t))
        spot = float(last_row[f"y{t}"]) - (irs_t if irs_t is not None else float(last_row[f"y{t}"]))
        target = premium_target[t]
        w = 1 - 0.5 ** (months / MODEL1_HALF_LIFE_MONTHS)
        return spot + w * (target - spot)

    snapshots: Dict[str, Dict[int, float]] = {}
    for horizon_years, label in MODEL1_HORIZONS:
        months = int(round(horizon_years * 12))
        future_date = last_date + relativedelta(months=months)
        snapshots[label] = {
            t: round(expectation(t, months, future_date) + premium(t, months), 3)
            for t in tenors
        }
    return snapshots


def _accuracy_stats(expected: list, fact: list, naive: list) -> Dict[str, Optional[float]]:
    """RMSE/MAE/смещение модели, плюс два стандартных для такого рода
    моделей ориентира:
    - Theil's U = RMSE модели / RMSE наивного прогноза (naive = "ставка не
      изменится", спот ключевой ставки на дату прогноза). U < 1 означает,
      что модель обыгрывает наивный прогноз; U >= 1 — что не обыгрывает, и
      весь механизм "очистки от премии" не добавляет ценности сверх
      наивного взгляда;
    - hit rate — доля наблюдений, где модель верно предсказала НАПРАВЛЕНИЕ
      (вырастет/упадёт факт относительно сегодняшнего спота), а не только
      была близка по уровню."""
    triples = [(e, f, n) for e, f, n in zip(expected, fact, naive) if e is not None and f is not None and n is not None]
    if not triples:
        return {"rmse": None, "mae": None, "bias": None, "n": 0, "theil_u": None, "hit_rate": None}
    errs = np.array([e - f for e, f, n in triples])
    naive_errs = np.array([n - f for e, f, n in triples])
    rmse = float(np.sqrt(np.mean(errs ** 2)))
    rmse_naive = float(np.sqrt(np.mean(naive_errs ** 2)))
    hits = [np.sign(e - n) == np.sign(f - n) for e, f, n in triples if e != n]
    return {
        "rmse": round(rmse, 3),
        "mae": round(float(np.mean(np.abs(errs))), 3),
        "bias": round(float(np.mean(errs)), 3),
        "n": len(triples),
        "theil_u": round(rmse / rmse_naive, 3) if rmse_naive > 0 else None,
        "hit_rate": round(100.0 * float(sum(hits)) / len(hits), 1) if hits else None,
    }


def model1_quality(tenors: Tuple[int, ...] = (1, 2, 5, 10)) -> Dict[str, Dict[int, Dict[str, Optional[float]]]]:
    """Историческая точность строительных блоков модели №1 — "ожидание"
    (очищенное от премии) против реализованного факта, отдельно для ОФЗ-
    основанного и IRS-основанного (только эра ключевой ставки, с 2022 г. —
    более короткая, но методологически однородная выборка) ожидания.

    Это НЕ бэктест модели №1 целиком: у нас нет архива прошлых версий
    нашего прогноза ключевой ставки, а сам механизм "рыночный спред линейно
    гаснет к январю 2029" — разовое допущение под текущую ситуацию, а не
    устойчивая закономерность, которую можно перепроверить на истории.
    Здесь измеряется точность именно оценки "чистого" ожидания ставки —
    ключевого входа модели."""
    ofz_data = key_rate_expectation_vs_fact(tenors)
    irs_data = irs_expectation_vs_fact(tenors)
    key_lookup = key_rate_lookup()

    def naive_series(dates: List[str]) -> List[float]:
        return [key_lookup(date.fromisoformat(d)) for d in dates]

    result: Dict[str, Dict[int, Dict[str, Optional[float]]]] = {"ofz": {}, "irs_keyrate": {}}
    for t in tenors:
        result["ofz"][t] = _accuracy_stats(
            ofz_data[t]["expected"], ofz_data[t]["fact"], naive_series(ofz_data[t]["dates"])
        )
        result["irs_keyrate"][t] = _accuracy_stats(
            irs_data[t]["expected_keyrate"], irs_data[t]["fact"], naive_series(irs_data[t]["dates"])
        )
    return result


# ---------------- Модель №2: декомпозиция по Фишеру (реальная ставка + инфляция) ----------------
MODEL2_HALF_LIFE_MONTHS = 24.0
MODEL2_CPI_TARGET = 4.0  # цель ЦБ по инфляции — действует с 2030 года и далее
MODEL2_CPI_GLIDE_PATH = {2028: 4.8, 2029: 4.2}  # среднегодовая инфляция на пути к цели после конца явного прогноза (до 2027 включительно)
MODEL2_MIN_WINDOW = 24
MODEL2_HORIZONS = [(3, "+3 мес"), (6, "+6 мес"), (12, "+12 мес")]
# На сроках 1-2 года подразумеваемая реальная ставка сейчас (после 2022 г.) заметно
# выше, чем "норма" за всю историю 2010-2026 (та смешивает совсем другой,
# дорежимный период низких/отрицательных реальных ставок 2010-2021) — поэтому для
# них норма считается ТОЛЬКО по текущему режиму (с даты смены бенчмарка свопов
# KEYRATE_SWAP_START). На сроках 5 и 10 лет так сделать честно нельзя: чтобы
# получить хоть одно реализованное (без заглядывания вперёд) значение реальной
# ставки на 5/10 лет вперёд, нужно 5/10 лет уже прошедшей истории — с 24.10.2022
# прошло всего ~4 года, реализованных наблюдений текущего режима на этих сроках
# попросту ещё не существует. Поэтому для 5 и 10 лет используется полная история
# 2010-2026 — что структурно занижает "норму" и может делать прогноз сроков 5/10
# лет слишком оптимистичным (см. историю правок), пока новый режим не наберёт
# достаточно истории.
MODEL2_REGIME_TENORS = (1, 2)
MODEL2_REGIME_MIN_WINDOW = 12


def _load_cpi_monthly() -> pd.DataFrame:
    """Месячная инфляция г/г (факт + наш прогноз до конца явного горизонта
    прогноза) из основного файла сайта."""
    df = _cached_read_excel(MAIN_DATA_FILE, sheet_name="cpi")
    df = df.rename(columns={"date": "Date", "% г/г": "yoy"})[["Date", "yoy"]].dropna()
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    return df.sort_values("Date").reset_index(drop=True)


def _cpi_extended_series() -> Tuple[List[date], List[float]]:
    """Ряд факт+прогноз инфляции, продлённый после конца явного помесячного
    прогноза (до 2027 включительно) плавным выходом на цель ЦБ: 2028 —
    4.8%, 2029 — 4.2%, с 2030 года — цель 4% и далее. Нужно, чтобы можно
    было усреднять ожидаемую инфляцию на срок до 10 лет вперёд от ЛЮБОЙ
    будущей даты, даже когда явный помесячный прогноз в файле кончается
    раньше."""
    cpi = _load_cpi_monthly()
    dates = cpi["Date"].tolist()
    vals = cpi["yoy"].tolist()
    tail_end = dates[-1] + relativedelta(years=11)
    d = dates[-1]
    while d < tail_end:
        d = d + relativedelta(months=1)
        dates.append(d)
        vals.append(MODEL2_CPI_GLIDE_PATH.get(d.year, MODEL2_CPI_TARGET))
    return dates, vals


def _avg_inflation_window(dates: List[date], vals: List[float], start: date, years: float,
                           max_date: Optional[date] = None) -> Optional[float]:
    """Средняя г/г инфляция за окно [start, start+years]. Если max_date задан
    и окно выходит за его пределы — вернёт None (используется, чтобы считать
    историческую норму реальной ставки ТОЛЬКО по уже реализованной на тот
    момент инфляции, без заглядывания в будущее)."""
    end = start + relativedelta(months=int(round(years * 12)))
    if max_date is not None and end > max_date:
        return None
    window = [v for d, v in zip(dates, vals) if start <= d <= end]
    return float(np.mean(window)) if window else None


def _model2_real_rate_monthly(tenors: Tuple[int, ...]) -> Tuple[pd.DataFrame, Dict[str, list]]:
    """Помесячная кривая ОФЗ + реализованная (без заглядывания вперёд)
    реальная ставка по каждому сроку — общий строительный блок для живого
    прогноза (среднее целиком) и для бэктеста (расширяющееся окно)."""
    cpi = _load_cpi_monthly()
    actual = cpi[cpi["Date"] <= date.today()]
    dates_r = actual["Date"].tolist()
    vals_r = actual["yoy"].tolist()
    last_actual_month = dates_r[-1]

    curve = load_ofz_curve().copy()
    curve["Date"] = pd.to_datetime(curve["Date"])
    monthly = curve.set_index("Date").resample("ME").last().reset_index()
    monthly["Date"] = monthly["Date"].dt.date
    monthly = monthly.dropna(subset=[f"y{t}" for t in tenors], how="all").reset_index(drop=True)

    real_rate: Dict[str, list] = {}
    for t in tenors:
        series = []
        for i in range(len(monthly)):
            d = monthly.iloc[i]["Date"]
            y = monthly.iloc[i][f"y{t}"]
            infl = _avg_inflation_window(dates_r, vals_r, d, t, max_date=last_actual_month)
            series.append(float(y) - infl if (infl is not None and pd.notna(y)) else None)
        real_rate[str(t)] = series
    return monthly, real_rate


def _model2_long_run_window(t: int, i: int, m_dates: List[date], series: list) -> Optional[list]:
    """Окно значений реальной ставки, по которому считается "норма" на дату
    с индексом i (включительно). Для MODEL2_REGIME_TENORS — только текущий
    режим (с KEYRATE_SWAP_START), и только если данных по режиму уже
    накопилось MODEL2_REGIME_MIN_WINDOW; иначе (в т.ч. для остальных сроков)
    — вся доступная история, если её хотя бы MODEL2_MIN_WINDOW."""
    if t in MODEL2_REGIME_TENORS:
        regime = [v for d, v in zip(m_dates[: i + 1], series[: i + 1]) if v is not None and d >= KEYRATE_SWAP_START]
        if len(regime) >= MODEL2_REGIME_MIN_WINDOW:
            return regime
    full = [v for v in series[: i + 1] if v is not None]
    return full if len(full) >= MODEL2_MIN_WINDOW else None


def _model2_regime_shift(i: int, m_dates: List[date], real_rate_hist: Dict[str, list]) -> float:
    """Средний сдвиг реальной ставки между дорежимной нормой (вся доступная
    история) и нормой текущего режима (с KEYRATE_SWAP_START) — измеряется на
    сроках 1-2 года (MODEL2_REGIME_TENORS), где реализованных наблюдений
    текущего режима уже достаточно. Раньше для сроков 5-10 лет "норма"
    молча бралась из смешанной дорежимной истории, хотя мы точно знаем (по
    коротким срокам и по рынку), что реальная ставка структурно выросла —
    игнорировать этот сдвиг только потому, что для длинных сроков нет
    ПРЯМЫХ реализованных наблюдений нового режима, значит выбрасывать
    единственную имеющуюся информацию о том, куда сдвинулся режим."""
    shifts = []
    for rt in MODEL2_REGIME_TENORS:
        rseries = real_rate_hist.get(str(rt))
        if rseries is None:
            continue
        regime = [v for d, v in zip(m_dates[: i + 1], rseries[: i + 1]) if v is not None and d >= KEYRATE_SWAP_START]
        full = [v for v in rseries[: i + 1] if v is not None]
        if len(regime) >= MODEL2_REGIME_MIN_WINDOW and full:
            shifts.append(float(np.mean(regime)) - float(np.mean(full)))
    return float(np.mean(shifts)) if shifts else 0.0


def _model2_long_run_target(t: int, i: int, m_dates: List[date], real_rate_hist: Dict[str, list]) -> Optional[float]:
    """"Норма", к которой сходится реальная ставка срока t на дату с индексом
    i. Для MODEL2_REGIME_TENORS — прямое среднее по текущему режиму. Для
    остальных сроков (5-10 лет, где для честного текущережимного среднего
    не хватает прошедшего времени) — среднее по всей истории, СКОРРЕКТИРОВАННОЕ
    на измеренный по коротким срокам сдвиг режима (см. _model2_regime_shift) —
    рыночная/наблюдаемая поправка вместо слепого доверия смешанной истории."""
    series = real_rate_hist[str(t)]
    window = _model2_long_run_window(t, i, m_dates, series)
    if window is None:
        return None
    base = float(np.mean(window))
    if t in MODEL2_REGIME_TENORS:
        return base
    return base + _model2_regime_shift(i, m_dates, real_rate_hist)


def model2_inflation_forecast(tenors: Tuple[int, ...] = tuple(TENORS), live_date=None,
                               live_curve: Optional[Dict[int, float]] = None) -> Dict[str, Dict[int, float]]:
    """Модель №2 прогноза доходности ОФЗ — декомпозиция по Фишеру: доходность
    = реальная ставка + ожидаемая инфляция на срок бумаги.

    Пробовали держать реальную ставку неизменной (спот) — на бэктесте хуже
    наивного прогноза почти везде: реальная ставка сама заметно колеблется,
    а не только инфляционная часть. Пробовали AR(1)-регрессию на реальную
    ставку — там, где phi близко к 1 (а это почти все сроки), оценка
    долгосрочного среднего (const/(1-phi)) становится крайне неустойчивой,
    и модель на бэктесте иногда взрывалась (RMSE > 10 п.п. на 5-летнем
    сроке). Итоговая версия: реальная ставка плавно (полураспад 24 мес —
    тот же горизонт, что и у премии в Model 1) сходится к простому
    историческому СРЕДНЕМУ реальной ставки.

    ВАЖНО, ЧЕСТНО О ПРИРОДЕ ЭТОГО МЕХАНИЗМА: формальные тесты (ADF, KPSS,
    statsmodels) на реальной ставке НЕ подтверждают, что это стационарная,
    mean-reverting величина — ADF нигде не отвергает единичный корень
    (p=0.11-0.83 по срокам на полной истории, p=0.59-1.00 на
    regime-restricted выборке 1-2 лет), а KPSS в большинстве случаев
    отвергает стационарность (p=0.01-0.07). На сроках 1-2 года реальная
    ставка не просто "смещена" — она продолжает расти даже внутри
    последних 6-24 месяцев текущего режима, без признаков стабилизации.
    Пробовали заменить плоское среднее на EWMA (полупериоды 3/6/9/12 мес,
    больше веса недавним значениям) — на бэктесте ХУЖЕ плоского среднего на
    всех 4 сроках, и чем короче полупериод (то есть чем сильнее модель
    следует за трендом), тем хуже. Вывод: возврат к среднему здесь работает
    не потому, что это подтверждённая экономическая закономерность (тесты
    её отвергают), а как чисто статистическая техника СЖАТИЯ (shrinkage) —
    она снижает дисперсию прогноза сильнее, чем добавляет систематическое
    смещение, и это эмпирический, проверенный бэктестом эффект, а не
    заявление о том, что реальная ставка "должна" вернуться к своей норме.
    Ожидаемая инфляция на срок бумаги берётся из факта и нашего прогноза,
    а после конца явного прогноза считается плавный выход на цель ЦБ:
    2028 — 4.8%, 2029 — 4.2%, с 2030 года — цель 4% и далее.

    "Норма", к которой сходится реальная ставка, для сроков 1-2 года
    считается только по текущему режиму (с 24.10.2022) — за всю историю
    2010-2026 она была бы искусственно занижена дорежимным периодом низких
    реальных ставок 2010-2021. Для сроков 5 и 10 лет реализованных наблюдений
    текущего режима физически ещё не хватает (не прошло 5-10 лет с
    24.10.2022) — но вместо того, чтобы молча использовать смешанную
    дорежимную историю как есть, полная история корректируется на сдвиг
    режима, ИЗМЕРЕННЫЙ по срокам 1-2 года (см. _model2_regime_shift): раз мы
    точно знаем, что реальная ставка сейчас выше дорежимной нормы на коротких
    сроках, разумно перенести этот наблюдаемый сдвиг и на длинные, а не
    делать вид, что режим не менялся.

    ВАЖНАЯ ОГОВОРКА: эта поправка принципиально НЕ ПРОВЕРЯЕМА бэктестом для
    сроков 5 и 10 лет — реализованный (без забегания вперёд) ряд реальной
    ставки на этих сроках обрывается ещё ДО начала эры KEYRATE (последняя
    точка для 5 лет — август 2021, для 10 лет — август 2016, см.
    model2_quality) — то есть до того момента, когда сдвиг режима вообще
    можно измерить. Метрики качества в таблице для этих сроков идентичны
    версии без поправки: это разумная, откалиброванная по данным
    экстраполяция, а не подтверждённый на истории результат."""
    curve = load_ofz_curve()
    last_row = pd.Series({f"y{t}": live_curve[t] for t in TENORS}) if live_curve else curve.iloc[-1]
    last_date = live_date or curve["Date"].max()

    dates_ext, vals_ext = _cpi_extended_series()

    def infl(start: date, years: int) -> float:
        v = _avg_inflation_window(dates_ext, vals_ext, start, years)
        return v if v is not None else MODEL2_CPI_TARGET

    fetch_tenors = tuple(sorted(set(tenors) | set(MODEL2_REGIME_TENORS)))
    monthly, real_rate_hist = _model2_real_rate_monthly(fetch_tenors)
    hist_dates = monthly["Date"].tolist()
    long_run = {}
    for t in tenors:
        series = real_rate_hist[str(t)]
        target = _model2_long_run_target(t, len(series) - 1, hist_dates, real_rate_hist)
        long_run[t] = target if target is not None else float(np.mean([v for v in series if v is not None]))
    real_rate_today = {t: float(last_row[f"y{t}"]) - infl(last_date, t) for t in tenors}

    snapshots: Dict[str, Dict[int, float]] = {}
    for horizon_years, label in MODEL1_HORIZONS:
        months = int(round(horizon_years * 12))
        future_date = last_date + relativedelta(months=months)
        w = 0.5 ** (months / MODEL2_HALF_LIFE_MONTHS)
        snapshots[label] = {
            t: round(long_run[t] + w * (real_rate_today[t] - long_run[t]) + infl(future_date, t), 3)
            for t in tenors
        }
    return snapshots


def model2_quality(tenors: Tuple[int, ...] = (1, 2, 5, 10)) -> Dict[str, Dict[int, Dict[str, Optional[float]]]]:
    """Честный бэктест модели №2 целиком (в отличие от модели 1, где нет
    архива прошлых версий нашего прогноза ставки): на каждую историческую
    дату долгосрочное среднее реальной ставки считается расширяющимся окном
    ТОЛЬКО по уже известным на тот момент данным (без заглядывания вперёд),
    инфляция для прогноза и для факта — тоже только реализованная. Прогноз
    на 3/6/12 мес вперёд сравнивается с реализованной доходностью ОФЗ.

    N в результате — это ПЕРЕКРЫВАЮЩИЕСЯ месячные окна (прогноз на 3/6/12
    мес с каждого из ~200 месяцев истории), а не независимые наблюдения:
    при среднем горизонте перекрытия ~7 месяцев реальный объём независимой
    информации на порядок меньше отображаемого N. Theil's U и hit_rate
    поэтому стоит читать как общее направление, а не как статистически
    точную оценку."""
    cpi = _load_cpi_monthly()
    actual = cpi[cpi["Date"] <= date.today()]
    dates_r = actual["Date"].tolist()
    vals_r = actual["yoy"].tolist()
    last_actual_month = dates_r[-1]

    fetch_tenors = tuple(sorted(set(tenors) | set(MODEL2_REGIME_TENORS)))
    monthly, real_rate_hist = _model2_real_rate_monthly(fetch_tenors)
    m_dates = monthly["Date"].tolist()

    result: Dict[str, Dict[int, Dict[str, Optional[float]]]] = {"inflation": {}}
    for t in tenors:
        series = real_rate_hist[str(t)]
        expected, fact, naive = [], [], []
        for i, d0 in enumerate(m_dates):
            r0 = series[i]
            long_run = _model2_long_run_target(t, i, m_dates, real_rate_hist)
            if long_run is None or r0 is None:
                continue
            for h_months, _hlabel in MODEL2_HORIZONS:
                future_date = d0 + relativedelta(months=h_months)
                future_idx = next((j for j in range(i, len(m_dates)) if m_dates[j] >= future_date), None)
                if future_idx is None:
                    continue
                actual_future_date = m_dates[future_idx]
                y_fut = monthly.iloc[future_idx].get(f"y{t}")
                y_spot = monthly.iloc[i].get(f"y{t}")
                if pd.isna(y_fut) or pd.isna(y_spot):
                    continue
                infl_future = _avg_inflation_window(dates_r, vals_r, actual_future_date, t, max_date=last_actual_month)
                if infl_future is None:
                    continue
                w = 0.5 ** (h_months / MODEL2_HALF_LIFE_MONTHS)
                real_forecast = long_run + w * (r0 - long_run)
                expected.append(real_forecast + infl_future)
                fact.append(float(y_fut))
                naive.append(float(y_spot))
        result["inflation"][t] = _accuracy_stats(expected, fact, naive)
    return result


# ---------------- Модель №3: ожидания по ставке + условия кредитования ----------------
CC_FILE = os.getenv("CC_FILE", os.path.join(BASE_DIR, "Условия кредитования ЦБ.xlsx"))
MODEL3_HORIZONS = [(3, "+3 мес"), (6, "+6 мес"), (12, "+12 мес")]
MODEL3_SAMPLE_START = date(2014, 1, 1)  # раньше нет надёжной истории ключевой ставки (введена в сентябре 2013)


def _load_credit_conditions_monthly() -> pd.DataFrame:
    """Баланс ответов "Как изменились условия кредитования?" из мониторинга
    предприятий Банка России (cbr.ru/analytics/dkp/monitoring/,
    mp_survey_data.xlsx, лист "Экономика всего") — помесячно. Аналог
    западных обследований банковского кредитования (Bank Lending Survey /
    Senior Loan Officer Survey).

    Раньше вторым фактором Модели 3 был индикатор бизнес-климата (ИБК) —
    на честном walk-forward условия кредитования дали заметно лучший и
    более устойчивый (в т.ч. к исключению 2022 года) результат почти на
    всех сроках, вероятно потому что это более прямой канал трансмиссии
    денежно-кредитной политики к доходности облигаций, чем общий
    композитный индекс настроений. Проверили также индекс потребительских
    настроений (инФОМ) и ещё несколько субиндикаторов мониторинга (ожидания
    производства/спроса, ценовые ожидания) — ни один не улучшил результат."""
    df = _cached_read_excel(CC_FILE, sheet_name="Условия кредитования")
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    return df.sort_values("Date").reset_index(drop=True)


def _model3_cc_rate_target() -> Tuple[float, float]:
    return _memoize("_model3_cc_rate_target", (), lambda: _model3_cc_rate_target_impl())


def _model3_cc_rate_target_impl() -> Tuple[float, float]:
    """Условия кредитования линейно связаны с уровнем ключевой ставки
    (корреляция ≈-0.65 помесячно, ≈-0.68 по годовым средним — чем выше
    ставка, тем туже условия). const/slope такой регрессии используются,
    чтобы спроецировать ОЖИДАЕМЫЙ уровень условий кредитования в будущем
    через НАШ ЖЕ прогноз ключевой ставки (тот же приём, что раньше был у
    ИБК с прогнозом ВВП) — а не через безусловное историческое среднее.

    Наклон оценивается на ВСЕЙ истории (2002-2026), а не только на текущем
    режиме высокой ставки — проверено честным walk-forward (2014-2026, без
    заглядывания в будущее: прогноз ΔCC на 3/6/12 мес по наклону текущего
    момента против РЕАЛИЗОВАННОГО дальнейшего движения ставки, сравнение с
    фактом). Наклон по всей истории даёт Theil's U 0.76-0.87 на всех
    горизонтах (лучше наивного «без изменений») и обходит любое узкое
    недавнее окно (12/24/36/48 мес) — короткие окна переобучаются на шум и
    местами хуже наивного прогноза (Theil's U до 1.53 на окне 12 мес).
    Гипотеза «текущий режим менее чувствителен к ставке, условия останутся
    туже, чем предполагает старый наклон» не подтвердилась на прогнозе
    вперёд, хотя есть небольшое (+0.2..+0.75 пп относительно RMSE ~9-12 пп)
    систематическое смещение в эту сторону — слишком маленькое, чтобы
    менять механизм."""
    cc = _load_credit_conditions_monthly()
    key_lookup = key_rate_lookup()
    key_rates = np.array([key_lookup(d) for d in cc["Date"]])
    beta = _ols(key_rates, cc["CC"].values)
    return float(beta[0]), float(beta[1])


def _model3_quarterly_dataset(tenors: Tuple[int, ...]) -> pd.DataFrame:
    """Квартальный датасет для регрессии Модели 3: изменение доходности ОФЗ
    за квартал против изменения наших ожиданий по ставке и изменения условий
    кредитования. Раньше диапазон кварталов ограничивался наличием данных по
    плану Минфина (с 2021 года, ~20 кварталов) — план убрали из модели (на
    честном walk-forward бэктесте он ухудшал точность, а не просто слабо
    помогал), и вместе с ним ушло искусственное ограничение диапазона:
    теперь берётся вся история, где надёжна ключевая ставка — с
    MODEL3_SAMPLE_START (начало 2014 года, ключевая ставка введена в
    сентябре 2013), это более чем вдвое больше наблюдений."""
    cc = _load_credit_conditions_monthly()
    cc["Date"] = pd.to_datetime(cc["Date"])
    curve = load_ofz_curve().copy()
    curve["Date"] = pd.to_datetime(curve["Date"])
    key_lookup = key_rate_lookup()

    last_quarter = pd.Period(curve["Date"].max(), freq="Q")
    quarters = pd.period_range(start=pd.Period(MODEL3_SAMPLE_START, freq="Q"), end=last_quarter, freq="Q")
    rows = []
    for t in tenors:
        for q in quarters:
            q_start, q_end = q.start_time.date(), q.end_time.date()
            cs = curve[curve["Date"] <= pd.Timestamp(q_start)]
            ce = curve[curve["Date"] <= pd.Timestamp(q_end)]
            if cs.empty or ce.empty:
                continue
            re_start = forward_average_key_rate(key_lookup, q_start, days=t * 365)
            re_end = forward_average_key_rate(key_lookup, q_end, days=t * 365)
            cc_q = cc[(cc["Date"] >= pd.Timestamp(q_start)) & (cc["Date"] <= pd.Timestamp(q_end))]["CC"].mean()
            rows.append({
                "tenor": t, "quarter": str(q), "year": q.year,
                "d_yield": float(ce.iloc[-1][f"y{t}"]) - float(cs.iloc[-1][f"y{t}"]),
                "d_rate_exp": re_end - re_start,
                "cc": cc_q,
            })
    df = pd.DataFrame(rows)
    df["d_cc"] = df.groupby("tenor")["cc"].diff()
    return df


def _model3_fit(tenors: Tuple[int, ...]) -> Dict[int, np.ndarray]:
    return _memoize("_model3_fit", (tenors,), lambda: _model3_fit_impl(tenors))


def _model3_fit_impl(tenors: Tuple[int, ...]) -> Dict[int, np.ndarray]:
    """Коэффициенты per-tenor: [b_rate_exp, b_cc], БЕЗ свободного члена —
    walk-forward бэктест (и на старой, и на расширенной до 2014 года
    выборке) показал, что intercept систематически ухудшает точность на
    всех 4 сроках. 2022 год остаётся в выборке — теперь это лишь один
    эпизод из ~50 кварталов (2014-2026), и коэффициент при условиях
    кредитования (в отличие от того, что было с ИБК) устойчив к его
    исключению."""
    df = _model3_quarterly_dataset(tenors)
    coefs: Dict[int, np.ndarray] = {}
    for t in tenors:
        sub = df[df["tenor"] == t].dropna(subset=["d_yield", "d_rate_exp", "d_cc"])
        X = sub[["d_rate_exp", "d_cc"]].values
        beta, *_ = np.linalg.lstsq(X, sub["d_yield"].values, rcond=None)
        coefs[t] = beta
    return coefs


def model3_ofz_forecast(tenors: Tuple[int, ...] = tuple(TENORS), live_date=None,
                         live_curve: Optional[Dict[int, float]] = None, key_lookup=None) -> Dict[str, Dict[int, float]]:
    """Модель №3 прогноза доходности ОФЗ: регрессия изменения доходности БЕЗ
    свободного члена на (1) изменение НАШИХ ожиданий по ставке
    [forward_average_key_rate] и (2) изменение условий кредитования
    (обследование ЦБ, баланс ответов) — коэффициенты оцениваются по
    квартальным данным с 2014 года (~50 кварталов), см. _model3_fit.

    Для прогноза вперёд: изменение ожиданий по ставке берётся из нашего же
    прогноза (как в Модели 1). Изменение условий кредитования на горизонте
    берётся НЕ из абсолютного уровня регрессии (cc_const + cc_slope*ставка),
    а только из её НАКЛОНА: d_cc = cc_slope * (наш прогноз ставки на дату
    минус ставка сегодня). Первая версия пыталась ещё и стягивать
    сегодняшний фактический уровень условий кредитования к абсолютному
    регрессионному таргету для сегодняшней ставки — но сегодняшний
    фактический уровень (-6.8) оказался ГОРАЗДО менее жёстким, чем
    предсказывает простая линейная регрессия для сегодняшних 14% (-18.2,
    оценка сильно тянется экстремальными наблюдениями 2024-2025 гг. при
    ставке 17-19%, где связь, вероятно, нелинейна) — попытка стянуть
    сегодняшний уровень к этому нереалистичному таргету давала ПЕРЕВЁРНУТЫЙ
    знак: доходность росла при прогнозе смягчения ставки. Наклон регрессии
    (чувствительность условий кредитования к ИЗМЕНЕНИЮ ставки) — гораздо
    более устойчивая, переносимая величина, чем её абсолютный уровень при
    конкретной ставке, и не требует верить в точность экстраполяции
    регрессии на весь диапазон ставок."""
    curve = load_ofz_curve()
    last_row = pd.Series({f"y{t}": live_curve[t] for t in TENORS}) if live_curve else curve.iloc[-1]
    last_date = live_date or curve["Date"].max()
    key_lookup = key_lookup or key_rate_lookup()
    coefs = _model3_fit(tenors)
    _cc_const, cc_slope = _model3_cc_rate_target()
    key_rate_today = key_lookup(last_date)

    snapshots: Dict[str, Dict[int, float]] = {"Сейчас": {t: round(float(last_row[f"y{t}"]), 3) for t in tenors}}
    for months, label in MODEL3_HORIZONS:
        future_date = last_date + relativedelta(months=months)
        d_cc = cc_slope * (key_lookup(future_date) - key_rate_today)
        row: Dict[int, float] = {}
        for t in tenors:
            b_rate, b_cc = coefs[t]
            d_rate = forward_average_key_rate(key_lookup, future_date, days=t * 365) - \
                forward_average_key_rate(key_lookup, last_date, days=t * 365)
            row[t] = round(float(last_row[f"y{t}"]) + b_rate * d_rate + b_cc * d_cc, 3)
        snapshots[label] = row
    return snapshots


MODEL3_WALKFORWARD_MIN_WINDOW = 12


def model3_quality(tenors: Tuple[int, ...] = (1, 2, 5, 10)) -> Dict[str, Dict[int, Dict[str, Optional[float]]]]:
    """Точность Модели 3 через РАСШИРЯЮЩЕЕСЯ ОКНО (walk-forward), как и у
    остальных моделей — не LOOCV (см. историю правок для объяснения, почему
    LOOCV на квартальном временном ряде даёт обманчиво хорошие цифры).

    После замены ИБК на условия кредитования (см. _load_credit_conditions_monthly)
    и удаления свободного члена результат заметно улучшился на 3 из 4
    сроков: 1 год и 2 года устойчиво бьют наивный прогноз, 5 лет — почти
    паритет, 10 лет — на уровне наивного."""
    df = _model3_quarterly_dataset(tenors)
    result: Dict[str, Dict[int, Dict[str, Optional[float]]]] = {"rate_credit": {}}
    for t in tenors:
        sub = df[df["tenor"] == t].dropna(subset=["d_yield", "d_rate_exp", "d_cc"]).reset_index(drop=True)
        X = sub[["d_rate_exp", "d_cc"]].values
        y = sub["d_yield"].values
        n = len(sub)
        expected, fact, naive = [], [], []
        for i in range(MODEL3_WALKFORWARD_MIN_WINDOW, n):
            beta, *_ = np.linalg.lstsq(X[:i], y[:i], rcond=None)
            pred = X[i] @ beta
            expected.append(float(pred))
            fact.append(float(y[i]))
            naive.append(0.0)
        result["rate_credit"][t] = _accuracy_stats(expected, fact, naive)
    return result


# ---------------- Итоговый ансамбль (Модель 1 + 2 + 3) для вкладки "Прогноз доходностей" ----------------
ENSEMBLE_SNAPSHOT_MONTHS = {"Сейчас": 0, "+3 мес": 3, "+6 мес": 6, "+12 мес": 12}
ENSEMBLE_QUANTILE_LOW = 0.20
ENSEMBLE_QUANTILE_HIGH = 0.80


def _snapshot_monthly_interp(snapshots: Dict[str, Dict[int, float]], tenors: Tuple[int, ...],
                              months_ahead: int = 12) -> Dict[int, Dict[int, float]]:
    """Линейная интерполяция помесячно между 4 точками модели (Сейчас/+3/+6/+12
    мес) — модели 1-3 считают только эти 4 снапшота, а графику на "Прогноз
    доходностей" нужен непрерывный помесячный ряд."""
    knots = sorted(
        ((ENSEMBLE_SNAPSHOT_MONTHS[k], v) for k, v in snapshots.items() if k in ENSEMBLE_SNAPSHOT_MONTHS),
        key=lambda kv: kv[0],
    )
    result: Dict[int, Dict[int, float]] = {m: {} for m in range(months_ahead + 1)}
    for t in tenors:
        pts = [(m, v[t]) for m, v in knots]
        for m in range(months_ahead + 1):
            if m <= pts[0][0]:
                val = pts[0][1]
            elif m >= pts[-1][0]:
                val = pts[-1][1]
            else:
                val = pts[-1][1]
                for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                    if x0 <= m <= x1:
                        w = (m - x0) / (x1 - x0) if x1 != x0 else 0.0
                        val = y0 + w * (y1 - y0)
                        break
            result[m][t] = val
    return result


ENSEMBLE_CACHE_TTL_SECONDS = 120.0
_ensemble_cache: Dict[tuple, Tuple[float, tuple]] = {}


def ensemble_ofz_forecast(tenors: Tuple[int, ...] = tuple(TENORS), months_ahead: int = 12,
                           live_date=None, live_curve: Optional[Dict[int, float]] = None):
    """Обёртка над _ensemble_ofz_forecast_impl с коротким TTL-кэшем (2 мин):
    сама модель дорогая (3 регрессии/декомпозиции, несколько Excel-файлов на
    каждый вызов), а "Прогноз доходностей" вызывает её дважды за один
    рендер страницы (график кривой + таблица ожидаемой доходности) — без
    кэша это удваивает нагрузку и рискует не уложиться в таймаут
    serverless-контейнера. Live-котировка меняется не чаще, чем раз в
    несколько минут, так что 2-минутный кэш не теряет актуальности."""
    # Округляем live_curve в ключе кэша до 2 знаков: после добавления сценариев
    # по ставке (см. _ensemble_ofz_forecast_impl) полный пересчёт стал заметно
    # дороже, а без округления повторный вызов в течение тех же 2 минут (ровно
    # то, что кэш должен отдавать бесплатно — "Прогноз доходностей" и сводная
    # таблица дёргают ансамбль дважды за один рендер) промахивался мимо кэша
    # всякий раз, когда живая котировка MOEX сдвигалась хотя бы на сотую
    # процента между двумя запросами, реально рискуя таймаутом контейнера.
    live_curve_key = tuple(sorted((t, round(v, 2)) for t, v in live_curve.items())) if live_curve else None
    key = (tuple(tenors), months_ahead, live_date, live_curve_key)
    now = time.time()
    cached = _ensemble_cache.get(key)
    if cached and now - cached[0] < ENSEMBLE_CACHE_TTL_SECONDS:
        return cached[1]
    result = _ensemble_ofz_forecast_impl(tenors, months_ahead, live_date, live_curve)
    _ensemble_cache[key] = (now, result)
    return result


def _ensemble_ofz_forecast_impl(tenors: Tuple[int, ...], months_ahead: int, live_date, live_curve: Optional[Dict[int, float]]):
    """Итоговый прогноз кривой ОФЗ для вкладки "Прогноз доходностей" — простое
    среднее трёх независимых моделей с вкладки "ОФЗ" (Модель 1: наш прогноз КС
    + поправка на рынок; Модель 2: декомпозиция по Фишеру, реальная ставка +
    инфляция; Модель 3: ожидания по ставке + бизнес-настроения), плюс
    интервал 20-80%.

    При всего 3 моделях классический процентиль не определён — используется
    тот же приём, что и в доверительном интервале прогноза ключевой ставки
    (build_key_rate_band): минимум и максимум берутся как условные 0%/100%,
    среднее БАЗОВОГО сценария 3 моделей — как 50%, а P20/P80 линейно
    интерполируются между ними. Это гарантирует, что среднее ВСЕГДА строго
    внутри интервала.

    В минимум/максимум входит не только несогласие между 3 моделями на
    ОДНОМ прогнозе ставки, но и несогласие МЕЖДУ сценариями ставки внутри
    Моделей 1 и 3 (оптимист/пессимист, см. key_rate_lookup_scenario) — без
    этого полоса на коротких сроках получалась почти нулевой шириной (0.1-
    0.4 п.п. на 1 годе): все 3 модели опираются на один и тот же базовый
    прогноз ставки, поэтому расходятся между собой мало, а неопределённость
    самого прогноза ставки (для которой у нас есть готовая полоса
    оптимист/пессимист) никак не отражалась. Модель 2 в сценарный прогон не
    входит — она не использует наш прогноз ключевой ставки вообще."""
    snap1 = model1_ofz_forecast(tenors, live_date=live_date, live_curve=live_curve)
    snap2 = model2_inflation_forecast(tenors, live_date=live_date, live_curve=live_curve)
    snap3 = model3_ofz_forecast(tenors, live_date=live_date, live_curve=live_curve)

    m1 = _snapshot_monthly_interp(snap1, tenors, months_ahead)
    m2 = _snapshot_monthly_interp(snap2, tenors, months_ahead)
    m3 = _snapshot_monthly_interp(snap3, tenors, months_ahead)

    scenario_m: Dict[str, Dict[int, Dict[int, float]]] = {}
    for scenario in ("Optimistic", "Pessimistic"):
        scen_lookup = key_rate_lookup_scenario(scenario)
        snap1_s = model1_ofz_forecast(tenors, live_date=live_date, live_curve=live_curve, key_lookup=scen_lookup)
        snap3_s = model3_ofz_forecast(tenors, live_date=live_date, live_curve=live_curve, key_lookup=scen_lookup)
        scenario_m[f"1_{scenario}"] = _snapshot_monthly_interp(snap1_s, tenors, months_ahead)
        scenario_m[f"3_{scenario}"] = _snapshot_monthly_interp(snap3_s, tenors, months_ahead)

    monthly: Dict[int, Dict[int, Dict[str, float]]] = {}
    for m in range(months_ahead + 1):
        monthly[m] = {}
        for t in tenors:
            v1, v2, v3 = m1[m][t], m2[m][t], m3[m][t]
            avg = (v1 + v2 + v3) / 3.0
            candidates = [v1, v2, v3] + [scenario_m[key][m][t] for key in scenario_m]
            lo, hi = min(candidates), max(candidates)
            p20 = lo + (avg - lo) * (ENSEMBLE_QUANTILE_LOW / 0.5)
            p80 = avg + (hi - avg) * ((ENSEMBLE_QUANTILE_HIGH - 0.5) / 0.5)
            monthly[m][t] = {"avg": round(avg, 3), "p20": round(p20, 3), "p80": round(p80, 3)}

    snapshots = {
        label: {t: monthly[months][t]["avg"] for t in tenors}
        for label, months in ENSEMBLE_SNAPSHOT_MONTHS.items()
    }
    return snapshots, monthly


def _spread_interpolator(spreads: Dict[int, float]):
    xs = sorted(spreads.keys())
    ys = [spreads[x] for x in xs]

    def interp(m: float) -> float:
        if m <= xs[0]:
            return ys[0]
        if m >= xs[-1]:
            return ys[-1]
        for i in range(len(xs) - 1):
            if xs[i] <= m <= xs[i + 1]:
                w = (m - xs[i]) / (xs[i + 1] - xs[i])
                return ys[i] + w * (ys[i + 1] - ys[i])
        return ys[-1]

    return interp


def _ensemble_ofz_total_return_1y(monthly_ensemble: Dict[int, Dict[int, Dict[str, float]]],
                                   today_yield: Dict[int, float], maturity: int, scenario: str = "avg") -> float:
    """То же самое, что делала _ofz_total_return_1y (доходность к погашению
    сегодня + переоценка тела облигации от прогнозного изменения доходности
    через год), но будущая доходность берётся из АНСАМБЛЯ 3 моделей
    (ensemble_ofz_forecast) — той же кривой, что показана на графиках
    вкладки "Прогноз доходностей", а не из отдельной устаревшей
    _ofz_spread_model, чтобы таблица не расходилась с графиком."""
    y0 = today_yield[maturity]
    remaining = maturity - 1
    if remaining <= 0:
        return y0  # погашается в течение года — просто фиксированная доходность к погашению
    points = {t: monthly_ensemble[12][t][scenario] for t in TENORS}
    y1 = _spread_interpolator(points)(remaining)
    p0 = (1 + y0 / 100) ** (-maturity)
    p1 = (1 + y1 / 100) ** (-remaining)
    return (p1 / p0 - 1) * 100


def expected_asset_returns(mmf_spread: float = -0.5, live_date=None, live_curve: Optional[Dict[int, float]] = None):
    """Ожидаемая доходность за 12 месяцев для денег, вложенных сегодня:
    депозит на 1 год (фиксируется уже сегодня — без интервала, неопределённости
    нет), фонд денежного рынка (RUONIA-подобный, минус спред управляющего) и
    ОФЗ 1/5/10 лет (полная доходность владения за год — купон/накопленная
    доходность + переоценка тела облигации). Для фонда денежного рынка и ОФЗ
    добавлен доверительный интервал 20-80%: для фонда — та же полоса ключевой
    ставки (build_key_rate_band), что и на вкладке "Прогноз доходностей"; для
    ОФЗ — P20/P80 ансамбля 3 моделей (ensemble_ofz_forecast)."""
    key_lookup = key_rate_lookup()
    curve = load_ofz_curve()
    last_date = live_date or curve["Date"].max()
    today_yield = live_curve or {t: float(curve.iloc[-1][f"y{t}"]) for t in TENORS}

    hist_dep, _fut_dep, _spread = forecast_deposit_rate()
    deposit_1y = float(hist_dep["Rate"].iloc[-1])

    mmf_1y = forward_average_key_rate(key_lookup, last_date) + mmf_spread
    band = build_key_rate_band()
    mmf_lo, mmf_hi = forward_average_key_rate_band(band, last_date)
    mmf_lo, mmf_hi = mmf_lo + mmf_spread, mmf_hi + mmf_spread

    _snapshots, monthly = ensemble_ofz_forecast(tuple(TENORS), live_date=live_date, live_curve=live_curve)

    rows = [
        {"asset": "Депозит на 1 год", "value": round(deposit_1y, 2)},
        {"asset": "Фонд денежного рынка", "value": round(mmf_1y, 2),
         "value_p20": round(mmf_lo, 2), "value_p80": round(mmf_hi, 2)},
    ]
    for label, maturity in (("ОФЗ 1 год", 1), ("ОФЗ 5 лет", 5), ("ОФЗ 10 лет", 10)):
        # доходность ОФЗ (цена облигации) движется ОБРАТНО доходности к погашению —
        # сценарий P20 по будущей ставке (ниже ставка) даёт ВЫСШУЮ доходность владения,
        # а P80 (выше ставка) — низшую, поэтому границы сортируются, а не берутся напрямую
        r_lo = _ensemble_ofz_total_return_1y(monthly, today_yield, maturity, "p20")
        r_hi = _ensemble_ofz_total_return_1y(monthly, today_yield, maturity, "p80")
        rows.append({
            "asset": label,
            "value": round(_ensemble_ofz_total_return_1y(monthly, today_yield, maturity, "avg"), 2),
            "value_p20": round(min(r_lo, r_hi), 2),
            "value_p80": round(max(r_lo, r_hi), 2),
        })
    return rows, last_date, mmf_spread
