"""Excel: изменение нетто-позиции населения по валютным фьючерсам Мосбиржи по месяцам, млрд $.

Данные — те же, что для графика на вкладке «Рубль» (update_futoi.py: FUTOI, физлица, Si/USDRUBF, Eu/EURRUBF,
CR/CNYRUBF; 1 контракт = 1000 ед. валюты). Изменение за месяц считается без переоценки: сумма по дням
(изменение позиции в контрактах × 1000 × курс ЦБ валюты на дату ÷ курс USD/RUB на дату). «+» — население
нарастило длинную позицию по валюте (купило валюту на срочном рынке), «−» — сократило.
Позиция на конец месяца — по курсам последнего дня месяца (включает переоценку).

Запуск: python data_scripts/futoi_monthly_excel.py
→ ~/Desktop/Совкомбанк/Рубль/Фьючерсы населения по месяцам.xlsx (другой путь — переменная FUTOI_EXCEL_OUT)
"""
import os
import sys
from datetime import date

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_futoi as uf  # noqa: E402

# Куда писать Excel: переменная FUTOI_EXCEL_OUT, по умолчанию — папка «Рубль» на Mac
OUT = os.environ.get("FUTOI_EXCEL_OUT") or os.path.expanduser("~/Desktop/Совкомбанк/Рубль/Фьючерсы населения по месяцам.xlsx")
CUR_NAMES = {"USD": "доллар (Si, USDRUBF)", "EUR": "евро (Eu, EURRUBF)", "CNY": "юань (CR, CNYRUBF)"}


def build():
    pos = uf.load_futoi()
    first = {t: pos[t].first_valid_index() for t in pos.columns}
    for t in pos.columns:
        pos.loc[first[t]:, t] = pos.loc[first[t]:, t].ffill()
    pos = pos.fillna(0)
    days = pos.index
    rates = uf.load_cbr_rates(days.min().date(), date.today())
    rates = rates.reindex(rates.index.union(days)).ffill().bfill().loc[days]
    usd = rates["USD"]

    flow, net = {}, {}
    for cur in ["USD", "EUR", "CNY"]:
        cols = [t for t in pos.columns if uf.TICKERS[t] == cur]
        contracts = pos[cols].sum(axis=1)
        flow[cur] = contracts.diff().fillna(0) * uf.LOT / 1e9 * rates[cur] / usd
        net[cur] = contracts * uf.LOT / 1e9 * rates[cur] / usd
    flow, net = pd.DataFrame(flow), pd.DataFrame(net)

    month = days.to_period("M")
    m_flow = flow.groupby(month).sum()
    m_flow.iloc[0] = float("nan")  # первый месяц ряда — без изменения (нет начальной позиции)
    m_net = net.groupby(month).last()
    last_day = pd.Series(days, index=days).groupby(month).last()

    out = pd.DataFrame({
        "Месяц": [p.to_timestamp() for p in m_flow.index],
        "Изменение нетто-позиции, всего": m_flow.sum(axis=1, min_count=1).values,
        **{f"в т.ч. {CUR_NAMES[c]}": m_flow[c].values for c in ["USD", "CNY", "EUR"]},
        "Нетто-позиция на конец месяца": m_net.sum(axis=1).values,
        "Изменение позиции на конец месяца (с переоценкой)": m_net.sum(axis=1).diff().values,
        "Последний день данных": last_day.values,
    })
    return out, days.max()


def write(df, last):
    with pd.ExcelWriter(OUT, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="По месяцам", index=False, startrow=3)
        ws = xw.sheets["По месяцам"]
        ws["A1"] = "Изменение нетто-позиции населения по валютным фьючерсам Мосбиржи, млрд $"
        ws["A1"].font = Font(bold=True, size=13)
        ws["A2"] = (f"Мосбиржа FUTOI (физлица) + курсы ЦБ; данные по {last:%d.%m.%Y}, текущий месяц неполный. "
                    "Изменение — без переоценки (контракты × курс на дату); «+» — население нарастило покупки валюты.")
        ws["A2"].font = Font(italic=True, color="666666")
        hdr = ws[4]
        for c in hdr:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="3F6DB3")
            c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.row_dimensions[4].height = 45
        widths = [11, 16, 16, 16, 16, 16, 18, 14]
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for row in ws.iter_rows(min_row=5, max_row=ws.max_row):
            row[0].number_format = "mmm yyyy"
            row[7].number_format = "dd.mm.yyyy"
            for c in row[1:7]:
                c.number_format = '+0.00;-0.00;0.00'
            row[1].font = Font(bold=True)
            row[5].number_format = "0.00"
        ws.freeze_panes = "B5"

        # годовые суммы
        y = df.assign(Год=df["Месяц"].dt.year).groupby("Год")[df.columns[1:5]].sum(min_count=1).round(2)
        y.to_excel(xw, sheet_name="По годам")
        wy = xw.sheets["По годам"]
        for c in wy[1]:
            c.font = Font(bold=True)
            c.alignment = Alignment(wrap_text=True)
        for i in range(1, 6):
            wy.column_dimensions[get_column_letter(i)].width = 18
    print(f"{OUT}: {len(df)} мес., по {last:%d.%m.%Y}")


if __name__ == "__main__":
    df, last = build()
    write(df, last)
    print(df.tail(8).round(2).to_string(index=False))
