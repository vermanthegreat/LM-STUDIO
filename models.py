"""Data model constants for the lead intelligence app."""

from __future__ import annotations

SOURCE_TYPES = (
    "shopify_directory",
    "linkedin_company",
    "linkedin_person",
    "website",
    "email",
    "note",
)

LEAD_STATUSES = ("new", "researching", "qualified", "contacted", "active", "closed", "archived")

EXTRACTION_STATUSES = ("ok", "needs_review", "fallback")

DECISION_MAKER_TITLES = (
    "founder",
    "co-founder",
    "cofounder",
    "ceo",
    "managing partner",
    "partner",
    "head of ecommerce",
    "vp ecommerce",
    "director of ecommerce",
    "head of shopify",
    "solutions architect",
    "technical director",
    "growth lead",
    "partnerships",
    "client success",
    "operations",
)

RELEVANT_TITLE_KEYWORDS = DECISION_MAKER_TITLES + (
    "ecommerce",
    "e-commerce",
    "shopify",
    "cto",
    "coo",
    "president",
    "owner",
)
