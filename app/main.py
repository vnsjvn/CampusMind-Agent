from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.tool_queue import get_tool_queue_worker
from app.services.operational_alerts import OperationalAlertMonitor


def create_app() -> FastAPI:
    app = FastAPI(title="MindBridge Python", version="0.1.0")

    @app.middleware("http")
    async def no_cache_frontend_assets(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.on_event("startup")
    def startup() -> None:
        create_schema()
        db = SessionLocal()
        try:
            seed_data(db)
        finally:
            db.close()
        settings = get_settings()
        worker = get_tool_queue_worker(settings)
        worker.start()
        app.state.tool_queue_worker = worker
        alert_monitor = OperationalAlertMonitor(settings, SessionLocal)
        alert_monitor.start()
        app.state.operational_alert_monitor = alert_monitor

    @app.on_event("shutdown")
    def shutdown() -> None:
        worker = getattr(app.state, "tool_queue_worker", None)
        if worker is not None:
            worker.stop()
        alert_monitor = getattr(app.state, "operational_alert_monitor", None)
        if alert_monitor is not None:
            alert_monitor.stop()

    app.include_router(router)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()
