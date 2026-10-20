"""Прогнозы аналитиков по кварталам — вытаскиваются из рабочей гугл-таблицы
(её периодически выгружают в analyst_forecasts.xlsx, по одной вкладке на
ежемесячный снимок, вкладка названа датой снимка d.m.yy).

Внутри снимка есть несколько блоков-табличек, каждый озаглавлен одним из
"якорных" слов (имя менялось со временем): "Ставка"/"КС" — ключевая ставка,
"Рубль" — USD/RUB, "Нефть" — Brent, "Инфляция", "ВВП". В строке блока (или в
строке прямо над ней) — заголовки кварталов вида "4к26" (либо те же кварталы
как даты конца периода), ниже — построчно аналитики (Миша/Маша/Никита/
Андрей/Миша Ник/Егор) и их прогноз на конец каждого квартала, завершается
строкой "Консенсус х5"/"Консенсус х3" (среднее по аналитикам) — эта строка
всегда последняя в блоке, на ней парсинг обязательно останавливается: ниже
начинается уже следующий показатель, который переиспользует те же колонки
под свои значения.

Якорное слово может случайно встретиться где-то ещё в вольных текстовых
комментариях соседних ячеек — поэтому якорь засчитывается только в той
колонке, которую подтверждает колонка "Ставка"/"КС" этого же снимка (они
всегда идут парой на расстоянии 11 колонок друг от друга в актуальном
формате; в более старых снимках — одна колонка без пары).

Инфляция и ВВП в этой таблице есть только годовые (конец 4 квартала) — у них
нет своего заголовка с кварталами, поэтому для них используется фиксированное
смещение от якорной колонки (+5 — конец текущего года, +10 — конец
следующего), с календарным годом, взятым из даты снимка."""
import calendar
import os
import re
from datetime import date
from typing import Dict, List, Optional, Tuple

import openpyxl

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANALYST_FILE = os.getenv("ANALYST_FORECASTS_FILE", os.path.join(BASE_DIR, "analyst_forecasts.xlsx"))

_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$")
_Q_RE = re.compile(r"^(\d)к(\d{2})$")

_RATE_LABELS = {"Ставка", "КС"}
_ANCHOR_TO_INDICATOR = {
    "Ставка": "key_rate", "КС": "key_rate",
    "Рубль": "usdrub", "Нефть": "brent",
    "Инфляция": "inflation", "ВВП": "gdp",
}
_ANALYSTS = {"Миша", "Маша", "Никита", "Андрей", "Егор", "Миша Ник"}
_CONSENSUS = {"Консенсус х5", "Консенсус х3", "Консенсус"}

INDICATORS = [
    {"key": "key_rate", "label": "Ключевая ставка", "annual_only": False},
    {"key": "usdrub", "label": "USD/RUB", "annual_only": False},
    {"key": "brent", "label": "Brent", "annual_only": False},
    {"key": "inflation", "label": "Инфляция", "annual_only": True},
    {"key": "gdp", "label": "ВВП", "annual_only": True},
]

_CACHE: Dict[str, object] = {"mtime": None, "records": None}

_Record = Tuple[date, str, str, str, float]  # snapshot, indicator, who, quarter, value


def _parse_snapshot_date(sheet_name: str) -> Optional[date]:
    m = _DATE_RE.match(sheet_name.strip())
    if not m:
        return None
    d, mo, y = m.groups()
    y = int(y)
    if y < 100:
        y += 2000
    try:
        return date(y, int(mo), int(d))
    except ValueError:
        return None


def quarter_sort_key(label: str) -> Tuple[int, int]:
    m = _Q_RE.match(label.strip())
    q, y = m.groups()
    return (2000 + int(y), int(q))


def quarter_end_date(label: str) -> date:
    year, q = quarter_sort_key(label)
    month = q * 3
    day = 31 if month in (3, 12) else 30
    return date(year, month, day)


def _q_label_from_date_cell(v) -> Optional[str]:
    if not hasattr(v, "month") or not hasattr(v, "day"):
        return None
    if v.month not in (3, 6, 9, 12):
        return None
    if v.day != calendar.monthrange(v.year, v.month)[1]:
        return None
    return f"{v.month // 3}к{v.year % 100:02d}"


