"""Публикация данных сайта в бакет Object Storage — без Docker и выкладки сайта.

    publish.sh <раздел> [--as-of ГГГГ-ММ-ДД]   проверить файлы раздела, загрузить изменённые в бакет,
                                               обновить manifest.json, дождаться их на сайте
    publish.sh --rollback <раздел>             вернуть раздел к предыдущей публикации (из версий бакета)
    publish.sh --list                          разделы и их файлы
    publish.sh <раздел> --dry-run              вхолостую: проверка файлов, что изменилось бы, вход на сайт —
                                               без загрузки в бакет и без записи manifest.json

Разделы: ruble, moex, ngd, cbr_models, macro, analysts, yield, inflation_weekly, inflation_monthly (см. SECTIONS ниже).

Шаги публикации:
  1. Проверка файлов: JSON разбирается и не содержит NaN/Infinity, Excel открывается и в нём есть нужные листы.
     Хоть один файл не прошёл — в бакет не загружается ничего. Дата данных — поле data_as_of, которое пишет
     скрипт расчёта (НГД, нефть и экспорт, инфляция); у остальных — последняя дата в файле не позже сегодняшней.
  2. Загрузка в бакет только изменившихся файлов (сравнение по SHA-256 с manifest.json).
  3. manifest.json (в бакете: data/manifest.json): по каждому файлу — когда опубликован, за какую дату данные,
     контрольная сумма и номер версии в бакете; по каждому разделу — история публикаций (для отката).
  4. Проверка сайта: вход через /login, ожидание, пока /diag/data несколько раз подряд не покажет новые
     контрольные суммы из бакета (кеш монтирования — до нескольких минут), затем страницы раздела должны ответить 200.

Ничего не привязано к конкретному компьютеру — всё настраивается переменными окружения:
  SITE_DIR          папка сайта (по умолчанию — папка, где лежит этот скрипт, уровнем выше)
  SITE_URL          адрес сайта
  DATA_BUCKET       бакет с данными (macro-site-data)
  PUBLISHER_KEY     ключ сервисного аккаунта macro-publisher (~/.config/macro/publisher-key.json)
  YC_PUBLISHER_HOME отдельная папка настроек yc для этого ключа (~/.config/macro/yc-publisher);
                    создаётся сама из PUBLISHER_KEY, настройки yc пользователя не затрагиваются
  SITE_USER, SITE_PASS  логин и пароль сайта; если не заданы — читаются из SITE_AUTH_FILE
  SITE_AUTH_FILE    файл с BASIC_AUTH_USER=… и BASIC_AUTH_PASS=… (~/.config/macro/site_auth.env)
  YC                команда yc (по умолчанию — из PATH или ~/yandex-cloud/bin/yc)
  PUBLISH_AUTH      instance — на облачной машине: yc работает от сервисного аккаунта машины (профиль yc
                    с instance-service-account), без PUBLISHER_KEY и отдельной папки настроек
  SITE_AUTH_LOCKBOX_ID  id секрета Lockbox с BASIC_AUTH_USER/BASIC_AUTH_PASS — если SITE_USER/SITE_PASS не заданы
  PUBLISH_WAIT      сколько секунд ждать новые данные на сайте (420)
Ключ и пароль скрипт не печатает.
"""
import argparse
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SITE_DIR = os.environ.get("SITE_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_URL = os.environ.get("SITE_URL", "https://bba2agi0c621jbhicgod.containers.yandexcloud.net").rstrip("/")
BUCKET = os.environ.get("DATA_BUCKET", "macro-site-data")
PUBLISHER_KEY = os.path.expanduser(os.environ.get("PUBLISHER_KEY", "~/.config/macro/publisher-key.json"))
YC_HOME = os.path.expanduser(os.environ.get("YC_PUBLISHER_HOME", "~/.config/macro/yc-publisher"))
AUTH_FILE = os.path.expanduser(os.environ.get("SITE_AUTH_FILE", "~/.config/macro/site_auth.env"))
# yc: переменная YC, иначе из PATH, иначе стандартная папка установки (у launchd PATH урезан до /usr/bin:/bin:…)
YC = (os.environ.get("YC") or shutil.which("yc")
      or next((p for p in [os.path.expanduser("~/yandex-cloud/bin/yc")] if os.path.isfile(p)), "yc"))
WAIT = int(os.environ.get("PUBLISH_WAIT", "420"))
INSTANCE_AUTH = os.environ.get("PUBLISH_AUTH", "") == "instance"
SITE_AUTH_LOCKBOX_ID = os.environ.get("SITE_AUTH_LOCKBOX_ID", "")
MANIFEST_KEY = "data/manifest.json"
HISTORY_LEN = 10
STABLE_POLLS = 3  # сколько ответов подряд должны совпасть: у сайта несколько экземпляров контейнера

# Раздел -> файлы (ключ в бакете, путь в папке сайта), обязательные листы Excel, адреса для проверки
SECTIONS = {
    "ruble": {
        "files": {f"data/{n}": f"data/{n}" for n in (
            "ruble_fx.json", "ruble_page.json", "cbr_fx_ops.json", "exporters_need.json", "oil_exports.json")},
        "pages": ["/ruble", "/api/ruble/page", "/api/ruble/fx", "/api/ruble/cbr_fx_ops"],
    },
    # Данные Мосбиржи для «Рубля» (FUTOI физлиц, объёмы юаня) — с 17.11.2026 публикует ОБЛАЧНАЯ МАШИНА
    # (inflation/tools/run_moex_vm.sh, таймер moex по рабочим дням в 19:30). Mac этот раздел не публикует.
    "moex": {
        "files": {f"data/{n}": f"data/{n}" for n in ("futoi_households.json", "cny_volumes.json")},
        "pages": ["/ruble", "/api/ruble/futoi", "/api/ruble/cny_volumes"],
    },
    "ngd": {"files": {"data/ngd_dashboard.json": "data/ngd_dashboard.json"}, "pages": ["/ngd", "/api/ngd"]},
    "cbr_models": {"files": {"data/cbr_models.json": "data/cbr_models.json"},
                   "pages": ["/cbr-models", "/api/cbr_models"]},
    "macro": {"files": {"excel/macro_main_linked.xlsx": "macro_main_linked.xlsx"},
              "sheets": {"macro_main_linked.xlsx": ["main"]},
              "pages": ["/", "/api/indicators", "/export"]},
    "analysts": {"files": {"excel/analyst_forecasts.xlsx": "analyst_forecasts.xlsx"},
                 "pages": ["/api/analyst_forecasts"]},
    "inflation_weekly": {"files": {"data/inflation_weekly.json": "data/inflation_weekly.json"},
                         "pages": ["/inflation/weekly", "/api/inflation/weekly"]},
    "inflation_monthly": {"files": {"data/inflation_monthly.json": "data/inflation_monthly.json"},
                          "pages": ["/inflation/monthly", "/api/inflation/monthly"]},
    "yield": {
        "files": {f"excel/{n}": n for n in (
            "yield_forecast_data.xlsx", "Денежный рынок – Ставки_исправлено_v2.xlsx", "Условия кредитования ЦБ.xlsx")},
        "sheets": {"yield_forecast_data.xlsx": ["Ключевая ставка", "Ставка по депозитам", "Доходности КБД"],
                   "Условия кредитования ЦБ.xlsx": ["Условия кредитования"]},
        "pages": ["/yield-forecast", "/ofz", "/api/yield/key_rate", "/api/ofz/model1"],
    },
}


class PublishError(Exception):
    pass


def log(msg=""):
    print(msg, flush=True)


def now_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------- проверка файлов и дата данных ----------------

def _reject_constant(name):
    raise ValueError(f"в JSON есть {name} — график на сайте будет пустым")


def _max_past_date(values, today):
    best = None
    for v in values:
        d = None
        if isinstance(v, dt.datetime):
            d = v.date()
        elif isinstance(v, dt.date):
            d = v
        elif isinstance(v, str) and len(v) >= 10 and v[4] == "-" and v[7] == "-":
            try:
                d = dt.date.fromisoformat(v[:10])
            except ValueError:
                d = None
        if d and d <= today and (best is None or d > best):
            best = d
    return best


def _walk_json(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _walk_json(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_json(v)
    else:
        yield x


def check_file(path, required_sheets=()):
    """Проверяет файл и возвращает дату данных (последнюю дату не позже сегодняшней) или None."""
    today = dt.date.today()
    if not os.path.isfile(path):
        raise PublishError(f"нет файла {path}")
    if os.path.getsize(path) == 0:
        raise PublishError(f"пустой файл {path}")
    name = os.path.basename(path)
    if name.endswith(".json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f, parse_constant=_reject_constant)
        except ValueError as e:
            raise PublishError(f"{name}: повреждённый JSON — {e}")
        # Дату данных скрипт расчёта пишет сам (поле data_as_of); нет поля — последняя дата не позже сегодня
        if isinstance(data, dict) and data.get("data_as_of"):
            try:
                return dt.date.fromisoformat(str(data["data_as_of"])[:10]).isoformat()
            except ValueError:
                raise PublishError(f"{name}: поле data_as_of не дата ГГГГ-ММ-ДД: {data['data_as_of']!r}")
        d = _max_past_date(_walk_json(data), today)
        return d.isoformat() if d else None
    if name.endswith(".xlsx"):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as e:
            raise PublishError(f"{name}: не открывается как Excel — {e}")
        try:
            missing = [s for s in required_sheets if s not in wb.sheetnames]
            if missing:
                raise PublishError(f"{name}: нет листов {', '.join(missing)}")
            if name == "analyst_forecasts.xlsx":
                # вкладки названы датой снимка (д.м.гг) — дата данных = последний снимок
                dates = []
                for s in wb.sheetnames:
                    try:
                        dates.append(dt.datetime.strptime(s.strip(), "%d.%m.%y").date())
                    except ValueError:
                        pass
                if not dates:
                    raise PublishError(f"{name}: нет ни одной вкладки-снимка с датой")
                d = _max_past_date(dates, today)
            else:
                sheets = list(required_sheets) or wb.sheetnames[:1]
                d = None
                for s in sheets:
                    cand = _max_past_date((v for row in wb[s].iter_rows(values_only=True) for v in row), today)
                    if cand and (d is None or cand > d):
                        d = cand
            return d.isoformat() if d else None
        finally:
            wb.close()
    raise PublishError(f"{name}: неизвестный тип файла")


# ---------------- бакет (yc под сервисным аккаунтом macro-publisher) ----------------

def _yc_env():
    env = dict(os.environ)
    if not INSTANCE_AUTH:
        env["HOME"] = YC_HOME  # отдельная папка настроек yc: настройки пользователя не меняются
    return env


def ensure_yc():
    if not (os.path.isfile(YC) or shutil.which(YC)):
        raise PublishError("не найден yc (CLI Яндекс Облака): задайте путь переменной YC или добавьте yc в PATH")
    if INSTANCE_AUTH:  # облачная машина: доступ от её сервисного аккаунта через метаданные, ключа нет
        return
    cfg = os.path.join(YC_HOME, ".config", "yandex-cloud", "config.yaml")
    if os.path.isfile(cfg):
        return
    if not os.path.isfile(PUBLISHER_KEY):
        raise PublishError(f"нет ключа сервисного аккаунта: {PUBLISHER_KEY} (переменная PUBLISHER_KEY)")
    os.makedirs(YC_HOME, mode=0o700, exist_ok=True)
    for args in (["config", "profile", "create", "publisher"], ["config", "set", "service-account-key", PUBLISHER_KEY]):
        r = subprocess.run([YC] + args, env=_yc_env(), capture_output=True, text=True)
        if r.returncode != 0:
            raise PublishError("не удалось настроить yc для macro-publisher (вывод скрыт: в нём может быть ключ)")


def yc(*args, check=True):
    r = subprocess.run([YC, *args], env=_yc_env(), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise PublishError(f"yc {' '.join(args[:3])}…: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def bucket_head(key):
    r = yc("storage", "s3api", "head-object", "--bucket", BUCKET, "--key", key, "--format", "json", check=False)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def bucket_get(key, dest, version_id=None):
    args = ["storage", "s3api", "get-object", "--bucket", BUCKET, "--key", key]
    if version_id:
        args += ["--version-id", version_id]
    r = yc(*args, dest, check=False)
    return r.returncode == 0


def bucket_put(path, key):
    yc("storage", "s3", "cp", path, f"s3://{BUCKET}/{key}")
    head = bucket_head(key)
    if not head:
        raise PublishError(f"файл {key} не виден в бакете после загрузки")
    return head.get("version_id")


def load_manifest(tmpdir):
    path = os.path.join(tmpdir, "manifest.json")
    if bucket_get(MANIFEST_KEY, path):
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    else:
        m = {}
    m.setdefault("files", {})
    m.setdefault("sections", {})
    return m


def save_manifest(m, tmpdir):
    m["updated_at"] = now_utc()
    path = os.path.join(tmpdir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1, allow_nan=False)
    bucket_put(path, MANIFEST_KEY)


def _snapshot(m, section):  # только файлы, которые сейчас входят в раздел
    return {"published_at": now_utc(),
            "files": {k: {f: m["files"][k].get(f) for f in ("version_id", "sha256", "as_of", "published_at")}
                      for k in SECTIONS[section]["files"] if k in m["files"]}}


def _baseline(m, section, tmpdir):
    """Первая публикация раздела: запомнить, что уже лежит в бакете, — чтобы было куда откатиться.
    Возвращает True, если манифест изменился."""
    sec = m["sections"].setdefault(section, {"history": []})
    if sec["history"]:
        return False
    for key in SECTIONS[section]["files"]:
        if key in m["files"]:
            continue
        head = bucket_head(key)
        if not head:
            continue
        p = os.path.join(tmpdir, "baseline_" + os.path.basename(key))
        if not bucket_get(key, p, head.get("version_id")):
            continue
        try:
            as_of = check_file(p, SECTIONS[section].get("sheets", {}).get(os.path.basename(key), ()))
        except PublishError:
            as_of = None
        m["files"][key] = {"section": section, "published_at": head.get("last_modified_at"), "as_of": as_of,
                           "sha256": sha256(p), "size": os.path.getsize(p), "version_id": head.get("version_id")}
    if any(k in m["files"] for k in SECTIONS[section]["files"]):
        snap = _snapshot(m, section)
        snap["baseline"] = True
        sec["history"].append(snap)
        return True
    return False


# ---------------- проверка сайта ----------------

def _site_credentials():
    user, pwd = os.environ.get("SITE_USER"), os.environ.get("SITE_PASS")
    if user and pwd:
        return user, pwd
    if SITE_AUTH_LOCKBOX_ID:  # облачная машина: секрет Lockbox (значения не печатаются)
        got = [yc("lockbox", "payload", "get", "--id", SITE_AUTH_LOCKBOX_ID, "--key", k, check=False)
               for k in ("BASIC_AUTH_USER", "BASIC_AUTH_PASS")]
        if all(r.returncode == 0 and r.stdout.strip() for r in got):
            return got[0].stdout.strip(), got[1].stdout.strip()
        raise PublishError(f"не удалось прочитать логин и пароль сайта из Lockbox (секрет {SITE_AUTH_LOCKBOX_ID})")
    vals = {}
    if os.path.isfile(AUTH_FILE):
        with open(AUTH_FILE, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals.get("BASIC_AUTH_USER"), vals.get("BASIC_AUTH_PASS")


class Site:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path, timeout=60):
        url = SITE_URL + urllib.parse.quote(path, safe="/?=&%")
        try:
            with self.opener.open(url, timeout=timeout) as r:
                return r.status, r.geturl(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, url, e.read()

    def login(self):
        user, pwd = _site_credentials()
        if user and pwd:
            data = urllib.parse.urlencode({"username": user, "password": pwd, "next": "/"}).encode()
            try:
                self.opener.open(SITE_URL + "/login", data=data, timeout=60).read()
            except urllib.error.HTTPError:
                pass
        status, url, body = self.get("/diag/data")
        if status != 200 or "/login" in url:
            raise PublishError("не удалось войти на сайт: проверьте логин и пароль (SITE_USER/SITE_PASS или SITE_AUTH_FILE)")
        return json.loads(body)

    def diag(self):
        status, url, body = self.get("/diag/data")
        if status != 200 or "/login" in url:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None


def verify_site(section, expected):
    """Ждёт, пока сайт несколько раз подряд не покажет ожидаемые контрольные суммы, затем проверяет страницы."""
    log(f"=== Проверка сайта {SITE_URL}")
    site = Site()
    site.login()
    if expected:
        log(f"Жду новые данные на сайте (до {WAIT} с; изменения в бакете доходят за 1–4 мин)…")
        start, streak = time.time(), 0
        while True:
            d = site.diag()
            files = (d or {}).get("files", {})
            pending = [k for k, sha in expected.items()
                       if files.get(k, {}).get("source") != "бакет" or files.get(k, {}).get("sha256") != sha]
            streak = streak + 1 if d and not pending else 0
            waited = int(time.time() - start)
            if streak >= STABLE_POLLS:
                log(f"  новые данные на сайте ({waited} с)")
                break
            if waited > WAIT:
                raise PublishError("сайт не показал новые данные за отведённое время; ещё ждём: " + ", ".join(pending))
            log(f"  {waited:>3} с: " + ("ещё не обновились: " + ", ".join(os.path.basename(k) for k in pending)
                                        if pending else f"совпало ({streak}/{STABLE_POLLS})"))
            time.sleep(10)
    bad = []
    for p in SECTIONS[section]["pages"]:
        status, url, _ = site.get(p, timeout=120)
        ok = status == 200 and "/login" not in url
        log(f"  {'ок    ' if ok else 'ОШИБКА'}  {p} -> {status}")
        if not ok:
            bad.append(p)
    if bad:
        raise PublishError("страницы раздела не открываются: " + ", ".join(bad))


# ---------------- публикация и откат ----------------

def publish(section, as_of_override=None, dry_run=False):
    spec = SECTIONS[section]
    log(f"=== Раздел «{section}»: проверка файлов")
    checked = {}
    errors = []
    for key, rel in spec["files"].items():
        path = os.path.join(SITE_DIR, rel)
        try:
            as_of = check_file(path, spec.get("sheets", {}).get(os.path.basename(rel), ()))
            checked[key] = (path, as_of_override or as_of)
            log(f"  ок      {rel}  (данные по {checked[key][1] or '—'})")
        except PublishError as e:
            errors.append(str(e))
            log(f"  ОШИБКА  {e}")
    if errors:
        raise PublishError("файлы не прошли проверку — в бакет ничего не загружено")

    ensure_yc()
    with tempfile.TemporaryDirectory() as tmp:
        m = load_manifest(tmp)
        dirty = _baseline(m, section, tmp)
        log("=== Загрузка в бакет")
        changed = {}
        for key, (path, as_of) in checked.items():
            digest = sha256(path)
            cur = m["files"].get(key)
            if cur and cur.get("sha256") == digest:
                log(f"  без изменений  {key}")
                continue
            if dry_run:
                changed[key] = digest
                log(f"  загрузил бы    {key}  (вхолостую — не загружаю)")
                continue
            version = bucket_put(path, key)
            m["files"][key] = {"section": section, "published_at": now_utc(), "as_of": as_of, "sha256": digest,
                               "size": os.path.getsize(path), "version_id": version}
            changed[key] = digest
            log(f"  загружен       {key}")
        if dry_run:
            log("Вхолостую: бакет и manifest.json не изменены.")
            site = Site()
            site.login()
            d = site.diag() or {}
            log(f"  вход на сайт: ок; /diag/data отвечает, файлов в нём: {len(d.get('files', {}))}")
            log(f"Готово (вхолостую): раздел «{section}», изменились бы: {', '.join(changed) or 'ничего'}.")
            return
        if changed:
            hist = m["sections"].setdefault(section, {"history": []})["history"]
            hist.append(_snapshot(m, section))
            del hist[:-HISTORY_LEN]
        else:
            log("Все файлы раздела уже опубликованы — загружать нечего.")
        if changed or dirty:
            save_manifest(m, tmp)
            log("  manifest.json обновлён")
    verify_site(section, changed)
    log(f"Готово: раздел «{section}» опубликован.")


def rollback(section):
    ensure_yc()
    with tempfile.TemporaryDirectory() as tmp:
        m = load_manifest(tmp)
        hist = m["sections"].get(section, {}).get("history", [])
        if len(hist) < 2:
            raise PublishError(f"у раздела «{section}» нет предыдущей публикации для отката")
        target = hist[-2]
        log(f"=== Откат раздела «{section}» к публикации {target['published_at']}")
        restored = {}
        for key, t in target["files"].items():
            if key not in SECTIONS[section]["files"]:
                continue  # файл перешёл в другой раздел (напр. FUTOI/юань из ruble в moex) — его не трогаем
            cur = m["files"].get(key, {})
            if cur.get("sha256") == t["sha256"]:
                log(f"  без изменений  {key}")
                continue
            p = os.path.join(tmp, os.path.basename(key))
            if not bucket_get(key, p, t["version_id"]):
                raise PublishError(f"в бакете нет версии {t['version_id']} файла {key}")
            if sha256(p) != t["sha256"]:
                raise PublishError(f"версия {t['version_id']} файла {key} не совпадает с записанной в manifest.json")
            check_file(p, SECTIONS[section].get("sheets", {}).get(os.path.basename(key), ()))
            version = bucket_put(p, key)
            m["files"][key] = {"section": section, "published_at": now_utc(), "as_of": t.get("as_of"),
                               "sha256": t["sha256"], "size": os.path.getsize(p), "version_id": version,
                               "rolled_back_to": t["published_at"]}
            restored[key] = t["sha256"]
            log(f"  возвращён      {key}")
        hist.pop()  # отменённая публикация больше не считается последней
        save_manifest(m, tmp)
        log("  manifest.json обновлён")
    verify_site(section, restored)
    log(f"Готово: раздел «{section}» возвращён к предыдущей публикации.")


def main():
    p = argparse.ArgumentParser(description="Публикация данных сайта в бакет Object Storage")
    p.add_argument("section", nargs="?", choices=sorted(SECTIONS))
    p.add_argument("--rollback", action="store_true", help="вернуть раздел к предыдущей публикации")
    p.add_argument("--as-of", help="дата данных для manifest.json (ГГГГ-ММ-ДД), если не подходит определённая автоматически")
    p.add_argument("--list", action="store_true", help="показать разделы и их файлы")
    p.add_argument("--dry-run", action="store_true", help="вхолостую: без загрузки в бакет и записи manifest.json")
    a = p.parse_args()
    if a.list or not a.section:
        for name, spec in SECTIONS.items():
            log(f"{name}: " + ", ".join(spec["files"].values()))
        return 0 if a.list else 2
    if a.as_of:
        dt.date.fromisoformat(a.as_of)
    try:
        if a.rollback and a.dry_run:
            raise PublishError("--dry-run для отката не поддерживается")
        rollback(a.section) if a.rollback else publish(a.section, a.as_of, a.dry_run)
    except PublishError as e:
        log(f"!!! ОШИБКА: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
