"""ЧЕРНОВИК (этап 1, ветка feature/gdp): данные вкладки «ВВП и промышленность» → data/gdp_activity.json.

Читает уже скачанные файлы из папки RAW (ничего не скачивает — Росстат опрашивать бережно):
  Росстат  VVP_kvartal_s-*.xlsx            ВВП: лист 10 — уровень с искл. сезонности (ц. 2021), лист 12 — % г/г
           GDP-quarters-of-use-*.xls       ВВП по использованию: лист 13 — % г/г
           VDS_kvartal_OKVED2_s-*.xlsx     ВДС по отраслям: лист 6 — % г/г
           ind_baza_2023.xlsx              индексы производства по ОКВЭД2: лист 2 — % г/г, месяц
           sezon_2023_*.xlsx               ИПП с искл. сезонного и календарного факторов (2023 = 100)
  ЦБ       mp_survey_data.xlsx             мониторинг предприятий: ИБК, баланс ответов, загрузка мощностей
  МЭР      minec_*.pdf                     «О текущей ситуации в российской экономике» — месячная оценка ВВП (текст)
Имена файлов Росстата с датой меняются каждый выпуск — в боевой версии их брать со страницы раздела.

Запуск:  python data_scripts/gdp/build_gdp_json.py RAW_DIR [OUT_JSON]
"""
import glob
import json
import math
import os
import re
import sys
from datetime import date

import pandas as pd

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def one(raw, pattern):
    files = sorted(glob.glob(os.path.join(raw, pattern)))
    if not files:
        sys.exit(f"нет файла {pattern} в {raw}")
    return files[-1]


def clean(v):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return round(float(v), 3)


def quarter_table(path, sheet):
    """Лист Росстата «годы × кварталы»: строка с годами (сноски вида 20262) → 2026), под ней строка кварталов.
    Возвращает (список меток 'ГГГГ Qn', {подпись строки: [значения]})."""
    d = pd.read_excel(path, sheet_name=sheet, header=None)
    yr_row = next(i for i in range(len(d)) if any(re.match(r"^(19|20)\d\d", str(x)) for x in d.iloc[i]))
    years = d.iloc[yr_row].copy()
    years = years.where(years.astype(str).str.match(r"^(19|20)\d\d"), None).ffill()
    qrow = d.iloc[yr_row + 1]
    cols, labels = [], []
    for j, q in qrow.items():
        m = re.match(r"^(IV|I{1,3})\s*квартал", str(q).strip())
        if m and years[j] is not None:
            cols.append(j)
            labels.append(f"{str(years[j])[:4]} Q{ROMAN[m.group(1)]}")
    rows = {}
    for i in range(yr_row + 2, len(d)):
        r = d.iloc[i]
        vals = [pd.to_numeric(r[j], errors="coerce") for j in cols]
        if all(pd.isna(v) for v in vals):
            continue
        name = " ".join(str(x).strip() for j, x in r.items() if j < cols[0] and pd.notna(x)) or "значение"
        rows[name] = [clean(v) for v in vals]
    # хвост: кварталы, которых ещё нет (пустые во всех строках)
    n = max(i for i in range(len(labels)) if any(v[i] is not None for v in rows.values())) + 1
    return labels[:n], {k: v[:n] for k, v in rows.items()}


def gdp_blocks(raw):
    f = one(raw, "VVP_kvartal_s*.xlsx")
    q_yoy, yoy = quarter_table(f, "12")
    q_sa, sa = quarter_table(f, "10")
    yoy = list(yoy.values())[0]
    sa = list(sa.values())[0]
    saar = {q_sa[i]: clean(((sa[i] / sa[i - 1]) ** 4 - 1) * 100) for i in range(1, len(sa)) if sa[i] and sa[i - 1]}
    rows = [{"q": q, "yoy": clean(v - 100) if v is not None else None, "saar": saar.get(q)} for q, v in zip(q_yoy, yoy)]
    return [r for r in rows if r["q"] >= "2021"]


def use_block(raw):
    labels, rows = quarter_table(one(raw, "GDP-quarters-of-use*.xls"), "13")
    want = [("Валовой внутренний продукт", "ВВП"), ("домашних хозяйств", "Потребление домохозяйств"),
            ("государственного управления", "Госпотребление"), ("валовое накопление основного", "Инвестиции в основной капитал"),
            ("экспорт", "Экспорт"), ("импорт", "Импорт")]  # экспорт/импорт Росстат сейчас не публикует — выпадут
    out = {}
    for key, name in want:
        k = next((k for k in rows if k.lower().startswith(key.lower())), None)
        if k and any(v is not None for v in rows[k][-8:]):
            out[name] = [clean(v - 100) if v is not None else None for v in rows[k]]
    start = labels.index(next(q for q in labels if q >= "2021"))
    return {"quarters": labels[start:], "series": {k: v[start:] for k, v in out.items()}}