def _row_quarter_cols(ws, row_idx: int) -> Dict[int, str]:
    """Колонка -> метка квартала для строки-заголовка. В нескольких переходных
    снимках (июнь-июль 2025) один и тот же квартал встречается в строке ДВАЖДЫ
    — старый "замороженный" прогноз слева и текущий, обновляемый каждый месяц,
    справа (правый блок всегда содержит прогноз по всем кварталам, включая уже
    прошедшие — признак "актуальной" версии). Чтобы в ряд не попадали сразу два
    значения за одну и ту же дату снимка, при совпадении метки оставляем только
    самую правую (последнюю при обходе слева направо) колонку."""
    label_to_col: Dict[str, int] = {}
    if row_idx < 1:
        return {}
    for cell in ws[row_idx]:
        v = cell.value
        label = None
        if isinstance(v, str):
            m = _Q_RE.match(v.strip())
            if m:
                label = v.strip()
        else:
            label = _q_label_from_date_cell(v)
        if label:
            label_to_col[label] = cell.column
    return {col: label for label, col in label_to_col.items()}


def _find_ref_col(ws) -> Tuple[Optional[int], Optional[int]]:
    cols = set()
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.strip() in _RATE_LABELS:
                cols.add(cell.column)
    for c in sorted(cols):
        if c + 11 in cols:
            return c, 11
    if cols:
        return min(cols), None
    return None, None


def _extract_sheet(ws, snapshot: date) -> List[_Record]:
    ref_col, pair_offset = _find_ref_col(ws)
    if ref_col is None:
        return []
    valid_cols = {ref_col} | ({ref_col + pair_offset} if pair_offset else set())
    this_year, next_year = snapshot.year, snapshot.year + 1

    out: List[_Record] = []
    for label, indicator in _ANCHOR_TO_INDICATOR.items():
        anchor_rows = sorted({
            c.row for row in ws.iter_rows() for c in row
            if isinstance(c.value, str) and c.value.strip() == label and c.column in valid_cols
        })
        for ar in anchor_rows:
            qcols = _row_quarter_cols(ws, ar) or _row_quarter_cols(ws, ar - 1)
            if not qcols:
                # Инфляция/ВВП — только годовые значения, без собственного
                # заголовка с кварталами: фиксированное смещение от якоря.
                qcols = {}
                for c in valid_cols:
                    if c + 5 <= ws.max_column:
                        qcols[c + 5] = f"4к{this_year % 100:02d}"
                    if c + 10 <= ws.max_column:
                        qcols[c + 10] = f"4к{next_year % 100:02d}"

            model_vals: Dict[str, float] = {}
            analysts_in_block: set = set()
            block_out: List[_Record] = []

            # "Модель" иногда сидит прямо в строке якоря (та же строка, где
            # написано "Рубль"/"Нефть"/...), а не отдельной строкой ниже —
            # проверяем её тоже, иначе бэкфилл по модели никогда не сработает.
            if any(isinstance(c.value, str) and c.value.strip() == "Модель" for c in ws[ar]):
                for col, qlabel in qcols.items():
                    v = ws.cell(row=ar, column=col).value
                    if isinstance(v, (int, float)):
                        model_vals[qlabel] = round(float(v), 3)

            r = ar + 1
            blank_streak = 0
            while blank_streak < 3 and r < ar + 20:
                who = None
                is_model = False
                for cell in ws[r]:
                    if isinstance(cell.value, str):
                        vv = cell.value.strip()
                        if vv in _ANALYSTS or vv in _CONSENSUS:
                            who = vv
                            break
                        if vv == "Модель":
                            is_model = True
                            break
                if who is None and not is_model:
                    blank_streak += 1
                    r += 1
                    continue
                blank_streak = 0
                if is_model:
                    for col, qlabel in qcols.items():
                        v = ws.cell(row=r, column=col).value
                        if isinstance(v, (int, float)):
                            model_vals[qlabel] = round(float(v), 3)
                    r += 1
                    continue
                if who in _ANALYSTS:
                    analysts_in_block.add(who)
                for col, qlabel in qcols.items():
                    v = ws.cell(row=r, column=col).value
                    if isinstance(v, (int, float)):
                        block_out.append((snapshot, indicator, who, qlabel, round(float(v), 3)))
                r += 1
                if who in _CONSENSUS:
                    break

            # Если аналитик не вписал своё число на этот квартал (ячейка
            # пустая), а строка "Модель" для этого квартала заполнена —
            # значит, никто не спорил с модельной оценкой; считаем, что
            # аналитик с ней согласен, и используем её как его значение.
            covered = {(who, qlabel) for _, _, who, qlabel, _ in block_out}
            for who in analysts_in_block:
                for qlabel, v in model_vals.items():
                    if (who, qlabel) not in covered:
                        block_out.append((snapshot, indicator, who, qlabel, v))

            out.extend(block_out)

    # Иногда в одном снимке одно и то же имя встречается дважды в пределах
    # блока одного показателя (например, в блоке по нефти два разных человека
    # исторически подписывались одинаково как "Миша") — это создаёт вторую,
    # несвязанную точку на ту же дату и ломает линию "зигзагом". Раз различить
    # их по одному только имени нельзя, оставляем последнее (самое нижнее по
    # листу) значение как компромисс — тот же принцип, что и для дублирующихся
    # колонок кварталов в _row_quarter_cols.
    dedup: Dict[Tuple[str, str, str], float] = {}
    for _, ind, who, qlabel, v in out:
        dedup[(ind, who, qlabel)] = v
    return [
        (snapshot, ind, who, qlabel, v)
        for (ind, who, qlabel), v in dedup.items()
    ]


