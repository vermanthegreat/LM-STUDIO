"""FastAPI web app for manual copy/paste lead intelligence."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
from ask_router import answer_question
from extractor import parse_and_save

load_dotenv()

BASE_DIR = Path(__file__).parent
app = FastAPI(title="LM Studio Lead Intelligence")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.on_event("startup")
def startup():
    db.init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    leads = db.get_all_leads_simple()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "leads": leads, "message": None},
    )


@app.post("/parse", response_class=HTMLResponse)
def parse_paste(
    request: Request,
    raw_text: str = Form(...),
    source_type: str = Form("shopify_directory"),
    source_url: str = Form(""),
    attach_to_lead_id: str = Form(""),
):
    lead_id = int(attach_to_lead_id) if attach_to_lead_id.strip().isdigit() else None
    result = parse_and_save(
        source_type=source_type,
        raw_text=raw_text,
        source_url=source_url or None,
        attach_to_lead_id=lead_id,
    )
    leads = db.get_all_leads_simple()
    msg = (
        f"Saved (status={result['extraction_status']}, lead_id={result['lead_id']}, "
        f"people={result['people_count']})"
    )
    if result["lead_id"]:
        return RedirectResponse(url=f"/leads/{result['lead_id']}?msg={msg}", status_code=303)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "leads": leads, "message": msg},
    )


@app.get("/leads", response_class=HTMLResponse)
def leads_list(request: Request):
    leads = db.list_leads()
    return templates.TemplateResponse(
        "leads.html",
        {"request": request, "leads": leads},
    )


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: int, msg: str = ""):
    lead = db.get_lead(lead_id)
    if not lead:
        return RedirectResponse("/leads", status_code=302)
    return templates.TemplateResponse(
        "lead_detail.html",
        {"request": request, "lead": lead, "message": msg},
    )


@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request):
    return templates.TemplateResponse(
        "ask.html",
        {"request": request, "result": None},
    )


@app.post("/ask", response_class=HTMLResponse)
def ask_submit(request: Request, question: str = Form(...), use_llm: bool = Form(False)):
    result = answer_question(question, use_llm=use_llm)
    return templates.TemplateResponse(
        "ask.html",
        {"request": request, "result": result},
    )


@app.get("/export/csv")
def export_csv():
    csv_data = db.export_leads_csv()
    return PlainTextResponse(
        csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_export.csv"},
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8025"))
    uvicorn.run("app:app", host="127.0.0.1", port=port, reload=False)
