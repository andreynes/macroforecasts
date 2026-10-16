"""Загрузка исходных данных для репликации правила Тейлора Банка России.

Источники:
  * Ключевая ставка (дневная)            — cbr.ru/hd_base/KeyRate
  * Инфляция ИПЦ г/г и цель (месячные)    — cbr.ru/hd_base/infl
  * ВВП квартальный, с искл. сезонности   — Росстат, VVP_kvartal_s_1995-YYYY.xlsx

Результат: data/raw/keyrate_daily.csv, data/raw/inflation_monthly.csv, data/raw/gdp_quarterly.xlsx
"""
from __future__ import annotations

import datetime as dt
import io
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
HEADERS = {"User-Agent": "Mozilla/5.0"}

CBR_KEYRATE = "https://www.cbr.ru/hd_base/KeyRate/"
CBR_INFL = "https://www.cbr.ru/hd_base/infl/"
ROSSTAT_GDP = "https://rosstat.gov.ru/storage/mediabank/VVP_kvartal_s_1995-{year}.xlsx"


def _cbr_table(url: str, date_from: str, date_to: str) -> pd.DataFrame:
    params = {"UniDbQuery.Posted": "True", "UniDbQuery.From": date_from, "UniDbQuery.To": date_to}
    r = requests.get(url, params=params, headers=HEADERS, timeout=120)
    r.raise_for_status()
    # дату читаем строкой, иначе «12.2020» превращается в число 12.202
    return pd.read_html(io.StringIO(r.text), decimal=",", thousands=" ", converters={"Дата": str})[0]


def download_keyrate(today: str) -> pd.DataFrame:
    df = _cbr_table(CBR_KEYRATE, "13.09.2013", today)
    df.columns = ["date", "key_rate"]
    df["date"] = pd.to_datetime(df["date"], format="%d.%m.%Y")
    return df.sort_values("date").reset_index(drop=True)


def download_inflation(today: str) -> pd.DataFrame:
    df = _cbr_table(CBR_INFL, "01.01.2013", today)
    df.columns = ["month", "key_rate_eom", "cpi_yoy", "target"]
    df["month"] = pd.to_datetime(df["month"], format="%m.%Y").dt.to_period("M")
    df["target"] = pd.to_numeric(df["target"], errors="coerce")
    return df.sort_values("month").reset_index(drop=True)


def download_gdp(path: Path) -> None:
    # Сертификат rosstat.gov.ru выпущен российским УЦ, которого нет в certifi,
    # поэтому качаем системным curl (он использует системное хранилище сертификатов).
    # Росстат меняет год в имени файла по мере обновления — пробуем от текущего года назад.
    for year in range(dt.date.today().year + 1, 2022, -1):
        url = ROSSTAT_GDP.format(year=year)
        res = subprocess.run(["curl", "-sfL", "-m", "300", "-A", HEADERS["User-Agent"], "-o", str(path), url])
        if res.returncode == 0 and path.read_bytes()[:2] == b"PK":
            print(f"ВВП: {url}")
            return
    raise RuntimeError("Не удалось скачать квартальный ВВП с сайта Росстата")


CBR_FORECAST_PAGES = ["https://www.cbr.ru/dkp/cal_mp/", "https://www.cbr.ru/dkp/mp_dec/decision_key_rate/"]
DEC_DEC_LABEL = "Инфляция, в %, декабрь к декабрю предыдущего года"


