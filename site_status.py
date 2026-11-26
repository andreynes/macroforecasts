"""Страница статуса портала /status: свежесть разделов данных (manifest.json бакета) и состояние машины macro-calc.

Только чтение. В облаке бакет macro-site-data смонтирован в контейнер целиком (DATA_DIR=/mnt/site-data/data),
поэтому parallel/ и backup/ читаются как папки рядом с data/. Локально бакета нет: манифест берётся из data/
(если есть), блок машины пуст; для проверки можно задать STATUS_BUCKET_ROOT — папку с копией бакета.
Результат кешируется на 5 минут, чтобы листинг смонтированного бакета не замедлял сайт.
"""
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

MSK = timezone(timedelta(hours=3))
CACHE_SECONDS = 300

SECTION_LABELS = {
    "inflation_weekly": "Недельная инфляция",
    "inflation_monthly": "Месячная инфляция",
    "moex": "Мосбиржа (FUTOI, юань)",
    "ruble": "Рубль",
    "ngd": "НГД",
    "macro": "Макропрогнозы",
    "cbr_models": "Модели ЦБ",
    "analysts": "Прогнозы аналитиков",
    "yield": "Доходности (ОФЗ, ставки)",
}

# Ожидаемая частота. Расписания (kind != age): светофор по последнему сроку — зелёный, если опубликовано после
# последнего срока (срок + grace уже прошёл), жёлтый — пропущен один срок, красный — два и больше.
# Разделы без расписания (публикуются вручную): по давности публикации — до green_days зелёный, до red_days жёлтый.
SCHEDULES: Dict[str, Dict] = {
    "inflation_weekly": {"kind": "weekly", "weekday": 2, "hm": (19, 0), "grace_h": 3,
                         "label": "ср 19:00 (повторы чт, пт)"},
    "inflation_monthly": {"kind": "age", "green_days": 35, "red_days": 50,
                          "label": "сама: релиз Росстата (ср/пт, 5–28 число) и бюллетень ЦБ (Mac)"},
    "moex": {"kind": "workdays", "hm": (19, 30), "grace_h": 2, "label": "пн–пт 19:30"},
    # Ниже — предложение (расписаний нет, публикуются вручную с Mac)
    "ruble": {"kind": "age", "green_days": 7, "red_days": 14, "label": "вручную, ~раз в неделю"},
    "ngd": {"kind": "age", "green_days": 7, "red_days": 14, "label": "вручную, ~раз в неделю"},
    "macro": {"kind": "age", "green_days": 35, "red_days": 62, "label": "вручную, ~раз в месяц"},
    "cbr_models": {"kind": "age", "green_days": 35, "red_days": 62, "label": "после «Макропрогнозов»"},
    "analysts": {"kind": "age", "green_days": 35, "red_days": 62, "label": "вручную, ~раз в месяц"},
    "yield": {"kind": "age", "green_days": 35, "red_days": 62, "label": "вручную, ~раз в месяц"},
    "credit": {"kind": "age", "green_days": 35, "red_days": 62, "label": "машина, по выходу данных ЦБ (~20-го)"},
}

# Кто публикует, если в manifest.json нет поля published_by (записи до 20.11.2026)
DEFAULT_PUBLISHER = {"moex": "macro-calc"}

STATUS_TEXT = {"green": "в срок", "yellow": "просрочен", "red": "давно нет", "grey": "нет данных"}

_cache: Dict[str, Tuple[float, Dict]] = {}


