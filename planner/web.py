"""Run the bot as a long-lived web service so Fly can health-check it."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .bot import build_application

logger = logging.getLogger(__name__)


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
