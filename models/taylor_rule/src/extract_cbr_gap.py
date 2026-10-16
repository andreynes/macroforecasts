"""Разрыв выпуска Банка России из «Основных направлений ДКП на 2026–2028 гг.» (рис. 10.8, с. 94 PDF).

На графике нет числовой шкалы, но он векторный. Порядок:
  1. из PDF достаются линии: «ВВП с.к. в ценах 2021 г.», «Потенциал» (верхняя панель) и «Разрыв выпуска» (средняя);
  2. шкала верхней панели калибруется по фактическому ВВП Росстата (с.к., в ценах 2021 г.): пиксели → млрд руб.;
  3. разрыв = 100·ln(ВВП / потенциал) по верхней панели; средняя панель масштабируется под него (проверка формы).
Кварталы: 2003Q2–2025Q2 (89 точек; сдвиг определён по наилучшему совпадению с ВВП Росстата).

Запуск: python src/extract_cbr_gap.py → data/raw/cbr_output_gap.csv
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import requests

from taylor_rule import RAW, load_gdp_sa

PDF_URL = "https://www.cbr.ru/Content/Document/File/180751/on_2026(2027-2028).pdf"
PAGE = 93                         # с. 94 PDF, рис. 10.8
FIRST_Q = pd.Period("2003Q2", "Q")
BLUE, DARK = (0.0, 0.73, 0.93), (0.14, 0.12, 0.12)


def _polylines(page) -> list[dict]:
    out = []
    for d in page.get_drawings():
        pts = []
        for it in d["items"]:
            if it[0] == "l":
                pts += [it[1], it[2]]
            elif it[0] == "c":
                pts += [it[1], it[4]]
        xy = []
        for p in pts:
            if not xy or abs(xy[-1][0] - p.x) > 1e-3 or abs(xy[-1][1] - p.y) > 1e-3:
                xy.append((p.x, p.y))
        color = tuple(round(c, 2) for c in (d.get("color") or (0, 0, 0)))
        if len(xy) > 50:
            out.append({"color": color, "xy": np.array(xy), "y0": d["rect"].y0})
    return sorted(out, key=lambda p: p["y0"])


def main() -> None:
    import pymupdf

    path = RAW / "ondkp_2026_2028.pdf"
    if not path.exists():
        path.write_bytes(requests.get(PDF_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=300).content)
    page = pymupdf.open(path)[PAGE]
    lines = _polylines(page)
    gdp_px = next(p for p in lines if p["color"] == BLUE)["xy"]                         # верхняя панель
    pot_px = next(p for p in lines if p["color"] == DARK and p["y0"] < 300)["xy"]
    gap_px = [p for p in lines if p["color"] == BLUE][1]["xy"]                          # средняя панель
    zero_y = min(it[1].y for d in page.get_drawings() for it in d["items"]
                 if it[0] == "l" and abs(it[1].y - it[2].y) < 0.01 and abs(it[2].x - it[1].x) > 300
                 and 350 < it[1].y < 480)

    q = pd.period_range(FIRST_Q, periods=len(gdp_px), freq="Q")
    gdp = load_gdp_sa()
    common = q.intersection(gdp.index)
    b, a = np.polyfit(gdp[common].values, pd.Series(gdp_px[:, 1], index=q)[common].values, 1)
    level = lambda px: (px - a) / b
    gap_top = 100 * np.log(level(gdp_px[:, 1]) / level(pot_px[:, 1]))
    k = np.polyfit(zero_y - gap_px[:, 1], gap_top, 1)
    gap_mid = np.polyval(k, zero_y - gap_px[:, 1])
    out = pd.DataFrame({"quarter": q.astype(str), "gap": gap_mid, "gap_top_panel": gap_top,
                        "gdp_chart": level(gdp_px[:, 1]), "potential_chart": level(pot_px[:, 1])})
    out.round(4).to_csv(RAW / "cbr_output_gap.csv", index=False)
    print(f"Разрыв выпуска ЦБ: {q[0]}–{q[-1]}, корреляция панелей {np.corrcoef(gap_top, gap_mid)[0, 1]:.4f}, "
          f"последнее значение {gap_mid[-1]:.2f}%")


if __name__ == "__main__":
    main()