def vds_block(raw):
    labels, rows = quarter_table(one(raw, "VDS_kvartal_OKVED2*.xlsx"), "6")
    sel = []
    for k, v in rows.items():
        m = re.match(r"^(Раздел [A-T])\s+(.*)", k)
        if m or k.startswith("Валовой внутренний") or k.startswith("Валовая добавленная"):
            code = m.group(1).replace("Раздел ", "") if m else ""
            name = (m.group(2) if m else k)
            sel.append({"code": code, "name": name[:60], "vals": [clean(x - 100) if x is not None else None for x in v[-6:]]})
    return {"quarters": labels[-6:], "rows": sel}


def ipp_yoy(raw):
    d = pd.read_excel(one(raw, "ind_baza_2023.xlsx"), sheet_name="2", header=None)
    hdr = next(i for i in range(len(d)) if str(d.iloc[i, 0]).startswith("Наименование"))
    years = d.iloc[hdr].where(d.iloc[hdr].astype(str).str.match(r"^20\d\d"), None).ffill()
    months = d.iloc[hdr + 1]
    cols, labels = [], []
    for j, m in months.items():
        if str(m).strip().lower() in MONTHS and years[j] is not None:
            cols.append(j)
            labels.append(f"{str(years[j])[:4]}-{MONTHS.index(str(m).strip().lower()) + 1:02d}")
    rows = []
    for i in range(hdr + 2, len(d)):
        name, code = str(d.iloc[i, 0]).strip(), str(d.iloc[i, 1]).strip()
        if name == "nan":
            continue
        vals = [pd.to_numeric(d.iloc[i, j], errors="coerce") for j in cols]
        rows.append({"code": code, "name": re.sub(r"\d+\s*\(.*$|\d$", "", name).strip(), "vals": vals})
    n = max(i for i in range(len(labels)) if pd.notna(rows[0]["vals"][i])) + 1
    labels = labels[:n]
    total = next(r for r in rows if r["code"].startswith("BCDE"))
    series = {"months": [m for m in labels if m >= "2021"],
              "total": [clean(v - 100) for m, v in zip(labels, total["vals"][:n]) if m >= "2021"]}
    keep = ["BCDE", "B", "C", "D", "E", "06", "10", "19", "20", "24", "25", "26", "27", "28", "29", "30"]
    table = []
    for code in keep:
        r = next((r for r in rows if r["code"].replace(" ", "") == code), None)
        if r and any(pd.notna(v) for v in r["vals"][n - 6:n]):  # нефть и газ Росстат скрывает — строки нет
            name = re.sub(r"^Производство\s+", "", r["name"]).replace("Обеспечение электрической энергией, газом и паром; конд",
                                                                         "Электроэнергия, газ и пар")
            table.append({"code": code, "name": (name[:1].upper() + name[1:].lower())[:48],
                          "vals": [clean(v - 100) if pd.notna(v) else None for v in r["vals"][n - 6:n]]})
    return series, {"months": labels[-6:], "rows": table}


def ipp_sa(raw):
    d = pd.read_excel(one(raw, "sezon_2023_*.xlsx"), sheet_name="1", header=None)
    out = {"months": [], "Промышленность": [], "Добыча": [], "Обработка": [], "Электроэнергия, газ, пар": []}
    year = None
    for _, r in d.iterrows():
        a = str(r[0]).strip()
        if re.fullmatch(r"(20\d\d)\d?(\.0)?", a):  # год, иногда со сноской: «20232» = 2023 и сноска 2
            year = int(a[:4])
            continue
        if year and a.lower() in MONTHS:
            out["months"].append(f"{year}-{MONTHS.index(a.lower()) + 1:02d}")
            # по 4 столбца на показатель: к пред. месяцу (факт, SA), к среднемесячному 2023 (факт, SA)
            for k, col in zip(list(out)[1:], (4, 8, 12, 16)):
                out[k].append(clean(pd.to_numeric(r[col], errors="coerce")))
    return out


