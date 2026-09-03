"""FastAPI只读监管后台及admin安全登录。"""

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine
from starlette.middleware.sessions import SessionMiddleware

from gold_crypto_quant.config import get_settings
from gold_crypto_quant.storage.database import build_engine, create_schema
from gold_crypto_quant.storage.shadow_monitor import ensure_system_shadow_accounts
from gold_crypto_quant.storage.web_admin import (
    authenticate_admin,
    change_admin_password,
    create_admin_if_missing,
    get_active_admin,
    save_admin_audit,
    validate_password_strength,
)
from gold_crypto_quant.web.data import (
    build_live_quotes,
    build_market_chart,
    build_overview,
    build_system_status,
    build_trade_events,
)

PACKAGE_DIR = Path(__file__).resolve().parent
SESSION_SECRET_PATH = Path(".runtime/web-session-secret")


def _session_secret() -> str:
    """首次启动生成仅当前用户可读的Cookie签名密钥。"""
    SESSION_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SESSION_SECRET_PATH.exists():
        SESSION_SECRET_PATH.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        SESSION_SECRET_PATH.chmod(0o600)
    return SESSION_SECRET_PATH.read_text(encoding="utf-8").strip()


def _static_version() -> str:
    """返回静态目录最新修改时间，用作资源URL的缓存版本号。"""
    static_dir = PACKAGE_DIR / "static"
    try:
        latest = max(path.stat().st_mtime for path in static_dir.iterdir() if path.is_file())
    except (OSError, ValueError):
        return "0"
    return str(int(latest))


