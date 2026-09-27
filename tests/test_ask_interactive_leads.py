from __future__ import annotations

import db
from ask_router import answer_question, _contact_evidence_status
from config import AppConfig
from fastapi.testclient import TestClient
from repositories.sqlite_store import SqliteContactStore

from app import create_app


def _seed(path):
    db.init_db(path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Acme Agency",
            "website": "https://acme.example",
            "company_email": "hello@acme.example",
            "description": "A commerce delivery agency.",
            "services": ["SEO", "Product descriptions"],
            "industries": ["Retail"],
            "primary_location": "London",
            "fit_score": 91,
            "status": "new",
        },
        db_path=path,
    )
    db.upsert_lead({"company_name": "Sparse Agency", "fit_score": 20}, db_path=path)
    return lead["id"]


def test_top_leads_render_interactive_rows_and_optional_fields(tmp_path):
    path = tmp_path / "ask.db"
    _seed(path)
    client = TestClient(create_app(AppConfig(database_path=path, max_paste_chars=1000, port=8025)), base_url="http://127.0.0.1:8025")

    with client:
        response = client.post("/ask", data={"question": "show top leads", "use_llm": "false"}, headers={"Origin": "http://127.0.0.1:8025"})

    html = response.text
    assert response.status_code == 200
    assert html.count("class=\"lead-result\"") == 2
    assert "/leads/1" in html
    assert html.count("Open agency profile") == 2
    assert "Acme Agency" in html
    assert "A commerce delivery agency." in html
    assert "SEO, Product descriptions" in html
    assert "Retail" in html
    assert "Fit: 91" in html
    assert "Status: new" in html
    assert 'href="https://acme.example" target="_blank" rel="noopener noreferrer"' in html
    assert "No contact evidence" in html
    assert "hello@acme.example" in html
    assert '<pre class="answer">' not in html


def test_lead_result_contact_status_does_not_use_email_and_uses_direction(tmp_path):
    path = tmp_path / "ask.db"
    lead_id = _seed(path)

    class DirectionalStore(SqliteContactStore):
        def list_imported_email_messages(self, **kwargs):
            return ([
                {"direction": "outbound", "occurred_at": "2026-01-01T10:00:00+00:00"},
                {"direction": "inbound", "occurred_at": "2026-01-02T10:00:00+00:00"},
            ], 2)

    store = DirectionalStore(path)
    lead = store.get_lead(lead_id)
    assert lead is not None
    assert _contact_evidence_status(lead, store=store) == "Replied"


def test_non_lead_answer_keeps_text_fallback_and_empty_leads_are_clear(tmp_path):
    path = tmp_path / "ask.db"
    _seed(path)
    store = SqliteContactStore(path)
    count_result = answer_question("how many companies", use_llm=False, store=store)
    assert count_result["data"] == {"count": 2}
    assert count_result["answer"]

    empty_path = tmp_path / "empty.db"
    db.init_db(empty_path)
    client = TestClient(create_app(AppConfig(database_path=empty_path, max_paste_chars=1000, port=8025)), base_url="http://127.0.0.1:8025")
    with client:
        response = client.post("/ask", data={"question": "companies without email", "use_llm": "false"}, headers={"Origin": "http://127.0.0.1:8025"})
    assert response.status_code == 200
    assert "No matching agencies found." in response.text
