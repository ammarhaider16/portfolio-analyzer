"""Personal dashboard and authenticated research API."""
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings, data_directory, externally_managed, save_settings
from .delivery import render_report
from .imports import parse_holdings_csv, validate_portfolio_snapshot
from .models import AnalysisRequest, AnalysisResult, PortfolioSnapshot
from .scheduling import next_session_run
from .service import ApplicationService, build_dependencies, create_model, read_secret


class RunSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis: AnalysisRequest | None = None
    source: str = Field(default="stored", pattern="^(stored|demo)$")
    question: str = Field(default="", max_length=4000)
    include_suggestions: bool = False
    send_email: bool = False


class PortfolioUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot: PortfolioSnapshot


class ScreenshotSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_base64: str = Field(min_length=1, max_length=7_000_000)
    content_type: Literal["image/png", "image/jpeg", "image/webp"]
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Snapshot date must include a timezone")
        return value


def create_app(directory: Path | None = None, server: bool | None = None):
    directory = directory or data_directory()
    server = server if server is not None else os.environ.get("PORTFOLIO_SERVER_MODE") == "1"
    service = ApplicationService(directory, server=server)
    public_url = os.environ.get("PORTFOLIO_PUBLIC_URL", "http://127.0.0.1:8000")
    parsed_url = urlsplit(public_url)
    if server and (parsed_url.scheme != "https" or not parsed_url.hostname):
        raise ValueError("Server mode requires PORTFOLIO_PUBLIC_URL with an HTTPS origin")
    owner_password = read_secret(service.secrets, "owner_password")
    if server and (not owner_password or len(owner_password) < 16):
        raise ValueError("Server mode requires a PORTFOLIO_SECRET_OWNER_PASSWORD of at least 16 characters")
    signing_key = service.store.get("session_key")
    if not signing_key:
        signing_key = secrets.token_hex(32)
        service.store.set("session_key", signing_key)

    @asynccontextmanager
    async def lifespan(app):
        service.store.recover()
        scheduler = asyncio.create_task(service.scheduler_loop())
        yield
        scheduler.cancel()
        await service.close()
        await asyncio.gather(scheduler, return_exceptions=True)

    app = FastAPI(title="Portfolio research", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    package = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=package / "static"), name="static")
    templates = Jinja2Templates(directory=package / "templates")
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    if server:
        hosts.append(parsed_url.hostname)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    login_attempts = {}
    image_import_lock = asyncio.Lock()
    # A cookie minted by an unauthenticated local dashboard must never authorize
    # a server deployment, a different origin, or a changed owner credential.
    session_audience = hashlib.sha256((
        ("server" if server else "local") + "\0" + public_url.rstrip("/") + "\0" +
        hashlib.sha256((owner_password or "").encode()).hexdigest()
    ).encode()).hexdigest()

    def sign(payload):
        return hmac.new(signing_key.encode(), (session_audience + "\0" + payload).encode(), hashlib.sha256).hexdigest()

    def session(authenticated=False):
        payload = f"{int(time.time())}:{int(authenticated)}:{secrets.token_hex(16)}"
        return payload + ":" + sign(payload)

    def valid_session(value):
        try:
            timestamp, auth, nonce, signature = value.split(":")
            payload = f"{timestamp}:{auth}:{nonce}"
            return 0 <= time.time() - int(timestamp) < 43200 and hmac.compare_digest(signature.encode(), sign(payload).encode())
        except (ValueError, AttributeError):
            return False

    async def json_object(request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Expected a JSON object") from None
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        return body

    @app.middleware("http")
    async def security(request, call_next):
        cookie = request.cookies.get("portfolio_session", "")
        existing = valid_session(cookie)
        if not existing:
            cookie = session(authenticated=not server)
        csrf = sign("csrf:" + cookie)
        request.state.csrf = csrf
        request.state.authenticated = existing and (not server or cookie.split(":")[1] == "1")
        bearer = request.headers.get("authorization", "")
        api_key = read_secret(service.secrets, "api_token")
        api_authorized = bool(api_key and bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].encode(), api_key.encode()))
        request.state.api_authorized = api_authorized
        path = request.url.path
        public = path in {"/", "/login", "/api/login", "/health"} or path.startswith("/static/")
        if api_authorized and path.startswith("/api/broker/"):
            return JSONResponse({"detail": "Brokerage connections require the owner browser session"}, status_code=403)
        if api_authorized and request.method not in {"GET", "HEAD", "OPTIONS"} and not (path == "/api/runs" or (path.startswith("/api/runs/") and path.endswith("/cancel"))):
            return JSONResponse({"detail": "Research API tokens cannot change settings, connect accounts, or send email"}, status_code=403)
        if server and not public and not request.state.authenticated and not api_authorized:
            return JSONResponse({"detail": "Owner sign-in or API token required"}, status_code=401)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not api_authorized:
            origin = request.headers.get("origin")
            allowed_origins = {public_url.rstrip("/")}
            if not server:
                allowed_origins.add(str(request.base_url).rstrip("/"))
            if (not existing or not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), csrf.encode())
                    or (origin is not None and origin not in allowed_origins)):
                return JSONResponse({"detail": "Reload this page before submitting. Cross-site requests are blocked."}, status_code=403)
            if server and not request.state.authenticated and path != "/api/login":
                return JSONResponse({"detail": "Owner sign-in required"}, status_code=401)
        # Application endpoints accept small JSON, not arbitrary uploads.
        try:
            body_limit = 7_100_000 if path == "/api/import/image" else 1_000_000
            if int(request.headers.get("content-length", "0")) > body_limit:
                return JSONResponse({"detail": "Request is too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Enforce the actual streamed size too; Content-Length alone misses chunked uploads.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > body_limit:
                    return JSONResponse({"detail": "Request is too large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        if not existing and "set-cookie" not in response.headers:
            response.set_cookie("portfolio_session", cookie, httponly=True, secure=server, samesite="strict", max_age=43200)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
        if server:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default errors echo submitted values, potentially including secrets.
        return JSONResponse({"detail": "Invalid request", "errors": [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)[:300]}, status_code=400)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    @app.get("/login", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={"csrf": request.state.csrf, "needs_login": server and not request.state.authenticated})

    @app.post("/api/login")
    async def login(request: Request):
        now = time.monotonic()
        client_id = request.client.host if request.client else "unknown"
        for peer in list(login_attempts):
            stamps = [stamp for stamp in login_attempts[peer] if now - stamp < 300]
            if stamps:
                login_attempts[peer] = stamps
            else:
                del login_attempts[peer]
        if client_id not in login_attempts and len(login_attempts) >= 1024:
            del login_attempts[min(login_attempts, key=lambda peer: login_attempts[peer][-1])]
        attempts = login_attempts.setdefault(client_id, [])
        if len(attempts) >= 10:
            raise HTTPException(429, "Too many sign-in attempts. Wait five minutes.")
        attempts.append(now)
        body = await json_object(request)
        if not owner_password or not hmac.compare_digest(str(body.get("password", "")).encode(), owner_password.encode()):
            raise HTTPException(401, "Sign-in failed")
        login_attempts.pop(client_id, None)
        response = JSONResponse({"ok": True})
        response.set_cookie("portfolio_session", session(True), httponly=True, secure=server, samesite="strict", max_age=43200)
        return response

    @app.post("/api/logout")
    async def logout():
        nonlocal signing_key
        # Single-owner application: signing-key rotation revokes copied cookies too.
        signing_key = secrets.token_hex(32)
        service.store.set("session_key", signing_key)
        response = JSONResponse({"ok": True})
        response.delete_cookie("portfolio_session")
        return response

    @app.get("/api/status")
    async def status():
        settings = service.settings
        snapshot = service.store.snapshot()
        next_run = None
        if settings.schedule.enabled:
            next_run = (await asyncio.to_thread(next_session_run, datetime.now(timezone.utc), settings.schedule.delay_minutes, settings.schedule.timezone)).isoformat()
        return {"snapshot": snapshot.model_dump(mode="json") if snapshot else None,
                "history": service.store.history(), "settings": settings.model_dump(mode="json"),
                "externally_managed": sorted(externally_managed()), "next_run": next_run,
                "verified_recipient": service.store.get("verified_recipient"), "server": server,
                "scheduler_error": service.store.get("scheduler_error"),
                "secrets_present": {name: bool(read_secret(service.secrets, name)) for name in ("model_api_key", "tavily_api_key", "smtp_password", "alpha_vantage_api_key")}}

    @app.post("/api/settings")
    async def settings_update(request: Request):
        body = await json_object(request)
        current = service.settings
        if set(body) & externally_managed():
            raise ValueError("Change externally managed settings in the deployment environment")
        try:
            updated = Settings.model_validate(current.model_dump() | body)
        except ValidationError as exc:
            raise ValueError("Check configuration: " + "; ".join(".".join(map(str, e["loc"])) + " " + e["msg"] for e in exc.errors())) from None
        old_recipient = current.smtp.recipient if current.smtp else None
        new_recipient = updated.smtp.recipient if updated.smtp else None
        if old_recipient != new_recipient:
            service.store.set("verified_recipient", None)
            updated.scheduled_email = False
        if updated.scheduled_email and (not updated.smtp or service.store.get("verified_recipient") != updated.smtp.recipient):
            raise ValueError("Verify an email recipient before enabling scheduled delivery")
        if updated.schedule.enabled and not current.schedule.enabled:
            service.store.set("schedule_enabled_at", time.time())
        save_settings(directory, updated)
        return {"ok": True}

    @app.post("/api/secrets/{name}")
    async def save_secret(name: str, request: Request):
        if name not in {"model_api_key", "tavily_api_key", "smtp_password", "alpha_vantage_api_key"}:
            raise HTTPException(404)
        value = (await json_object(request)).get("value", "")
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("Enter a valid credential")
        from .secrets import SecretStorageError
        try:
            await asyncio.to_thread(service.secrets.set, name, value)
        except SecretStorageError as exc:
            raise ValueError(str(exc)) from None
        return {"ok": True}

    @app.post("/api/test/{kind}")
    async def test_connection(kind: str):
        try:
            async with asyncio.timeout(50):
                if kind == "model":
                    from .agent import generate_narrative
                    from .demo import demo_snapshot
                    model = create_model(service.settings, service.secrets)
                    from .metrics import calculate_metrics
                    snapshot = demo_snapshot()
                    await generate_narrative(model, AnalysisRequest(snapshot=snapshot), calculate_metrics(snapshot), [])
                elif kind == "search":
                    dep = build_dependencies(service.settings.model_copy(update={"model_enabled": False, "primary_feeds_enabled": False}), service.secrets)
                    if not dep.research:
                        raise ValueError("Choose a search provider and add its key if required")
                    results = await dep.research.search("Federal Reserve latest policy announcement", max_results=1)
                    if not results:
                        raise ValueError("Search returned no results")
                else:
                    raise HTTPException(404)
        except HTTPException:
            raise
        except Exception:
            raise ValueError("Connection test failed. Check provider, credentials, model capabilities, quota, and network access.") from None
        return {"ok": True, "message": "Connection and required response format verified"}

    @app.post("/api/import")
    async def import_holdings(request: Request):
        body = await json_object(request)
        timestamp = datetime.fromisoformat(str(body.get("as_of", "")).replace("Z", "+00:00"))
        content = body.get("csv", "")
        if not isinstance(content, str) or len(content) > 500_000:
            raise ValueError("Choose a holdings CSV smaller than 500 KB")
        snapshot = parse_holdings_csv(content, timestamp)
        if body.get("confirm"):
            service.store.save_snapshot(snapshot)
        return snapshot.model_dump(mode="json")

    @app.post("/api/portfolio")
    async def update_portfolio(update: PortfolioUpdate):
        snapshot = validate_portfolio_snapshot(update.snapshot)
        snapshot.source = "manual"
        snapshot.account_id = "manual"
        service.store.save_snapshot(snapshot)
        return snapshot.model_dump(mode="json")

    @app.post("/api/import/image")
    async def import_image(submission: ScreenshotSubmission):
        if image_import_lock.locked():
            raise HTTPException(409, "A screenshot is already being processed; wait for it to finish")
        try:
            image_bytes = base64.b64decode(submission.image_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Upload a valid PNG, JPEG, or WebP screenshot") from None
        from .screenshot_import import extract_portfolio_image, ImageImportError
        async with image_import_lock:
            try:
                model = create_model(service.settings, service.secrets)
                draft = await extract_portfolio_image(image_bytes, submission.content_type, model)
            except ImageImportError:
                raise
            except Exception:
                raise ValueError("Screenshot import requires a configured vision-capable model and API key. Configure one in Imports & models, or enter holdings manually.") from None
        # An extraction is only a draft. Saving requires a separate owner-confirmed request.
        return {**draft, "as_of": submission.as_of.isoformat(), "account_id": "manual", "unsupported": []}

    @app.post("/api/runs", status_code=202)
    async def create_run(submission: RunSubmission, request: Request):
        if request.state.api_authorized and submission.send_email:
            raise HTTPException(403, "Research API tokens cannot request email delivery")
        run_id = service.submit(submission.analysis, submission.source, submission.send_email,
                                overrides={"question": submission.question, "include_suggestions": submission.include_suggestions})
        return {"run_id": run_id, "status": "queued", "result_url": "/api/runs/" + run_id}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        row = service.store.run(run_id)
        if not row:
            raise HTTPException(404, "Report not found")
        row["result"] = json.loads(row["result"]) if row["result"] else None
        return row

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        if not service.cancel(run_id):
            raise ValueError("This run is no longer active")
        return {"ok": True}

    @app.get("/reports/{run_id}")
    async def report(run_id: str, plain: bool = False):
        row = service.store.run(run_id)
        if not row or not row["result"]:
            raise HTTPException(404, "Completed report not found")
        html, text = render_report(AnalysisResult.model_validate_json(row["result"]))
        return PlainTextResponse(text) if plain else HTMLResponse(html)

    @app.post("/api/runs/{run_id}/send")
    async def email_report(run_id: str):
        return await service.deliver(run_id)

    @app.post("/api/email/verification")
    async def verification():
        return await service.begin_verification()

    @app.post("/api/email/verify")
    async def verify(request: Request):
        service.verify(str((await json_object(request)).get("code", "")))
        return {"ok": True}

    @app.get("/api/schema/{name}")
    async def schema(name: str):
        models = {"request": AnalysisRequest, "result": AnalysisResult, "submission": RunSubmission}
        if name not in models:
            raise HTTPException(404)
        return models[name].model_json_schema()

    return app
