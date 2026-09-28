"""DocFlow AI - application factory."""
import hmac
import logging
import secrets

from dotenv import load_dotenv
from flask import Flask, Response, abort, render_template, request, session

from . import db
from .config import Config


def create_app(overrides: dict | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__, instance_relative_config=False)
    app.config.update(Config().to_flask())
    if overrides:
        app.config.update(overrides)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if app.config.get("SECRET_KEY_IS_EPHEMERAL") and not app.testing:
        app.logger.warning("SECRET_KEY not set - using a temporary key (fine for local demo only).")

    db.init_db(app)

    from .routes import bp
    app.register_blueprint(bp)

    # ---- Optional password gate for public deployments (HTTP Basic Auth)
    @app.before_request
    def require_password():
        password = app.config.get("APP_PASSWORD")
        if not password or request.path == "/healthz":
            return None
        auth = request.authorization
        if auth and auth.password and hmac.compare_digest(auth.password.encode(), password.encode()):
            return None
        return Response("Password required.", 401,
                        {"WWW-Authenticate": 'Basic realm="DocFlow AI demo", charset="UTF-8"'})

    # ---- CSRF: a per-session token required on every POST form
    @app.before_request
    def csrf_protect():
        if request.method == "POST" and not app.config.get("CSRF_DISABLED"):
            token = session.get("csrf_token")
            if not token or token != request.form.get("csrf_token"):
                abort(400, "Your session expired. Please reload the page and try again.")

    @app.context_processor
    def inject_globals():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        # Only non-sensitive facts about configuration are ever exposed to templates.
        provider = ("Claude · " + app.config["LLM_MODEL"]) if app.config.get("ANTHROPIC_API_KEY") \
            else ("Offline demo analyzer" if app.config.get("DEMO_MODE") else "No AI configured")
        return {"csrf_token": session["csrf_token"], "demo_mode": app.config.get("DEMO_MODE"),
                "provider_label": provider, "ai_live": bool(app.config.get("ANTHROPIC_API_KEY")),
                "max_upload_mb": app.config["MAX_UPLOAD_MB"]}

    # ---- Security headers
    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    # ---- Friendly errors that never leak internals
    @app.errorhandler(400)
    def bad_request(e):
        return render_template("error.html", title="Request problem",
                               message=getattr(e, "description", "Bad request.")), 400

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", title="Not found",
                               message="That page or document doesn't exist."), 404

    @app.errorhandler(413)
    def too_large(_e):
        return render_template("error.html", title="File too large",
                               message=f"Uploads are limited to {app.config['MAX_UPLOAD_MB']} MB."), 413

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("error.html", title="Something went wrong",
                               message="An unexpected error occurred. Please try again."), 500

    return app
