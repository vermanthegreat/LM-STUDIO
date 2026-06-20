"""FastAPI web app for manual copy/paste lead intelligence."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ask_router import (
    answer_question,
    apply_write_proposal_route,
    approve_write_proposal_route,
    get_write_proposal_detail_route,
    list_pending_write_proposals_route,
)
from config import AppConfig
from errors import AppError, ValidationError
from extractor import parse_and_save
from intake import validate_parse_intake
from repositories.factory import get_contact_store
from security import assert_safe_mutation_request

load_dotenv()

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
logger = logging.getLogger(__name__)


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or AppConfig.from_env()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO))
    store = get_contact_store(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.init_db()
        app.state.store = store
        yield

    application = FastAPI(title="LM Studio Lead Intelligence", lifespan=lifespan)
    application.state.config = cfg
    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @application.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError):
        if exc.status_code == 404:
            return HTMLResponse(
                content=f"<h1>Not Found</h1><p>{exc.message}</p>",
                status_code=404,
            )
        if exc.status_code == 422:
            return JSONResponse(
                status_code=422,
                content={"error_code": exc.error_code, "message": exc.message},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": exc.error_code, "message": exc.message},
        )

    @application.get("/", response_class=HTMLResponse)
    def index(request: Request):
        leads = request.app.state.store.get_all_leads_simple()
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "leads": leads, "message": None},
        )

    @application.post("/parse", response_class=HTMLResponse)
    def parse_paste(
        request: Request,
        raw_text: str = Form(...),
        source_type: str = Form("shopify_directory"),
        source_url: str = Form(""),
        attach_to_lead_id: str = Form(""),
    ):
        assert_safe_mutation_request(request, port=cfg.port)
        intake = validate_parse_intake(
            source_type=source_type,
            raw_text=raw_text,
            source_url=source_url,
            attach_to_lead_id=attach_to_lead_id,
            max_paste_chars=cfg.max_paste_chars,
            store=request.app.state.store,
            database_path=cfg.database_path,
        )
        try:
            result = parse_and_save(
                source_type=intake.source_type,
                raw_text=intake.raw_text,
                source_url=intake.source_url,
                attach_to_lead_id=intake.attach_to_lead_id,
                store=request.app.state.store,
            )
        except Exception:
            logger.exception("parse_and_save failed")
            raise ValidationError(
                error_code="parse_failed",
                message="Failed to save pasted content. No changes were committed.",
                status_code=500,
            ) from None

        leads = request.app.state.store.get_all_leads_simple()
        msg = (
            f"Saved (status={result['extraction_status']}, lead_id={result['lead_id']}, "
            f"people={result['people_count']})"
        )
        if result["lead_id"]:
            return RedirectResponse(
                url=f"/leads/{result['lead_id']}?msg={quote(msg)}",
                status_code=303,
            )
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "leads": leads, "message": msg},
        )

    @application.get("/leads", response_class=HTMLResponse)
    def leads_list(request: Request):
        leads = request.app.state.store.list_leads()
        return templates.TemplateResponse(
            "leads.html",
            {"request": request, "leads": leads},
        )

    @application.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_detail(request: Request, lead_id: int, msg: str = ""):
        lead = request.app.state.store.get_lead(lead_id)
        if not lead:
            raise ValidationError(
                error_code="lead_not_found",
                message=f"Lead {lead_id} was not found.",
                status_code=404,
            )
        return templates.TemplateResponse(
            "lead_detail.html",
            {"request": request, "lead": lead, "message": msg},
        )

    @application.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request):
        return templates.TemplateResponse(
            "ask.html",
            {"request": request, "result": None},
        )

    @application.post("/ask", response_class=HTMLResponse)
    def ask_submit(request: Request, question: str = Form(...), use_llm: bool = Form(False)):
        assert_safe_mutation_request(request, port=cfg.port)
        result = answer_question(question, use_llm=use_llm, store=request.app.state.store)
        return templates.TemplateResponse(
            "ask.html",
            {"request": request, "result": result},
        )

    @application.post("/ask/commands/{command_id}/approve")
    def ask_approve_command(request: Request, command_id: str):
        assert_safe_mutation_request(request, port=cfg.port)
        from uuid import UUID

        result = approve_write_proposal_route(
            UUID(command_id),
            store=request.app.state.store,
        )
        status_code = 200 if result["status"] == "ok" else 409
        return JSONResponse(status_code=status_code, content=result)

    @application.post("/ask/commands/{command_id}/apply")
    def ask_apply_command(request: Request, command_id: str):
        assert_safe_mutation_request(request, port=cfg.port)
        from uuid import UUID

        result = apply_write_proposal_route(
            UUID(command_id),
            store=request.app.state.store,
        )
        status_code = 200 if result["status"] == "ok" else 409
        return JSONResponse(status_code=status_code, content=result)

    @application.get("/ask/commands/pending")
    def ask_list_pending_write_proposals(request: Request):
        result = list_pending_write_proposals_route(store=request.app.state.store)
        return JSONResponse(content=result)

    @application.get("/ask/commands/{command_id}")
    def ask_get_write_proposal_detail(request: Request, command_id: str):
        from uuid import UUID

        result = get_write_proposal_detail_route(
            UUID(command_id),
            store=request.app.state.store,
        )
        status_code = 200 if result["status"] == "ok" else 404
        return JSONResponse(status_code=status_code, content=result)

    @application.get("/export/csv")
    def export_csv(request: Request):
        csv_data = request.app.state.store.export_leads_csv()
        return PlainTextResponse(
            csv_data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=leads_export.csv"},
        )

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    main_config = AppConfig.from_env()
    uvicorn.run(
        "app:app",
        host=main_config.app_host,
        port=main_config.port,
        reload=False,
    )
