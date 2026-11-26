"""FRMAX (максимальная ставка по вкладам топ-10 банков, ЦБ, по декадам) → лист «Ставка по депозитам» книги
yield_forecast_data.xlsx (26.11.2026). Книга ведётся вручную; этот скрипт только дописывает новые декады сверху
(лист отсортирован от новых к старым) и сверяет перекрытие с тем, что уже лежит в книге.

    python data_scripts/update_frmax.py            # дописать новые декады
    python data_scripts/update_frmax.py --check    # только показать, что дописалось бы
На сайт — ./publish.sh yield.

Источник — таблица на cbr.ru/statistics/avgprocstav/ (UniDbQuery.From/To), один запрос, системный curl (российский УЦ).
"""
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

import openpyxl

SITE = Path(__file__).resolve().parent.parent
BOOK = SITE / "yield_forecast_data.xlsx"
SHEET = "Ставка по депозитам"
URL = "https://www.cbr.ru/statistics/avgprocstav/?UniDbQuery.Posted=True&UniDbQuery.From={f}&UniDbQuery.To={t}"


def decade_key(label):
    m = re.fullmatch(r"(I{1,3})\.(\d{2})\.(\d{4})", str(label or "").strip())
    return (int(m[3]), int(m[2]), len(m[1])) if m else None


def fetch(since: dt.date):
    url = URL.format(f=since.strftime("%d.%m.%Y"), t=dt.date.today().strftime("%d.%m.%Y"))
    html = subprocess.run(["/usr/bin/curl", "-sS", "-f", "-A", "Mozilla/5.0", url],
                          capture_output=True, check=True).stdout.decode("utf-8", "ignore")
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>|\s+", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) == 2 and decade_key(cells[0]):
            out[cells[0]] = float(cells[1].replace(",", "."))
    if not out:
        raise SystemExit("таблица FRMAX на cbr.ru не разобралась — книга не тронута")
    return out


def main():
    check = "--check" in sys.argv
    wb = openpyxl.load_workbook(BOOK)
    ws = wb[SHEET]
    have = {}
    for r in range(2, ws.max_row + 1):
        k = decade_key(ws.cell(r, 1).value)
        if k:
            have[ws.cell(r, 1).value.strip()] = ws.cell(r, 2).value
    last = max(have, key=decade_key)
    y, m, _ = decade_key(last)
    cbr = fetch(dt.date(y, m, 1) - dt.timedelta(days=60))
    mism = [(k, have[k], v) for k, v in cbr.items() if k in have and abs(float(have[k]) - v) > 0.0005]
    for k, a, b in mism:
        print(f"⚠ {k}: в книге {a}, у ЦБ {b} (книгу не меняю — проверьте вручную)")
    new = sorted((k for k in cbr if decade_key(k) > decade_key(last)), key=decade_key, reverse=True)
    if not new:
        print(f"Новых декад нет (последняя в книге — {last})")
        return
    print(f"Новые декады: {', '.join(f'{k} {cbr[k]}' for k in reversed(new))}")
    if check:
        return
    ws.insert_rows(2, amount=len(new))
    for i, k in enumerate(new):
        ws.cell(2 + i, 1, k)
        ws.cell(2 + i, 2, cbr[k])
    wb.save(BOOK)
    print(f"✓ {BOOK.name}: дописано {len(new)}, последняя декада — {new[0]}")


if __name__ == "__main__":
    main()
