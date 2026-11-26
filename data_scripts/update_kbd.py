"""Кривая бескупонной доходности ОФЗ (КБД, Мосбиржа) → лист «Доходности КБД» книги yield_forecast_data.xlsx (26.11.2026).
Книга ведётся вручную; скрипт дописывает завершённые торговые дни после последней даты в книге (сегодняшний день не берёт:
кривая за него ещё внутридневная). Последнюю строку книги, если она внутридневная (время раньше 18:00), заменяет итогом дня.

    python data_scripts/update_kbd.py            # дописать
    python data_scripts/update_kbd.py --check    # только показать, что дописалось бы
На сайт — ./publish.sh yield.

Источник — iss.moex.com/iss/engines/stock/zcyc.json?date=… (блок yearyields), один запрос на день, пауза PAUSE с; системный
curl. Сроки книги: 1, 2, 3, 5, 7, 10 лет — как у Мосбиржи; 4 года — среднее 3 и 5 лет (так заполнена книга: проверено
на 08.09.2026 до последнего знака).
"""
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

import openpyxl

SITE = Path(__file__).resolve().parent.parent
BOOK = SITE / "yield_forecast_data.xlsx"
SHEET = "Доходности КБД"
URL = "https://iss.moex.com/iss/engines/stock/zcyc.json?date={d}&iss.meta=off&iss.only=yearyields"
PERIODS = [1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0]
PAUSE = 1.0


def fetch_day(d: dt.date):
    out = subprocess.run(["/usr/bin/curl", "-sS", "-f", "-m", "30", "-A", "Mozilla/5.0", URL.format(d=d.isoformat())],
                         capture_output=True, check=True).stdout
    data = json.loads(out)["yearyields"]["data"]
    if not data or data[0][0] != d.isoformat():
        return None                                  # нерабочий день: Мосбиржа отдаёт ближайший прошлый
    pts = {float(p): v for _, _, p, v in data}
    tt = data[0][1]
    if not all(p in pts for p in (1.0, 2.0, 3.0, 5.0, 7.0, 10.0)):
        return None
    pts[4.0] = (pts[3.0] + pts[5.0]) / 2
    return tt, [round(pts[p], 6) for p in PERIODS]


def main():
    check = "--check" in sys.argv
    wb = openpyxl.load_workbook(BOOK)
    ws = wb[SHEET]
    hdr = [c.value for c in ws[1]]
    assert hdr[:2] == ["tradedate", "tradetime"] and hdr[2:9] == [f"period_{p}" for p in PERIODS], hdr
    last_r = ws.max_row
    while last_r > 1 and ws.cell(last_r, 1).value is None:
        last_r -= 1
    last_d = ws.cell(last_r, 1).value
    last_d = last_d.date() if isinstance(last_d, dt.datetime) else last_d
    redo_last = str(ws.cell(last_r, 2).value or "00")[:2] < "18"      # итог дня у Мосбиржи — 18:49…18:55
    start = last_d if redo_last else last_d + dt.timedelta(days=1)
    today = dt.date.today()
    rows, d = [], start
    while d < today:
        if d.weekday() < 5:
            r = fetch_day(d)
            if r:
                rows.append((d, *r))
            time.sleep(PAUSE)
        d += dt.timedelta(days=1)
    if not rows:
        print(f"Новых дней нет (последний в книге — {last_d})")
        return
    print(f"Дней: {len(rows)}, {rows[0][0]} … {rows[-1][0]}; последний: {rows[-1][2]}"
          + (f" (строка {last_d} была внутридневной — заменяется)" if redo_last and rows[0][0] == last_d else ""))
    if check:
        return
    r0 = last_r if (redo_last and rows[0][0] == last_d) else last_r + 1
    for i, (d, tt, vals) in enumerate(rows):
        ws.cell(r0 + i, 1, dt.datetime(d.year, d.month, d.day))
        ws.cell(r0 + i, 2, tt)
        for j, v in enumerate(vals):
            ws.cell(r0 + i, 3 + j, v)
        ws.cell(r0 + i, 1).number_format = ws.cell(last_r, 1).number_format
    wb.save(BOOK)
    print(f"✓ {BOOK.name}: записано {len(rows)} дн., последний — {rows[-1][0]}")


if __name__ == "__main__":
    main()