def create_app(engine: Engine | None = None) -> FastAPI:
    """创建可测试的Web应用；所有业务接口均要求admin会话。"""
    settings = get_settings()
    engine = engine or build_engine()
    create_schema(engine)
    ensure_system_shadow_accounts(engine)
    app = FastAPI(title="Moon Oversight", docs_url=None, redoc_url=None)
    app.state.engine = engine
    app.state.initial_admin_password = create_admin_if_missing(engine)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(),
        session_cookie="quant_admin_session",
        max_age=8 * 60 * 60,
        same_site="strict",
        https_only=settings.web_cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    # 静态资源URL带上文件修改时间；样式或脚本一变URL就变，浏览器和CDN不会再用旧缓存。
    templates.env.globals["static_version"] = _static_version

    def current_admin(request: Request):
        user_id = request.session.get("user_id")
        if not isinstance(user_id, int):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        user = get_active_admin(user_id, request.app.state.engine)
        if user is None:
            request.session.clear()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        if user.force_password_change and request.url.path not in {"/password", "/logout"}:
            raise HTTPException(status_code=428, detail="password change required")
        return user

    def page_context(request: Request, user, active_page: str) -> dict[str, object]:
        return {
            "request": request,
            "user": user,
            "active_page": active_page,
            "csrf_token": request.session.get("csrf_token", ""),
        }

    @app.exception_handler(HTTPException)
    async def auth_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and request.url.path.startswith("/api/"):
            return HTMLResponse("Unauthorized", status_code=401)
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return RedirectResponse("/login", status_code=303)
        if exc.status_code == 428:
            return RedirectResponse("/password", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if request.session.get("user_id"):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": None, "initial_password_created": bool(app.state.initial_admin_password)},
        )

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, username: str = Form(), password: str = Form()):
        user = authenticate_admin(username, password, engine=request.app.state.engine)
        ip_address = request.client.host if request.client else None
        if user is None:
            save_admin_audit(
                user_id=None,
                action="LOGIN",
                result="FAILED",
                ip_address=ip_address,
                user_agent=request.headers.get("user-agent"),
                details={"username": username[:64]},
                engine=request.app.state.engine,
            )
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "用户名或密码不正确，连续失败五次将锁定十五分钟。"},
                status_code=400,
            )
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["csrf_token"] = secrets.token_urlsafe(24)
        save_admin_audit(
            user_id=user.id,
            action="LOGIN",
            result="SUCCESS",
            ip_address=ip_address,
            user_agent=request.headers.get("user-agent"),
            engine=request.app.state.engine,
        )
        return RedirectResponse("/password" if user.force_password_change else "/", status_code=303)

    @app.get("/password", response_class=HTMLResponse)
    def password_page(request: Request, user=Depends(current_admin)):
        return templates.TemplateResponse(
            request,
            "password.html",
            {**page_context(request, user, "password"), "error": None},
        )

    @app.post("/password", response_class=HTMLResponse)
    def update_password(
        request: Request,
        current_password: str = Form(),
        new_password: str = Form(),
        confirm_password: str = Form(),
        csrf_token: str = Form(),
        user=Depends(current_admin),
    ):
        error = None
        if not secrets.compare_digest(csrf_token, request.session.get("csrf_token", "")):
            raise HTTPException(status_code=403)
        if new_password != confirm_password:
            error = "两次输入的新密码不一致。"
        else:
            try:
                validate_password_strength(new_password)
            except ValueError:
                error = "新密码至少8个字符，且需含大写、小写、数字、标点中至少3类。"
        if not error and not change_admin_password(
            user.id,
            current_password,
            new_password,
            engine=request.app.state.engine,
        ):
            error = "当前密码不正确。"
        if error:
            return templates.TemplateResponse(
                request,
                "password.html",
                {**page_context(request, user, "password"), "error": error},
                status_code=400,
            )
        save_admin_audit(
            user_id=user.id,
            action="CHANGE_PASSWORD",
            result="SUCCESS",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            engine=request.app.state.engine,
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(
        request: Request,
        csrf_token: str = Form(),
        user=Depends(current_admin),
    ):
        if not secrets.compare_digest(csrf_token, request.session.get("csrf_token", "")):
            raise HTTPException(status_code=403)
        save_admin_audit(
            user_id=user.id,
            action="LOGOUT",
            result="SUCCESS",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            engine=request.app.state.engine,
        )
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user=Depends(current_admin)):
        if user.force_password_change:
            return RedirectResponse("/password", status_code=303)
        return templates.TemplateResponse(
            request, "dashboard.html", page_context(request, user, "dashboard")
        )

    @app.get("/market", response_class=HTMLResponse)
    def market_page(request: Request, user=Depends(current_admin)):
        return templates.TemplateResponse(
            request, "market.html", page_context(request, user, "market")
        )

    @app.get("/trades", response_class=HTMLResponse)
    def trades_page(request: Request, user=Depends(current_admin)):
        return templates.TemplateResponse(
            request, "trades.html", page_context(request, user, "trades")
        )

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, user=Depends(current_admin)):
        return templates.TemplateResponse(
            request, "accounts.html", page_context(request, user, "accounts")
        )

    @app.get("/system", response_class=HTMLResponse)
    def system_page(request: Request, user=Depends(current_admin)):
        return templates.TemplateResponse(
            request, "system.html", page_context(request, user, "system")
        )

    @app.get("/api/overview")
    def overview_api(request: Request, user=Depends(current_admin)):
        return build_overview(request.app.state.engine, viewer=user)

    @app.get("/api/market")
    def market_api(
        request: Request,
        venue: str = Query(default="GATE_LIVE_PUBLIC"),
        symbol: str = Query(default="ETH_USDT"),
        interval: str = Query(default="15m"),
        _user=Depends(current_admin),
    ):
        try:
            return build_market_chart(
                request.app.state.engine,
                venue=venue,
                symbol=symbol,
                interval=interval,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/quotes")
    def quotes_api(
        request: Request,
        venue: str = Query(default="GATE_LIVE_PUBLIC"),
        symbol: str = Query(default="ETH_USDT"),
        _user=Depends(current_admin),
    ):
        # 盘口是公开行情，不含账户信息，因此不按 viewer 过滤。
        return build_live_quotes(request.app.state.engine, venue=venue, symbol=symbol)

    @app.get("/api/trades")
    def trades_api(
        request: Request,
        venue: str | None = None,
        symbol: str | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        user=Depends(current_admin),
    ):
        return build_trade_events(
            request.app.state.engine,
            viewer=user,
            venue=venue,
            symbol=symbol,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/system")
    def system_api(_user=Depends(current_admin)):
        return build_system_status()

    return app
