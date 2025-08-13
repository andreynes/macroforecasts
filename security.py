import os
from flask import Response, request, send_from_directory, current_app

def _basic_auth_required():
    """Включается, если REQUIRE_BASIC_AUTH=1 в окружении."""
    return os.getenv("REQUIRE_BASIC_AUTH", "0") == "1"

def _check_auth(username, password):
    return (
        username == os.getenv("BASIC_AUTH_USER", "user")
        and password == os.getenv("BASIC_AUTH_PASS", "pass")
    )

def _authenticate():
    return Response(
        "Нужна авторизация", 401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'}
    )

def init_noindex(app):
    """
    Подключает:
    - /robots.txt (Disallow: /)
    - Заголовок X-Robots-Tag: noindex, nofollow
    - (опционально) Basic Auth на всё приложение, если REQUIRE_BASIC_AUTH=1
    """

    # 1) robots.txt из /static/robots.txt, если есть, иначе сгенерим «на лету»
    @app.route("/robots.txt")
    def robots_txt():
        static_path = os.path.join(app.root_path, "static")
        file_path = os.path.join(static_path, "robots.txt")
        if os.path.exists(file_path):
            return send_from_directory(static_path, "robots.txt")
        return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

    # 2) Глобально запрещаем индексацию
    @app.after_request
    def add_noindex_headers(resp):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    # 3) (опционально) Базовая авторизация
    @app.before_request
    def maybe_require_auth():
        if not _basic_auth_required():
            return None
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return _authenticate()
        return None
