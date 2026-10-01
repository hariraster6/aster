import reflex as rx

import asyncio
import logging
from http.cookies import SimpleCookie

from starlette.types import ASGIApp, Message, Receive, Scope, Send

import main as panel


# The legacy page is kept intact, including its CSS, navigation, and API calls.
# Chart.js is optional: a blocked CDN must not stop the rest of the dashboard.
panel.PANEL_HTML = panel.PANEL_HTML.replace(
    "function initChart(){\nconst ctx=$m('tc');",
    "function initChart(){\nif(typeof Chart==='undefined')return;\nconst ctx=$m('tc');",
)
# The panel cookie is HttpOnly; JavaScript cannot (and should not) read it.
panel.PANEL_HTML = panel.PANEL_HTML.replace(
    "const token=document.cookie.split('; ').find(r=>r.startsWith('ren_session='))?.split('=')[1];\nif(!token)return;\nlogsWS=new WebSocket(`${protocol}//${location.host}/ws/live-logs?token=${token}`);",
    "logsWS=new WebSocket(`${protocol}//${parent.location.host}/ws/live-logs`);",
)
panel.PANEL_HTML = panel.PANEL_HTML.replace(
    "const protocol=location.protocol==='https:'?'wss:':'ws:';",
    "const protocol=parent.location.protocol==='https:'?'wss:':'ws:';",
)
panel.PANEL_HTML = panel.PANEL_HTML.replace(
    "'https://'+location.host+'/sub/'",
    "'https://'+parent.location.host+'/sub/'",
)


_panel_lifecycle_lock = asyncio.Lock()
_panel_started = False
_panel_stopped = False


async def _start_panel() -> None:
    global _panel_started
    if _panel_started:
        return
    async with _panel_lifecycle_lock:
        if _panel_started:
            return
        try:
            await panel.startup()
        except Exception as e:
            logging.exception(f"Error: {e}")
            raise
        _panel_started = True


async def _stop_panel() -> None:
    global _panel_stopped
    async with _panel_lifecycle_lock:
        if not _panel_started or _panel_stopped:
            return
        try:
            await panel.shutdown()
        except Exception as e:
            logging.exception(f"Error: {e}")
            raise
        _panel_stopped = True


# If the panel itself receives lifespan events, route those through the same
# guard as the hosted backend instead of running the original handlers twice.
panel.app.router.on_startup[
    panel.app.router.on_startup.index(panel.startup)
] = _start_panel
panel.app.router.on_shutdown[
    panel.app.router.on_shutdown.index(panel.shutdown)
] = _stop_panel


def panel_backend_lifecycle(application: ASGIApp) -> ASGIApp:
    async def with_lifecycle(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "lifespan":
            await application(scope, receive, send)
            return

        async def send_with_lifecycle(message: Message) -> None:
            if message["type"] == "lifespan.startup.complete":
                await _start_panel()
            elif message["type"] == "lifespan.shutdown.complete":
                await _stop_panel()
            await send(message)

        await application(scope, receive, send_with_lifecycle)

    return with_lifecycle


def panel_ws_cookie_auth(application: ASGIApp) -> ASGIApp:
    panel_paths = (
        "/api",
        "/stats",
        "/sub",
        "/ws",
        "/xhttp",
        "/health",
        "/client",
        "/login",
        "/dashboard",
        "/panel",
    )
    reflex_api_paths = ("/api/_event", "/api/_upload", "/api/ping")

    async def with_cookie_auth(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] in ("http", "websocket") and not _panel_started:
            path = scope["path"]
            if any(
                path == prefix or path.startswith(f"{prefix}/")
                for prefix in panel_paths
            ) and not any(
                path == prefix or path.startswith(f"{prefix}/")
                for prefix in reflex_api_paths
            ):
                await _start_panel()
        if scope["type"] == "websocket" and scope["path"] == "/ws/live-logs":
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin", b"").decode("latin-1")
            host = headers.get(b"host", b"").decode("latin-1")
            cookies = SimpleCookie()
            cookies.load(headers.get(b"cookie", b"").decode("latin-1"))
            session = cookies.get(panel.SESSION_COOKIE)
            if session and origin in (f"http://{host}", f"https://{host}"):
                scope = dict(scope)
                scope["query_string"] = f"token={session.value}".encode("ascii")
        await application(scope, receive, send)

    return with_cookie_auth


def panel_surface() -> rx.Component:
    return rx.el.main(
        rx.el.iframe(
            src_doc=panel.PANEL_HTML,
            title="Luffy Panel",
            class_name="block h-dvh w-full border-0 bg-[#090909]",
            style={
                "width": "100%",
                "height": "100dvh",
                "min_height": "100vh",
                "border": "0",
                "display": "block",
                "background_color": "#090909",
            },
        ),
        class_name="h-dvh w-full overflow-hidden bg-[#090909]",
        style={
            "width": "100%",
            "height": "100dvh",
            "min_height": "100vh",
            "border": "0",
            "display": "block",
            "margin": "0",
            "padding": "0",
            "overflow": "hidden",
            "background_color": "#090909",
        },
    )


def index() -> rx.Component:
    return panel_surface()


# The original FastAPI app owns all panel, API, client, tunnel, and health routes.
# Reflex mounts its internal event/upload API on this app in the same ASGI process.
app = rx.App(
    api_transformer=[panel.app, panel_ws_cookie_auth, panel_backend_lifecycle],
    theme=rx.theme(appearance="light"),
    style={
        "html, body, #__next, #root, .radix-themes": {
            "margin": "0",
            "padding": "0",
            "width": "100%",
            "min_height": "100vh",
            "background_color": "#090909",
        },
    },
)
app.add_page(index, route="/")
app.add_page(panel_surface, route="/login")
app.add_page(panel_surface, route="/dashboard")
app.add_page(panel_surface, route="/panel")