# Показатель в самой свежей вкладке сверху листа (над блоками по аналитикам)
# всегда даёт факт/прогноз по кварталам одной строкой, с явным тегом в
# строке 2 ("Факт"/"Прогноз"/"Среднее значение"). Пока используется только
# для Brent — у него больше нет другого источника факта на сайте.
_TOP_BLOCK_LABELS = {"brent": "Brent"}
_TOP_CACHE: Dict[str, object] = {"mtime": None, "facts": None}


def _top_block_facts() -> Dict[str, Dict[str, float]]:
    mtime = os.path.getmtime(ANALYST_FILE)
    if _TOP_CACHE["mtime"] == mtime and _TOP_CACHE["facts"] is not None:
        return _TOP_CACHE["facts"]

    wb = openpyxl.load_workbook(ANALYST_FILE, data_only=True)
    latest_name, latest_date = None, None
    for sheet_name in wb.sheetnames:
        d = _parse_snapshot_date(sheet_name)
        if d and (latest_date is None or d > latest_date):
            latest_name, latest_date = sheet_name, d

    facts: Dict[str, Dict[str, float]] = {k: {} for k in _TOP_BLOCK_LABELS}
    if latest_name:
        ws = wb[latest_name]
        rows_by_indicator: Dict[str, int] = {}
        for r in range(1, 20):
            label = ws.cell(row=r, column=1).value
            if not isinstance(label, str):
                continue
            for key, needle in _TOP_BLOCK_LABELS.items():
                if needle in label and key not in rows_by_indicator:
                    rows_by_indicator[key] = r

        qcols: Dict[int, str] = {}
        for cell in ws[1]:
            if isinstance(cell.value, str):
                m = _Q_RE.match(cell.value.strip())
                if m:
                    qcols[cell.column] = cell.value.strip()

        for key, row_idx in rows_by_indicator.items():
            for col, qlabel in qcols.items():
                if ws.cell(row=2, column=col).value != "Факт":
                    continue
                v = ws.cell(row=row_idx, column=col).value
                if isinstance(v, (int, float)):
                    facts[key][qlabel] = round(float(v), 3)

    _TOP_CACHE["mtime"] = mtime
    _TOP_CACHE["facts"] = facts
    return facts


def top_block_fact(indicator: str, quarter_label: str) -> Optional[float]:
    return _top_block_facts().get(indicator, {}).get(quarter_label.strip())


_RU_MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}
_RECENT_MONTHS_LABELS = {"brent": "Brent", "usdrub": "USDRUB"}
_RECENT_CACHE: Dict[str, object] = {"mtime": None, "data": None}


def _infer_month_year(month_num: int, ref_date: date) -> int:
    """Снимок содержит только названия месяцев без года — берём ближайший
    месяц этого номера НЕ позже даты снимка (т.е. прошлый декабрь для
    снимка в январе и т.п.)."""
    return ref_date.year if month_num <= ref_date.month else ref_date.year - 1


def _recent_months_actuals() -> Dict[str, Dict[Tuple[int, int], float]]:
    """В каждом снимке, помимо блоков по аналитикам, есть отдельная мини-
    таблица с фактическими среднемесячными значениями за последние ~3
    месяца (обновляется вручную, с нерегулярной задержкой — иногда текущий
    месяц уже проставлен, иногда ещё нет). Берём её из САМОГО СВЕЖЕГО снимка
    — это единственный источник хоть какого-то факта по Brent на сайте."""
    mtime = os.path.getmtime(ANALYST_FILE)
    if _RECENT_CACHE["mtime"] == mtime and _RECENT_CACHE["data"] is not None:
        return _RECENT_CACHE["data"]

    wb = openpyxl.load_workbook(ANALYST_FILE, data_only=True)
    latest_name, latest_date = None, None
    for sheet_name in wb.sheetnames:
        d = _parse_snapshot_date(sheet_name)
        if d and (latest_date is None or d > latest_date):
            latest_name, latest_date = sheet_name, d

    out: Dict[str, Dict[Tuple[int, int], float]] = {k: {} for k in _RECENT_MONTHS_LABELS}
    if latest_name:
        ws = wb[latest_name]
        header = None
        for r in range(1, 40):
            run: List[Tuple[int, str]] = []
            for c in range(1, ws.max_column + 1):
                v = ws.cell(row=r, column=c).value
                if isinstance(v, str) and v.strip().lower() in _RU_MONTHS:
                    run.append((c, v.strip().lower()))
                    if len(run) == 3:
                        header = (r, run)
                        break
                else:
                    run = []
            if header:
                break

        if header:
            hrow, month_cells = header
            label_col = month_cells[0][0] - 1
            month_year = [
                (c, _RU_MONTHS[name], _infer_month_year(_RU_MONTHS[name], latest_date))
                for c, name in month_cells
            ]
            for r in range(hrow + 1, hrow + 10):
                label = ws.cell(row=r, column=label_col).value
                if not isinstance(label, str):
                    continue
                for key, needle in _RECENT_MONTHS_LABELS.items():
                    if needle in label:
                        for col, m, y in month_year:
                            v = ws.cell(row=r, column=col).value
                            if isinstance(v, (int, float)):
                                out[key][(y, m)] = round(float(v), 3)

    _RECENT_CACHE["mtime"] = mtime
    _RECENT_CACHE["data"] = out
    return out