def download_cbr_forecasts(folder: Path) -> pd.DataFrame:
    """PDF «Среднесрочный прогноз Банка России» (с 2019 г., опорные заседания) и разбор строки
    «Инфляция, декабрь к декабрю»: факт прошлого года и прогнозы на текущий и 2 следующих года."""
    import re

    import pypdf

    folder.mkdir(parents=True, exist_ok=True)
    links = set()
    for page in CBR_FORECAST_PAGES:
        html = requests.get(page, headers=HEADERS, timeout=120).text
        links |= set(re.findall(r'href="([^"]*forecast_\d{6}\.pdf)"', html))
    rows = []
    for link in sorted(links, key=lambda s: s[-10:]):
        path = folder / link.rsplit("/", 1)[-1]
        if not path.exists():
            path.write_bytes(requests.get("https://www.cbr.ru" + link, headers=HEADERS, timeout=120).content)
        date = dt.datetime.strptime(re.search(r"(\d{6})\.pdf", path.name).group(1), "%y%m%d").date()
        text = "\n".join(p.extract_text() for p in pypdf.PdfReader(path).pages[:3])
        lines = text.split("\n")
        infl = parse_forecast_line(lines, DEC_DEC_LABEL)
        kr = parse_forecast_line(lines, KR_LABEL)
        gdp = parse_forecast_line(lines, GDP_LABEL)
        row = {"date": date, "source": path.name, "raw": infl["raw"],
               "fact_prev_year": infl["vals"][0], "fc_y": infl["vals"][1], "fc_y1": infl["vals"][2],
               "fc_y2": infl["vals"][3], "fc_y3": infl["vals"][4],
               "gdp_prev": gdp["vals"][0], "gdp_y": gdp["vals"][1], "gdp_y1": gdp["vals"][2], "gdp_y2": gdp["vals"][3], "gdp_y3": gdp["vals"][4],
               "kr_y1": kr["vals"][2], "kr_y2": kr["vals"][3], "kr_y3": kr["vals"][4], "kr_raw": kr["raw"],
               **{f"kr_y{k}_{b}": kr[b][k + 1] for k in (1, 2, 3) for b in ("lo", "hi")}}
        # сноска 1: средняя ключевая ставка с даты заседания до конца текущего года
        m = re.search(r"с (\d{1,2}) (\w+) до конца (\d{4}) года средняя ключевая\s+ставка\s+прогнозируется в\s+"
                      r"(?:диапазоне\s+)?(\d+,\d)(?:\s*[–-]\s*(\d+,\d))?", text.replace("\n", " "))
        if m:
            nums = [float(x.replace(",", ".")) for x in m.group(4, 5) if x]
            row["kr_rest"], row["kr_rest_lo"], row["kr_rest_hi"] = np.mean(nums), min(nums), max(nums)
            row["kr_rest_from"] = dt.date(int(m.group(3)), MONTHS_GEN[m.group(2)], int(m.group(1)))
        rows.append(row)
    return pd.DataFrame(rows)


MEETINGS_FROM = dt.date(2018, 1, 1)
# заседания, пресс-релиз которых лежит по нестандартному адресу (перебор их не находит)
MANUAL_MEETINGS = {dt.date(2023, 6, 9): "пресс-релиз (нестандартный адрес)"}
PRESS_RELEASE_URL = "https://www.cbr.ru/press/pr/?file={d:%d%m%Y}_133000key.htm"


def _has_press_release(d: dt.date) -> bool:
    try:
        r = requests.get(PRESS_RELEASE_URL.format(d=d), headers=HEADERS, timeout=60)
        # на несуществующий адрес сайт отдаёт заглушку или другой пресс-релиз — проверяем дату решения в тексте
        return r.ok and "ключев" in r.text and f"{d:%d.%m.%Y}" in r.text
    except requests.RequestException:
        return False


def download_meetings(keyrate: pd.DataFrame, cache: Path) -> pd.DataFrame:
    """Даты заседаний по ключевой ставке.

    1) ссылки на пресс-релизы (ДДММГГГГ_ЧЧММССkey.htm) на страницах ЦБ — полный список только за последние годы;
    2) для более ранних лет — перебор пятниц с MEETINGS_FROM: есть ли пресс-релиз ДДММГГГГ_133000key.htm
       (результат кешируется в cache, перебираются только пятницы, которых там нет);
    3) внеочередные заседания (не в пятницу 13:30) — по изменениям ставки, которым не предшествует найденное заседание:
       берётся день перед вступлением новой ставки в силу (тип «внеочередное (по изменению ставки)»).
    """
    import re
    from concurrent.futures import ThreadPoolExecutor

    rows = {}
    for page in CBR_FORECAST_PAGES + ["https://www.cbr.ru/dkp/mp_dec/"]:
        html = requests.get(page, headers=HEADERS, timeout=120).text
        for x in re.findall(r"(\d{8})_\d{6}key\.htm", html):
            rows[dt.datetime.strptime(x, "%d%m%Y").date()] = "пресс-релиз"
    old = pd.read_csv(cache, parse_dates=["date"]) if cache.exists() else pd.DataFrame(columns=["date", "source", "checked"])
    checked = set(pd.to_datetime(old.get("checked", pd.Series(dtype=str)).dropna()).dt.date)
    for _, r in old[old["source"] == "пресс-релиз"].iterrows():
        rows[r["date"].date()] = "пресс-релиз"

    # страницы ЦБ дают полный список только за последние годы: перебираем пятницы до начала непрерывного участка
    page_dates = sorted(rows)
    complete_from = page_dates[-1] if page_dates else dt.date.today()
    for a, b in zip(reversed(page_dates[:-1]), reversed(page_dates[1:])):
        if (b - a).days > 80:  # между заседаниями не больше ~2,5 месяцев
            break
        complete_from = a
    fridays = [d.date() for d in pd.date_range(MEETINGS_FROM, complete_from, freq="W-FRI")]
    todo = [d for d in fridays if d not in checked and d not in rows]
    with ThreadPoolExecutor(4) as ex:
        for d, ok in zip(todo, ex.map(_has_press_release, todo)):
            if ok:
                rows[d] = "пресс-релиз"
    checked |= set(todo)

    kr = keyrate.set_index("date")["key_rate"]
    changes = kr[kr.ne(kr.shift())].index[1:]
    found = sorted(rows)
    for e in changes:
        e = e.date()
        if e >= MEETINGS_FROM and not any(0 < (e - m).days <= 10 for m in found):
            rows[e] = "внеочередное (по изменению ставки)"  # внеочередные решения вступали в силу в тот же день
    rows.update(MANUAL_MEETINGS)
    out = pd.DataFrame({"date": sorted(rows), "source": [rows[d] for d in sorted(rows)]})
    out["checked"] = None
    extra = pd.DataFrame({"date": None, "source": "проверено", "checked": sorted(checked)})
    pd.concat([out, extra]).to_csv(cache, index=False)
    return out


