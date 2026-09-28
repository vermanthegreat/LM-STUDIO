"""FastAPI web app for manual copy/paste lead intelligence."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError as PydanticValidationError

import db
from knowledge import repository as knowledge_repo
from knowledge.ingestion import (
    KnowledgeRuntimeUnsupportedError,
    UploadedFile,
    build_ingestor,
    require_knowledge_sqlite_runtime,
)
from knowledge.retrieval import search_knowledge
from knowledge.schemas import SearchKnowledgeInput
from knowledge.storage import OriginalFileStore

from ask_router import (
    answer_question,
    apply_write_proposal_route,
    approve_write_proposal_route,
    get_write_proposal_detail_route,
    list_pending_write_proposals_route,
)
from config import AppConfig
from errors import AppError, ValidationError, parse_command_id
from gmail_runtime import GmailRuntimeUnsupportedError
from extractor import parse_and_save
from intake import validate_parse_intake
from repositories.factory import get_contact_store
from security import assert_safe_mutation_request
from services.gmail_sync_service import gmail_integration_status, sync_gmail_label

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
            request,
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
            request,
            "index.html",
            {"request": request, "leads": leads, "message": msg},
        )

    @application.get("/leads", response_class=HTMLResponse)
    def leads_list(request: Request):
        leads = request.app.state.store.list_leads()
        return templates.TemplateResponse(
            request,
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
            request,
            "lead_detail.html",
            {"request": request, "lead": lead, "message": msg},
        )

    @application.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request):
        return templates.TemplateResponse(
            request,
            "ask.html",
            {"request": request, "result": None},
        )

    @application.post("/ask")
    async def ask_submit(request: Request):
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type == "application/json":
            assert_safe_mutation_request(request, port=cfg.port)
            body = await request.json()
            result = answer_question(
                str(body.get("question") or ""),
                use_llm=bool(body.get("use_llm", False)),
                store=request.app.state.store,
                planner_payload=body.get("planner_payload"),
            )
            return JSONResponse(content=result)
        form = await request.form()
        question = str(form.get("question") or "")
        use_llm = str(form.get("use_llm", "")).lower() in ("true", "1", "on", "yes")
        assert_safe_mutation_request(request, port=cfg.port)
        result = answer_question(question, use_llm=use_llm, store=request.app.state.store)
        return templates.TemplateResponse(
            request,
            "ask.html",
            {"request": request, "result": result},
        )

    @application.post("/ask/commands/{command_id}/approve")
    def ask_approve_command(request: Request, command_id: str):
        assert_safe_mutation_request(request, port=cfg.port)
        result = approve_write_proposal_route(
            parse_command_id(command_id),
            store=request.app.state.store,
        )
        status_code = 200 if result["status"] == "ok" else 409
        return JSONResponse(status_code=status_code, content=result)

    @application.post("/ask/commands/{command_id}/apply")
    def ask_apply_command(request: Request, command_id: str):
        assert_safe_mutation_request(request, port=cfg.port)
        result = apply_write_proposal_route(
            parse_command_id(command_id),
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
        result = get_write_proposal_detail_route(
            parse_command_id(command_id),
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

    @application.get("/integrations/gmail", response_class=HTMLResponse)
    def gmail_integration_page(request: Request):
        status = gmail_integration_status(request.app.state.store, cfg)
        return templates.TemplateResponse(
            request,
            "gmail_integration.html",
            {"request": request, "status": status, "message": None},
        )

    @application.post("/integrations/gmail/sync", response_class=HTMLResponse)
    def gmail_sync(request: Request):
        assert_safe_mutation_request(request, port=cfg.port)
        result, entry = sync_gmail_label(request.app.state.store, cfg)
        status = gmail_integration_status(request.app.state.store, cfg)
        if result.status == "ok":
            counts = result.counts
            msg = (
                f"Gmail sync complete: imported={counts.imported}, updated={counts.updated}, "
                f"already_present={counts.already_present}, failed={counts.failed}. "
                f"Command: {entry.id if entry else 'n/a'}"
            )
        else:
            msg = result.message or result.error_code or "Gmail sync failed."
        return templates.TemplateResponse(
            request,
            "gmail_integration.html",
            {"request": request, "status": status, "message": msg},
        )

    @application.get("/emails", response_class=HTMLResponse)
    def emails_list(
        request: Request,
        intent: str = "",
        marker: str = "",
        direction: str = "",
        link_status: str = "",
        since: str = "",
        limit: int = 50,
    ):
        store = request.app.state.store
        runtime_error: str | None = None
        records: list = []
        total = 0
        try:
            records, total = store.list_imported_email_messages(
                intent=intent or None,
                marker=marker or None,
                direction=direction or None,
                link_status=link_status or None,
                since=since or None,
                limit=min(max(limit, 1), 100),
                app_timezone=cfg.app_timezone,
            )
        except GmailRuntimeUnsupportedError as exc:
            runtime_error = exc.message
        return templates.TemplateResponse(
            request,
            "emails.html",
            {
                "request": request,
                "emails": records,
                "total": total,
                "runtime_error": runtime_error,
                "filters": {
                    "intent": intent,
                    "marker": marker,
                    "direction": direction,
                    "link_status": link_status,
                    "since": since,
                    "limit": limit,
                },
            },
        )

    @application.get("/emails/thread/{thread_id}", response_class=HTMLResponse)
    def email_thread_detail(request: Request, thread_id: str, account: str = ""):
        runtime_error: str | None = None
        messages: list = []
        try:
            messages = request.app.state.store.get_imported_email_thread(
                thread_id,
                external_account=account or None,
                app_timezone=cfg.app_timezone,
            )
        except GmailRuntimeUnsupportedError as exc:
            runtime_error = exc.message
        return templates.TemplateResponse(
            request,
            "email_thread.html",
            {
                "request": request,
                "thread_id": thread_id,
                "messages": messages,
                "runtime_error": runtime_error,
            },
        )

    # ------------------------------------------------------------ knowledge

    def _knowledge_db_path(request: Request) -> Path:
        try:
            return require_knowledge_sqlite_runtime(request.app.state.store)
        except KnowledgeRuntimeUnsupportedError as exc:
            raise ValidationError(error_code=exc.error_code, message=exc.message, status_code=501) from None

    def _knowledge_search_params(request: Request) -> SearchKnowledgeInput:
        raw = {k: v for k, v in request.query_params.items() if str(v).strip() != ""}
        if "q" in raw:
            raw["query"] = raw.pop("q")
        try:
            return SearchKnowledgeInput.model_validate(raw)
        except PydanticValidationError as exc:
            fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
            raise ValidationError(
                error_code="invalid_knowledge_search",
                message="Invalid search parameters: " + ", ".join(fields),
                status_code=422,
            ) from None

    @application.get("/knowledge", response_class=HTMLResponse)
    def knowledge_page(request: Request):
        db_path = _knowledge_db_path(request)
        with db.get_conn(db_path) as conn:
            items, total = knowledge_repo.list_recent(conn, limit=50)
            facets = knowledge_repo.facet_counts(conn)
        return templates.TemplateResponse(
            request,
            "knowledge.html",
            {
                "request": request,
                "items": items,
                "total": total,
                "facets": facets,
                "vision_enabled": bool(cfg.knowledge_vision_model),
                "max_upload_mb": cfg.knowledge_max_upload_bytes // (1024 * 1024),
            },
        )

    @application.post("/knowledge/ingest")
    async def knowledge_ingest(
        request: Request,
        files: list[UploadFile] = File(...),
        project: str = Form(""),
    ):
        assert_safe_mutation_request(request, port=cfg.port)
        _knowledge_db_path(request)
        uploads: list[UploadedFile] = []
        limit = cfg.knowledge_max_upload_bytes
        for upload in files:
            # Read at most limit+1 bytes so oversize files are rejected without full buffering.
            data = await upload.read(limit + 1)
            uploads.append(UploadedFile(filename=upload.filename or "upload", data=data))
            await upload.close()
        ingestor = build_ingestor(request.app.state.store, cfg)
        result = await run_in_threadpool(ingestor.ingest_batch, uploads, project=project or None)
        status_code = 200 if result.status != "error" else 422
        return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))

    @application.get("/api/knowledge/items")
    def knowledge_items_api(request: Request, limit: int = 50, offset: int = 0):
        db_path = _knowledge_db_path(request)
        with db.get_conn(db_path) as conn:
            items, total = knowledge_repo.list_recent(
                conn, limit=min(max(limit, 1), 200), offset=max(offset, 0)
            )
        return JSONResponse(content={"total": total, "items": items})

    @application.get("/api/knowledge/items/{item_id}")
    def knowledge_item_api(request: Request, item_id: int):
        db_path = _knowledge_db_path(request)
        with db.get_conn(db_path) as conn:
            item = knowledge_repo.get_item(conn, item_id, include_text=True)
            related = knowledge_repo.related_items(conn, item_id) if item else []
        if item is None:
            return JSONResponse(
                status_code=404,
                content={"error_code": "knowledge_item_not_found", "message": f"Knowledge item {item_id} was not found."},
            )
        return JSONResponse(content={**item, "related": related})

    @application.get("/api/knowledge/search")
    def knowledge_search_api(request: Request):
        db_path = _knowledge_db_path(request)
        params = _knowledge_search_params(request)
        return JSONResponse(content=search_knowledge(db_path, params))

    @application.get("/knowledge/items/{item_id}", response_class=HTMLResponse)
    def knowledge_item_page(request: Request, item_id: int):
        db_path = _knowledge_db_path(request)
        with db.get_conn(db_path) as conn:
            item = knowledge_repo.get_item(conn, item_id, include_text=True)
            related = knowledge_repo.related_items(conn, item_id) if item else []
        if item is None:
            raise ValidationError(
                error_code="knowledge_item_not_found",
                message=f"Knowledge item {item_id} was not found.",
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "knowledge_item.html",
            {"request": request, "item": item, "related": related},
        )

    @application.get("/knowledge/items/{item_id}/original")
    def knowledge_item_original(request: Request, item_id: int):
        db_path = _knowledge_db_path(request)
        with db.get_conn(db_path) as conn:
            item = knowledge_repo.get_item(conn, item_id, include_text=False)
        if item is None:
            raise ValidationError(
                error_code="knowledge_item_not_found",
                message=f"Knowledge item {item_id} was not found.",
                status_code=404,
            )
        file_store = OriginalFileStore(cfg.knowledge_storage_dir)
        try:
            path = file_store.resolve(item["source_path"])
        except ValueError:
            raise ValidationError(error_code="knowledge_original_invalid_path", message="Invalid stored path.", status_code=500) from None
        if not path.is_file():
            raise ValidationError(
                error_code="knowledge_original_missing",
                message=f"Original file for knowledge item {item_id} is missing from storage.",
                status_code=404,
            )
        # Always download; never render uploaded HTML/SVG inline in the app origin.
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=item["original_filename"],
            headers={"X-Content-Type-Options": "nosniff"},
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