def cbr_blocks(raw):
    f = one(raw, "mp_survey_data.xlsx")
    d = pd.read_excel(f, sheet_name="Экономика всего", header=None)
    hdr = d.iloc[3].tolist()
    sa0 = 15  # с 15-го столбца — сезонно скорректированные данные
    def col(name, start=sa0):
        return next(j for j in range(start, len(hdr)) if str(hdr[j]).startswith(name))
    cols = {"ИБК": col("Индикатор бизнес-климата Банка России"),
            "ИБК: текущие оценки": col("Индикатор бизнес-климата Банка России (факт)"),
            "ИБК: ожидания": col("Индикатор бизнес-климата Банка России (ожид)"),
            "Спрос: изменение": col("Как изменился спрос"),
            "Спрос: ожидания на 3 мес.": col("Как изменится в ближайшие три месяца спрос"),
            "Выпуск: изменение": col("Как изменился объем производства"),
            "Ценовые ожидания": col("Ценовые ожидания")}
    rows = d.iloc[4:]
    rows = rows[pd.to_datetime(rows[0], errors="coerce").notna()]
    rows = rows[pd.to_datetime(rows[0]) >= "2019-01-01"]
    out = {"months": [pd.Timestamp(x).strftime("%Y-%m") for x in rows[0]]}
    for k, j in cols.items():
        out[k] = [clean(pd.to_numeric(v, errors="coerce")) for v in rows[j]]
    # загрузка мощностей (квартал, SA)
    c = pd.read_excel(f, sheet_name="Загрузка произв мощностей", header=None)
    h = c.iloc[3].tolist()
    sa_start = next(j for j in range(1, len(h)) if j > 1 and str(h[j]).startswith("Экономика всего"))
    cu = {"quarters": [], "Экономика всего": [], "Промышленность": [], "Обработка": []}
    for _, r in c.iloc[4:].iterrows():
        m = re.fullmatch(r"(\d)к(\d\d)", str(r[0]).strip())
        if not m:
            continue
        y = 2000 + int(m.group(2))
        if y < 2015:
            continue
        cu["quarters"].append(f"{y} Q{m.group(1)}")
        for k, off in (("Экономика всего", 0), ("Промышленность", 1), ("Обработка", 3)):
            cu[k].append(clean(pd.to_numeric(r[sa_start + off], errors="coerce")))
    return out, cu


def minec(raw):
    files = sorted(glob.glob(os.path.join(raw, "minec_*.pdf")))
    if not files:
        return None
    import pymupdf
    t = re.sub(r"\s+", " ", " ".join(p.get_text() for p in pymupdf.open(files[-1])))
    m = re.search(r"в (\w+) (\d{4}) года рост ВВП составил ([+−-]?\d+,\d)% г/г после ([+−-]?\d+,\d)% г/г в (\w+)", t)
    if not m:
        return None
    num = lambda s: float(s.replace(",", ".").replace("−", "-"))
    nom = {"январе": "январь", "феврале": "февраль", "марте": "март", "апреле": "апрель", "мае": "май", "июне": "июнь",
           "июле": "июль", "августе": "август", "сентябре": "сентябрь", "октябре": "октябрь", "ноябре": "ноябрь",
           "декабре": "декабрь"}
    return {"month": f"{nom.get(m.group(1), m.group(1))} {m.group(2)}", "yoy": num(m.group(3)),
            "prev_month": nom.get(m.group(5), m.group(5)),
            "prev_yoy": num(m.group(4)), "file": os.path.basename(files[-1])}


def main():
    raw = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "..", "data", "gdp_activity.json")
    gdp = gdp_blocks(raw)
    ipp_series, ipp_table = ipp_yoy(raw)
    sa = ipp_sa(raw)
    cbr, cu = cbr_blocks(raw)
    mer = minec(raw)
    last_gdp = next(r for r in reversed(gdp) if r["yoy"] is not None)
    res = {
        "draft": True,
        "data_as_of": max(ipp_series["months"][-1], cbr["months"][-1]) + "-01",
        "generated": date.today().isoformat(),
        "kpi": {
            "gdp": {"label": last_gdp["q"], "yoy": last_gdp["yoy"], "saar": last_gdp["saar"]},
            "minec": mer,
            "ipp": {"label": ipp_series["months"][-1], "yoy": ipp_series["total"][-1],
                    "sa_mom": clean((sa["Промышленность"][-1] / sa["Промышленность"][-2] - 1) * 100)},
            "ibk": {"label": cbr["months"][-1], "v": cbr["ИБК"][-1], "prev": cbr["ИБК"][-2]},
        },
        "gdp_q": gdp,
        "gdp_use": use_block(raw),
        "vds": vds_block(raw),
        "ipp_yoy": ipp_series,
        "ipp_table": ipp_table,
        "ipp_sa": {k: (v[-48:]) for k, v in sa.items()},
        "cbr": cbr,
        "capacity": cu,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, allow_nan=False)
    print(f"{out_path}: ВВП по {last_gdp['q']}, ИПП по {ipp_series['months'][-1]}, мониторинг ЦБ по {cbr['months'][-1]}, "
          f"МЭР: {mer['month'] if mer else '—'}")


if __name__ == "__main__":
    main()
