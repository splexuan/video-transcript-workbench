import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.credentials import router as credentials_router
from app.api.models import router as models_router
from app.api.router import router
from app.api.tts import router as tts_router
from app.application import tts_jobs
from app.application.worker import local_worker
from app.config import settings
from app.infrastructure.database import init_database


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_database()
    # 合成记录只存在内存里，音频却落盘；启动时清掉上次会话的遗留，
    # 否则每次重启都会留下一批再也引用不到的文件。
    tts_jobs.manager.sweep_orphans()
    if settings.worker_enabled:
        local_worker.start()
    try:
        yield
    finally:
        if settings.worker_enabled:
            local_worker.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.dev_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)
app.include_router(router)
app.include_router(models_router)
app.include_router(credentials_router)
app.include_router(tts_router)

def _find_frontend_dist() -> Path | None:
    """前端静态资源：打包版在资源目录里，开发版在仓库的 frontend/dist。"""

    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        packaged = base / "frontend_dist"
        if packaged.is_dir():
            return packaged
        side_by_side = Path(sys.executable).parent / "frontend_dist"
        if side_by_side.is_dir():
            return side_by_side
    candidate = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    return candidate if candidate.is_dir() else None


frontend_dist = _find_frontend_dist()
if frontend_dist is not None:
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
