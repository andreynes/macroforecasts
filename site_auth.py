"""Вход на закрытые вкладки сайта: форма логина + подписанная cookie.

Почему не Basic Auth: Yandex Serverless Containers забирает заголовок Authorization
себе (он не доходит до приложения) и вырезает WWW-Authenticate из ответа, поэтому
браузер даже не спрашивает пароль. Cookie платформа не трогает.

Открытые адреса задаются списком public_paths (вкладка «Макропрогнозы» и её API),
всё остальное требует входа. Логин/пароль — переменные окружения BASIC_AUTH_USER /
BASIC_AUTH_PASS (имена исторические), их передаёт deploy_yc.sh.
"""
import hashlib
import hmac
import time
from typing import Iterable

from flask import Flask, Response, jsonify, make_response, redirect, render_template_string, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE_NAME = "macro_auth"
COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 дней

_LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Вход — Макропрогнозы</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f6f8;margin:0;
       display:flex;min-height:100vh;align-items:center;justify-content:center;color:#1f2933}
  form{background:#fff;padding:28px 28px 24px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);
       width:100%;max-width:340px;box-sizing:border-box;margin:16px}
  h1{font-size:20px;margin:0 0 6px} p{margin:0 0 18px;color:#52606d;font-size:14px}
  label{display:block;font-size:13px;margin:12px 0 4px;color:#52606d}
  input{width:100%;box-sizing:border-box;padding:10px 12px;font-size:16px;border:1px solid #cbd2d9;border-radius:8px}
  button{margin-top:18px;width:100%;padding:11px;font-size:16px;border:0;border-radius:8px;background:#1f6feb;color:#fff}
  .err{color:#b42318;font-size:14px;margin:12px 0 0} a{color:#1f6feb;font-size:14px}
</style></head><body>
<form method="post" action="/login">
  <h1>Вход</h1>
  <p>Раздел доступен по логину и паролю. <a href="/">Макропрогнозы</a> открыты без входа.</p>
  <input type="hidden" name="next" value="{{ next_url }}">
  <label for="u">Логин</label>
  <input id="u" name="username" autocomplete="username" autocapitalize="none" autofocus required>
  <label for="p">Пароль</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  {% if error %}<div class="err">Неверный логин или пароль.</div>{% endif %}
  <button type="submit">Войти</button>
</form></body></html>"""


def _safe_next(target: str) -> str:
    """Разрешаем возврат только на адреса этого же сайта."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def init_auth(app: Flask, require: bool, user: str, password: str, public_paths: Iterable[str]) -> None:
    public = set(public_paths) | {"/login", "/logout"}
    # «pass» — значение по умолчанию, паролем не считается: закрытое остаётся закрытым для всех.
    configured = bool(user) and bool(password) and password != "pass"
    secret = hashlib.sha256(("macro-site-auth|" + user + "|" + password).encode("utf-8")).hexdigest()
    signer = URLSafeTimedSerializer(secret, salt="macro-auth-cookie")

    def _is_public(path: str) -> bool:
        return path in public or path.startswith("/static/")

    def _logged_in() -> bool:
        token = request.cookies.get(COOKIE_NAME)
        if not token or not configured:
            return False
        try:
            return signer.loads(token, max_age=COOKIE_MAX_AGE) == user
        except BadSignature:
            return False

    @app.before_request
    def _require_login():
        if not require or _is_public(request.path) or _logged_in():
            return None
        if request.path.startswith("/api/") or request.method != "GET":
            return jsonify({"error": "Нужен вход"}), 401
        full = request.full_path if request.query_string else request.path
        return redirect("/login?next=" + _quote(full))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not require:
            return redirect("/")
        if request.method == "GET":
            return render_template_string(_LOGIN_PAGE, next_url=_safe_next(request.args.get("next", "/")), error=False)
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        nxt = _safe_next(request.form.get("next", "/"))
        ok = (configured
              and hmac.compare_digest(u.encode("utf-8"), user.encode("utf-8"))
              and hmac.compare_digest(p.encode("utf-8"), password.encode("utf-8")))
        if not ok:
            time.sleep(1)  # притормаживаем перебор
            return render_template_string(_LOGIN_PAGE, next_url=nxt, error=True), 401
        resp = make_response(redirect(nxt))
        resp.set_cookie(COOKIE_NAME, signer.dumps(user), max_age=COOKIE_MAX_AGE,
                        httponly=True, secure=request.is_secure or request.headers.get("X-Forwarded-Proto", "https") == "https",
                        samesite="Lax")
        return resp

    @app.route("/logout")
    def logout():
        resp = make_response(redirect("/"))
        resp.delete_cookie(COOKIE_NAME)
        return resp


def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="/")