def download_meeting_calendar() -> pd.DataFrame:
    """Календарь заседаний Совета директоров по ключевой ставке (включая будущие) со страницы ЦБ."""
    import html as html_lib
    import re

    page = requests.get("https://www.cbr.ru/dkp/cal_mp/", headers=HEADERS, timeout=120).text
    text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", page)))
    found = re.findall(r"(\d{1,2}) (\w+) (\d{4}) года Заседание Совета директоров Банка России по ключевой ставке", text)
    dates = sorted({dt.date(int(y), MONTHS_GEN[m], int(d)) for d, m, y in found if m in MONTHS_GEN})
    return pd.DataFrame({"date": dates})


KR_LABEL = "Ключевая ставка, в среднем за год, в % годовых"
GDP_LABEL = "Валовой внутренний продукт"
MONTHS_GEN = {m: i for i, m in enumerate(["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                                          "сентября", "октября", "ноября", "декабря"], start=1)}


def parse_forecast_line(lines: list[str], label: str) -> dict:
    """Строка таблицы прогноза: факт прошлого года и диапазоны на годы вперёд → середины диапазонов.
    Учитывает сноски, приклеенные к числу («7,9-8,31»), и отрицательные значения «-(4,0-6,0)», «(-1,0)-(+1,0)»."""
    import re

    line = next((l for l in lines if l.startswith(label)), None)
    out = {"vals": [np.nan] * 5, "lo": [np.nan] * 5, "hi": [np.nan] * 5, "raw": None}
    if line is None:
        return out
    tokens = line[len(label):].replace("–", "-").split()
    out["raw"] = " | ".join(tokens)
    if len(tokens) > 5:  # в части PDF 2019 г. таблица продублирована (два сценария) — берём факт + 3 года
        tokens = tokens[:4]
    vals, lo, hi = [], [], []
    for tok in tokens[:5]:
        # знак — только сразу после скобки «(-1,0)»; дефис в «4,7-5,2» — разделитель диапазона
        nums = [float(x.replace(",", ".")) for x in re.findall(r"(?<=\()[+-]\d+,\d|\d+,\d", tok)]
        if tok.startswith("-(") and nums:  # «-(4,0-6,0)» — отрицательный диапазон
            nums = [-abs(x) for x in nums]
        vals.append(np.mean(nums) if nums else np.nan)
        lo.append(min(nums) if nums else np.nan)
        hi.append(max(nums) if nums else np.nan)
    out["vals"], out["lo"], out["hi"] = [(v + [np.nan] * 5)[:5] for v in (vals, lo, hi)]
    return out


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().strftime("%d.%m.%Y")

    kr = download_keyrate(today)
    kr.to_csv(RAW / "keyrate_daily.csv", index=False)
    print(f"Ключевая ставка: {kr['date'].min():%Y-%m-%d} — {kr['date'].max():%Y-%m-%d}")

    infl = download_inflation(today)
    infl.to_csv(RAW / "inflation_monthly.csv", index=False)
    print(f"Инфляция: {infl['month'].min()} — {infl['month'].max()}")

    download_gdp(RAW / "gdp_quarterly.xlsx")

    import extract_cbr_gap  # разрыв выпуска ЦБ из «Основных направлений» (калибруется по ВВП Росстата)
    extract_cbr_gap.main()

    cal = download_meeting_calendar()
    cal.to_csv(RAW / "cbr_meeting_calendar.csv", index=False)
    print(f"Календарь заседаний ЦБ: {len(cal)} дат, {cal['date'].min()} — {cal['date'].max()}")

    mt = download_meetings(kr, RAW / "cbr_meetings_cache.csv")
    mt.to_csv(RAW / "cbr_meetings.csv", index=False)
    print(f"Заседания по ставке: {len(mt)} дат, {mt['date'].min()} — {mt['date'].max()}")

    fc = download_cbr_forecasts(RAW / "cbr_forecasts").drop_duplicates("date")
    fc.to_csv(RAW / "cbr_inflation_forecasts.csv", index=False)
    print(f"Прогнозы ЦБ: {len(fc)} шт., {fc['date'].min()} — {fc['date'].max()}")


if __name__ == "__main__":
    main()