def _parse_ts(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(MSK)
    except (TypeError, ValueError):
        return None


def _fmt(dt: Optional[datetime]) -> str:
    return dt.strftime("%d.%m.%Y %H:%M") if dt else "—"


def _age(delta: timedelta) -> str:
    h = delta.total_seconds() / 3600
    if h < 1:
        return f"{max(int(h * 60), 0)} мин"
    if h < 48:
        return f"{int(h)} ч"
    return f"{int(h // 24)} дн."


def _last_slots(now: datetime, sch: Dict, n: int = 2) -> List[datetime]:
    """Последние n сроков публикации, у которых уже прошёл запас grace_h."""
    out = []
    grace = timedelta(hours=sch["grace_h"])
    d = now.date()
    for _ in range(120):
        if ((sch["kind"] == "weekly" and d.weekday() == sch["weekday"])
                or (sch["kind"] == "workdays" and d.weekday() < 5)
                or (sch["kind"] == "monthly" and d.day == sch["day"])):
            slot = datetime(d.year, d.month, d.day, *sch["hm"], tzinfo=MSK)
            if slot + grace <= now:
                out.append(slot)
                if len(out) == n:
                    break
        d -= timedelta(days=1)
    return out


def _light(section: str, published: Optional[datetime], now: datetime) -> Tuple[str, str]:
    """(цвет, пояснение)."""
    if published is None:
        return "grey", "нет публикаций"
    sch = SCHEDULES.get(section, {"kind": "age", "green_days": 30, "red_days": 60})
    if sch["kind"] == "age":
        days = (now - published).total_seconds() / 86400
        if days <= sch["green_days"]:
            return "green", f"не старше {sch['green_days']} дн."
        if days <= sch["red_days"]:
            return "yellow", f"старше {sch['green_days']} дн."
        return "red", f"старше {sch['red_days']} дн."
    slots = _last_slots(now, sch)
    if not slots or published >= slots[0]:
        return "green", f"срок {_fmt(slots[0]) if slots else '—'} выполнен"
    if len(slots) > 1 and published >= slots[1]:
        return "yellow", f"пропущен срок {_fmt(slots[0])}"
    return "red", "пропущено 2 срока и больше"


def _sections(manifest: Optional[Dict], now: datetime) -> List[Dict]:
    files = (manifest or {}).get("files") or {}
    by_sec: Dict[str, List[Tuple[str, Dict]]] = {}
    for key, e in files.items():
        by_sec.setdefault(e.get("section") or "?", []).append((key, e))
    order = list(SECTION_LABELS) + sorted(set(by_sec) - set(SECTION_LABELS))
    rows = []
    for sec in order:
        items = by_sec.get(sec, [])
        flist = []
        for key, e in sorted(items):
            pub = _parse_ts(e.get("published_at"))
            by = e.get("published_by")
            flist.append({
                "key": key, "published": _fmt(pub), "pub_dt": pub, "as_of": e.get("as_of"),
                "by": by or DEFAULT_PUBLISHER.get(sec, "Mac"), "by_inferred": not by,
                "rolled_back": bool(e.get("rolled_back_to")),
            })
        last = max((f for f in flist if f["pub_dt"]), key=lambda f: f["pub_dt"], default=None)
        pub = last["pub_dt"] if last else None
        as_of_dates = [date.fromisoformat(f["as_of"]) for f in flist if f["as_of"]]
        as_of = max(as_of_dates) if as_of_dates else None
        color, why = _light(sec, pub, now)
        rows.append({
            "id": sec, "label": SECTION_LABELS.get(sec, sec), "published": _fmt(pub),
            "pub_age": _age(now - pub) if pub else "—",
            "by": last["by"] if last else "—", "by_inferred": last["by_inferred"] if last else False,
            "as_of": as_of.strftime("%d.%m.%Y") if as_of else "—",
            "data_age": f"{(now.date() - as_of).days} дн." if as_of else "—",
            "color": color, "status": STATUS_TEXT[color], "why": why,
            "schedule": SCHEDULES.get(sec, {}).get("label", "—"),
            "proposed": SCHEDULES.get(sec, {}).get("kind") == "age",
            "files": flist,
        })
    return rows


def _read(path: str, limit: int = 200_000) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def _parallel(root: str, now: datetime) -> Dict:
    base = os.path.join(root, "parallel", "macro-calc")
    try:
        runs = sorted(d for d in os.listdir(base) if re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}", d))
    except OSError as e:
        return {"error": f"нет папки parallel/macro-calc ({e.__class__.__name__})"}
    if not runs:
        return {"error": "расчётов ещё нет"}
    run = runs[-1]
    d = os.path.join(base, run)
    started = datetime.strptime(run, "%Y-%m-%d_%H%M%S").replace(tzinfo=MSK)
    out = {"run": run, "at": _fmt(started), "age": _age(now - started), "count": len(runs)}
    sec = _read(os.path.join(d, "seconds_from_start.txt"))
    out["seconds"] = sec.strip() if sec else None
    text = _read(os.path.join(d, "add_week.out")) or ""
    m = re.search(r"Дашборд собран.*?последняя неделя:\s*([\d-]+)", text)
    out["week"] = m.group(1) if m else None
    m = re.search(r"^\s*ИПЦ NSA=([^|]+)\|\s*SA=([^|]+)\|", text, re.M)
    out["headline"] = f"NSA {m.group(1).strip()}, SA {m.group(2).strip()}" if m else None
    out["anomaly"] = "🚩 АНОМАЛИЯ" in text
    cmp_raw = _read(os.path.join(d, "compare.json"))
    if cmp_raw is None:
        out["compare"] = None
    else:
        try:
            c = json.loads(cmp_raw)
            ok = c.get("ok")  # null — опубликованной недели ещё нет (не ошибка)
            out["compare"] = {"ok": ok, "color": "grey" if ok is None else ("green" if ok else "red"),
                              "checked_at": _fmt(_parse_ts(c.get("checked_at"))),
                              "summary": c.get("summary") or {None: "не с чем сравнить", True: "в пределах критерия",
                                                              False: "вне критерия"}.get(ok, "—"),
                              "failed": [f"{x.get('group')}: {x.get('block')}.{x.get('column')} |Δ|={x.get('delta')}"
                                         for x in (c.get("checks") or []) if not x.get("ok")][:10]}
        except ValueError:
            out["compare"] = {"ok": False, "color": "red", "checked_at": "—", "summary": "compare.json не читается",
                              "failed": []}
    return out


def _backup(root: str, now: datetime) -> Dict:
    base = os.path.join(root, "backup")
    try:
        days = sorted(d for d in os.listdir(base) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    except OSError as e:
        return {"error": f"нет папки backup ({e.__class__.__name__})"}
    if not days:
        return {"error": "копий ещё нет"}
    day = days[-1]
    files = []
    for name in sorted(os.listdir(os.path.join(base, day))):
        try:
            size = os.path.getsize(os.path.join(base, day, name))
            files.append({"name": name, "size": f"{size / 2**20:.1f} МБ" if size >= 2**20 else f"{size / 1024:.0f} КБ"})
        except OSError:
            files.append({"name": name, "size": "?"})
    lag = (now.date() - date.fromisoformat(day)).days
    # ночная копия — 03:30 МСК: сегодняшняя появляется после ~04:00
    color = "green" if lag == 0 or (lag == 1 and now.hour < 5) else ("yellow" if lag <= 2 else "red")
    return {"day": date.fromisoformat(day).strftime("%d.%m.%Y"), "files": files, "count": len(days),
            "color": color, "status": STATUS_TEXT[color]}


def bucket_root(data_dir: str) -> Optional[str]:
    root = os.getenv("STATUS_BUCKET_ROOT", "").strip()
    if root:
        return root
    return os.path.dirname(data_dir.rstrip("/")) if data_dir else None


def build_status(manifest_loader, data_dir: str) -> Dict:
    hit = _cache.get("status")
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    now = datetime.now(MSK)
    root = bucket_root(data_dir)
    manifest = manifest_loader()
    if root and not manifest:  # STATUS_BUCKET_ROOT без DATA_DIR (локальная проверка)
        try:
            with open(os.path.join(root, "data", "manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            pass
    res = {
        "checked_at": _fmt(now),
        "manifest_found": manifest is not None,
        "manifest_updated": _fmt(_parse_ts((manifest or {}).get("updated_at"))),
        "sections": _sections(manifest, now),
        "bucket": bool(root and os.path.isdir(root)),
        "parallel": _parallel(root, now) if root else None,
        "backup": _backup(root, now) if root else None,
    }
    _cache["status"] = (time.time(), res)
    return res
