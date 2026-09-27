"""Regression: Shopify fallback parse -> source -> company detail contact projection."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from extractor import deterministic_parse, parse_and_save
from persistence.models import Base, Organization, Source
from persistence.session import get_engine, reset_cached_engines
from repositories.mapping import organization_to_lead_detail
from repositories.postgres_store import PostgresContactStore
from sqlalchemy import text
from tests.pg_support import run_alembic_upgrade

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "shopvert_shopify.txt"
SHOPVERT_TEXT = FIXTURE_PATH.read_text(encoding="utf-8")


def test_shopify_fallback_detail_projects_parsed_contacts_without_contact_methods():
    fallback = deterministic_parse("shopify_directory", SHOPVERT_TEXT)
    org = Organization(name="Shopvert", legacy_lead_id=1)
    source = Source(
        source_type="shopify_directory",
        raw_text=SHOPVERT_TEXT,
        legacy_source_id=1,
        legacy_metadata={
            "lead_id": 1,
            "parsed_json": fallback,
            "extraction_status": "fallback",
            "confidence": fallback["confidence"],
        },
    )
    org._loaded_sources = [source]  # type: ignore[attr-defined]

    detail = organization_to_lead_detail(org)

    assert detail["company_email"] == fallback["company_email"]
    assert detail["company_phone"] == fallback["company_phone"]
    assert detail["canonical_source"]["parsed_json"]["company_email"] == fallback["company_email"]


@pytest.fixture()
def pg_store():
    reset_cached_engines()
    run_alembic_upgrade(TEST_DATABASE_URL, "head")
    store = PostgresContactStore(TEST_DATABASE_URL)
    store.init_db()
    yield store
    engine = get_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
    reset_cached_engines()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
def test_shopify_fallback_parse_and_save_projects_contacts_on_detail(pg_store):
    fallback = deterministic_parse("shopify_directory", SHOPVERT_TEXT)
    llm_payload = {
        "company_name": "Shopvert",
        "company_email": "shecsar@gmail.com",
        "company_phone": "+1 555 000 0000",
        "people": [],
    }

    with patch("extractor.extract_structured", return_value=(llm_payload, "llm-raw")):
        result = parse_and_save(
            "shopify_directory",
            SHOPVERT_TEXT,
            source_url="https://partners.shopify.com/directory/partners/shopvert",
            store=pg_store,
        )

    assert result["lead_id"]
    detail = pg_store.get_lead(result["lead_id"])
    assert detail is not None

    parsed = (detail["canonical_source"] or {}).get("parsed_json") or {}
    assert parsed.get("company_email") == fallback["company_email"]
    assert parsed.get("company_phone") == fallback["company_phone"]
    assert "shecsar@gmail.com" not in (parsed.get("company_email") or "")

    assert detail["company_email"] == fallback["company_email"]
    assert detail["company_phone"] == fallback["company_phone"]
    assert "shecsar@gmail.com" not in (detail["company_email"] or "")