def recent_actual_quarter_avg(indicator: str, quarter_label: str) -> Optional[float]:
    """Среднее по тем месяцам целевого квартала, которые уже попали в
    таблицу последних фактических месяцев (см. _recent_months_actuals).
    Если нет ни одного — вернёт None (значит, по этому кварталу пока вообще
    нет фактических данных ни на сайте, ни в этом снимке)."""
    year, q = quarter_sort_key(quarter_label)
    months = {(year, q * 3 - 2), (year, q * 3 - 1), (year, q * 3)}
    data = _recent_months_actuals().get(indicator, {})
    vals = [data[m] for m in months if m in data]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 3)


def _load_records() -> List[_Record]:
    mtime = os.path.getmtime(ANALYST_FILE)
    if _CACHE["mtime"] == mtime and _CACHE["records"] is not None:
        return _CACHE["records"]

    wb = openpyxl.load_workbook(ANALYST_FILE, data_only=True)
    records: List[_Record] = []
    for sheet_name in wb.sheetnames:
        snapshot = _parse_snapshot_date(sheet_name)
        if not snapshot:
            continue
        records.extend(_extract_sheet(wb[sheet_name], snapshot))

    _CACHE["mtime"] = mtime
    _CACHE["records"] = records
    return records


def available_quarters(indicator: str = "key_rate") -> List[str]:
    records = _load_records()
    quarters = sorted(
        {q for _, ind, _, q, _ in records if ind == indicator},
        key=quarter_sort_key,
    )
    return quarters


def current_quarter_label(today: Optional[date] = None) -> str:
    today = today or date.today()
    q = (today.month - 1) // 3 + 1
    return f"{q}к{today.year % 100:02d}"


def quarter_series(indicator: str, quarter_label: str) -> Dict:
    records = _load_records()
    quarter_label = quarter_label.strip()
    q_end = quarter_end_date(quarter_label)
    by_who: Dict[str, List[Tuple[date, float]]] = {}
    for snapshot, ind, who, q, value in records:
        if ind != indicator or q != quarter_label:
            continue
        # Снимки после окончания квартала по-прежнему содержат его колонку
        # (прошедшие кварталы не удаляют) — но это уже не прогноз, а
        # перенесённое значение; все линии обрываем на конце квартала.
        if snapshot > q_end:
            continue
        by_who.setdefault(who, []).append((snapshot, value))
    for who in by_who:
        by_who[who].sort(key=lambda t: t[0])

    analysts = sorted(a for a in by_who if a in _ANALYSTS)
    series = {
        who: [{"date": d.isoformat(), "value": v} for d, v in by_who[who]]
        for who in analysts
    }

    # Разные снимки используют то "Консенсус х3", то "Консенсус х5" (менялось
    # со временем) — склеиваем их в один непрерывный ряд, предпочитая х5 там,
    # где за одну и ту же дату снимка есть оба варианта.
    consensus_by_date: Dict[date, float] = {}
    for who in ("Консенсус х3", "Консенсус", "Консенсус х5"):
        for d, v in by_who.get(who, []):
            consensus_by_date[d] = v
    consensus_series = [
        {"date": d.isoformat(), "value": v}
        for d, v in sorted(consensus_by_date.items())
    ]
    consensus_name = "Консенсус" if consensus_series else None

    return {
        "indicator": indicator,
        "quarter": quarter_label,
        "quarter_end": q_end.isoformat(),
        "analysts": analysts,
        "series": series,
        "consensus_name": consensus_name,
        "consensus_series": consensus_series,
    }
