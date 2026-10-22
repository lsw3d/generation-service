import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers import generate, queries, webhooks
from app.watchdog import watchdog_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(watchdog_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="generation-service", lifespan=lifespan)
app.include_router(generate.router)
app.include_router(webhooks.router)
app.include_router(queries.router)


@app.get("/health")
async def health():
    return {"ok": True}
