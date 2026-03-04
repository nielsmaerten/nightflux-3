"""HTTP + WebSocket server for the slidedeck browser client."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import aiohttp.web as web

from .state import ASSETS_DIR, DeckState

logger = logging.getLogger("slidedeck.web")

CLIENT_HTML = Path(__file__).parent / "client.html"

# Connected WebSocket clients
_ws_clients: set[web.WebSocketResponse] = set()

# Reference to deck state (set by server.py lifespan)
_deck: DeckState | None = None


def set_deck(deck: DeckState) -> None:
    global _deck
    _deck = deck


def get_deck() -> DeckState:
    assert _deck is not None, "Deck state not initialized"
    return _deck


async def broadcast(msg_type: str, data: dict) -> None:
    """Push a message to all connected WebSocket clients."""
    payload = json.dumps({"type": msg_type, "data": data})
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_str(payload)
        except (ConnectionResetError, ConnectionError):
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ── HTTP handlers ──

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(
        text=CLIENT_HTML.read_text(),
        content_type="text/html",
    )


async def handle_asset(request: web.Request) -> web.FileResponse:
    filename = request.match_info["filename"]
    path = ASSETS_DIR / filename
    if not path.exists() or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _ws_clients.add(ws)
    logger.info("WebSocket client connected (%d total)", len(_ws_clients))

    # Send full sync on connect
    deck = get_deck()
    await ws.send_str(json.dumps({
        "type": "deck:sync",
        "data": deck.to_dict(),
    }))

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    parsed = json.loads(msg.data)
                    if parsed.get("type") == "slide:viewed":
                        slide_id = parsed.get("data", {}).get("id")
                        if slide_id and deck.get_slide(slide_id):
                            deck.current_slide_id = slide_id
                            deck.save()
                except (json.JSONDecodeError, KeyError):
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                logger.warning("WS error: %s", ws.exception())
    finally:
        _ws_clients.discard(ws)
        logger.info("WebSocket client disconnected (%d total)", len(_ws_clients))

    return ws


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/assets/{filename}", handle_asset)
    app.router.add_get("/ws", handle_ws)
    return app


async def start_server(host: str = "127.0.0.1", port: int = 8765) -> tuple[web.AppRunner, web.TCPSite]:
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Slidedeck server running at http://%s:%d", host, port)
    return runner, site


async def stop_server(runner: web.AppRunner) -> None:
    await runner.cleanup()
    logger.info("Slidedeck server stopped")
