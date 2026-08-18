"""Run the bot as a long-lived web service so Fly can health-check it."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import google
from .bot import build_application

logger = logging.getLogger(__name__)

PAGE = (
    "<html><body style='font-family:sans-serif;padding:3rem'>"
    "<h2>{title}</h2><p>{body}</p></body></html>"
)


def database_path() -> str:
    """Prefer the mounted volume so plans survive a redeploy."""
    default = "/data/planner.sqlite3" if os.path.isdir("/data") else "planner.sqlite3"
    return os.environ.get("PLANNER_DB", default)


@asynccontextmanager
async def lifespan(app: FastAPI):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN before starting the service.")
    application = build_application(token, database_path())
    app.state.application = application
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Planner bot polling")
    try:
        yield
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    """Where Google sends the person back after they allow access."""
    application = app.state.application
    storage = application.bot_data["storage"]
    if error or not code or not state:
        return HTMLResponse(
            PAGE.format(title="Not connected", body=error or "Google sent no code back."),
            status_code=400,
        )
    link = storage.take_google_link(state)
    if link is None:
        return HTMLResponse(
            PAGE.format(title="Link expired", body="Send /connect in the chat again."),
            status_code=400,
        )
    user_id, chat_id = link
    try:
        email = await google.finish_consent(storage, user_id, code)
    except google.GoogleError as exc:
        return HTMLResponse(PAGE.format(title="Not connected", body=str(exc)), status_code=400)
    await application.bot.send_message(
        chat_id,
        f"✅ Connected to {email or 'your Google account'} — tell me what you need, "
        "e.g. “meet Ada tomorrow 3pm with a Meet link and email her the contract”.",
    )
    return HTMLResponse(
        PAGE.format(title="Connected", body="You can close this and go back to Telegram.")
    )
