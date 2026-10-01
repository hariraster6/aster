import reflex as rx

import asyncio
import json
import logging
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import main as panel


_config = rx.config.get_config()
_api_origin = _config.api_url.rstrip("/")
_frontend_origin = _config.deploy_url.rstrip("/")
_api_parts = urlsplit(_api_origin)
_frontend_parts = urlsplit(_frontend_origin)
_cross_site_https = (
    _api_parts.scheme == "https"
    and _frontend_parts.scheme == "https"
    and _api_parts.hostname != _frontend_parts.hostname
)


def _js_string(value: str) -> str:
    return json.dumps(value).replace("<", "\\u003c")


# Only the srcdoc copy redirects panel requests; the standalone FastAPI HTML is unchanged.
_embedded_fetch = """<script>
(() => {
  const apiOrigin = __API_ORIGIN__;
  const nativeFetch = window.fetch.bind(window);
  const panelPath = /^\\/(?:api|stats|sub|health|client|xhttp|ws)(?:\\/|$)/;
  window.fetch = (input, init) => {
    const url = new URL(input instanceof Request ? input.url : input, parent.location.href);
    if (url.origin === parent.location.origin && panelPath.test(url.pathname)) {
      const target = new URL(url.pathname + url.search + url.hash, apiOrigin);
      input = input instanceof Request ? new Request(target, input) : target.href;
    }
    return nativeFetch(input, {...init, credentials: 'include'});
  };
})();
</script>
""".replace("__API_ORIGIN__", _js_string(_api_origin))

_embedded_html = panel.PANEL_HTML.replace(
    "<script>\nfunction $(s)", f"{_embedded_fetch}<script>\nfunction $(s)", 1
).replace(
    "function initChart(){\nconst ctx=$m('tc');",
    "function initChart(){\nif(typeof Chart==='undefined')return;\nconst ctx=$m('tc');",
)
_legacy_logs_ws = """function connectLogsWS(){
  if(logsWS){try{logsWS.close()}catch(e){}}
  const protocol=location.protocol==='https:'?'wss:':'ws:';
  const token=document.cookie.split('; ').find(r=>r.startsWith('ren_session='))?.split('=')[1];
  if(!token)return;
  logsWS=new WebSocket(`${protocol}//${location.host}/ws/live-logs?token=${token}`);"""
assert _embedded_html.count(_legacy_logs_ws) == 1, (
    "Legacy connectLogsWS setup not found exactly once"
)
_embedded_html = _embedded_html.replace(
    _legacy_logs_ws,
    """function connectLogsWS(){
  if(logsWS){try{logsWS.close()}catch(e){}}
  const wsUrl=new URL('/ws/live-logs', __API_ORIGIN__);
  wsUrl.protocol=wsUrl.protocol==='https:'?'wss:':'ws:';
  logsWS=new WebSocket(wsUrl.href);""".replace(
        "__API_ORIGIN__", _js_string(_api_origin)
    ),
    1,
).replace(
    "'https://'+location.host+'/sub/'",
    _js_string(_api_origin) + "+'/sub/'",
)


def panel_embedded_cors(application: ASGIApp) -> ASGIApp:
    return CORSMiddleware(
        application,
        allow_origins=[_frontend_origin, _api_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def panel_session_cookies(application: ASGIApp) -> ASGIApp:
    async def with_session_cookies(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http" or scope["path"] not in (
            "/api/login",
            "/api/logout",
        ):
            await application(scope, receive, send)
            return

        async def send_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"set-cookie" and value.startswith(
                        f"{panel.SESSION_COOKIE}=".encode("ascii")
                    ):
                        cookie = value.decode("latin-1")
                        if _cross_site_https:
                            cookie = cookie.replace(
                                "SameSite=lax", "SameSite=none"
                            )
                        if (
                            _api_parts.scheme == "https"
                            and "Secure" not in cookie
                        ):
                            cookie = f"{cookie}; Secure"
                        value = cookie.encode("latin-1")
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await application(scope, receive, send_cookie)

    return with_session_cookies


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
            allowed_origins = {_frontend_origin, _api_origin}
            if _api_parts.hostname == "localhost":
                allowed_origins.add("http://localhost:8080")
            if origin not in allowed_origins:
                await send({"type": "websocket.close", "code": 1008})
                return
            cookies = SimpleCookie()
            cookies.load(headers.get(b"cookie", b"").decode("latin-1"))
            session = cookies.get(panel.SESSION_COOKIE)
            if session:
                scope = dict(scope)
                scope["query_string"] = f"token={session.value}".encode("ascii")
        await application(scope, receive, send)

    return with_cookie_auth


def panel_surface() -> rx.Component:
    return rx.el.main(
        rx.el.iframe(
            src_doc=_embedded_html,
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
    api_transformer=[
        panel.app,
        panel_ws_cookie_auth,
        panel_backend_lifecycle,
        panel_session_cookies,
        panel_embedded_cors,
    ],
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
