"""Map PostgreSQL entities to legacy lead-shaped dicts for templates and routes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from persistence.models import ContactMethod, Interaction, Organization, Person, Source, Task


def _meta(org: Organization) -> dict[str, Any]:
    return dict(org.legacy_metadata or {})


def organization_to_lead_row(org: Organization) -> Dict[str, Any]:
    meta = _meta(org)
    lead_id = org.legacy_lead_id
    return {
        "id": lead_id,
        "company_name": org.name,
        "normalized_name": org.normalized_name,
        "website": org.website,
        "domain": org.normalized_domain,
        "company_email": _primary_contact_value(org.contact_methods, "email", org_level=True),
        "company_phone": _primary_contact_value(org.contact_methods, "phone", org_level=True),
        "partner_tier": meta.get("partner_tier"),
        "plus_partner_signal": bool(meta.get("plus_partner_signal")),
        "rating": meta.get("rating"),
        "review_count": meta.get("review_count"),
        "partner_since": meta.get("partner_since"),
        "primary_location": meta.get("primary_location"),
        "supported_locations": meta.get("supported_locations") or [],
        "languages": meta.get("languages") or [],
        "featured_work": meta.get("featured_work") or [],
        "services": meta.get("services") or [],
        "locations": meta.get("locations") or meta.get("supported_locations") or [],
        "industries": meta.get("industries") or [],
        "description": org.description,
        "fit_score": org.relevance_score,
        "status": org.status,
        "confidence": meta.get("confidence", 0.0),
        "extraction_status": meta.get("extraction_status", "ok"),
        "possible_duplicate": bool(meta.get("possible_duplicate")),
        "created_at": org.created_at.isoformat() if org.created_at else None,
        "updated_at": org.updated_at.isoformat() if org.updated_at else None,
    }


def _primary_contact_value(
    methods: List[ContactMethod],
    kind: str,
    *,
    org_level: bool = False,
) -> Optional[str]:
    for method in methods:
        if method.kind != kind:
            continue
        if org_level and method.organization_id is None:
            continue
        return method.value
    return None


def person_to_dict(person: Person) -> Dict[str, Any]:
    meta = dict(person.legacy_metadata or {})
    email = _person_contact(person, "email")
    linkedin = _person_contact(person, "linkedin")
    return {
        "id": meta.get("legacy_person_id"),
        "lead_id": person.organization.legacy_lead_id if person.organization else None,
        "name": person.name,
        "title": person.title,
        "department": meta.get("department"),
        "seniority": meta.get("seniority"),
        "email": email,
        "linkedin_url": linkedin,
        "is_decision_maker": int(person.is_decision_maker),
        "is_relevant_contact": int(meta.get("is_relevant_contact", 0)),
        "relevance_reason": person.relevance_reason,
        "confidence": meta.get("confidence", 0.0),
        "raw_source_id": meta.get("raw_source_id"),
        "created_at": person.created_at.isoformat() if person.created_at else None,
    }


def _person_contact(person: Person, kind: str) -> Optional[str]:
    for method in person.contact_methods:
        if method.kind == kind:
            return method.value
    return None


def interaction_to_dict(item: Interaction, org: Organization) -> Dict[str, Any]:
    meta = dict(item.legacy_metadata or {})
    return {
        "id": meta.get("legacy_interaction_id"),
        "lead_id": org.legacy_lead_id,
        "person_id": meta.get("person_id"),
        "type": item.kind,
        "subject": meta.get("subject"),
        "body": meta.get("body"),
        "summary": item.summary,
        "reply_needed": int(item.requires_followup),
        "deadline": meta.get("deadline"),
        "priority": meta.get("priority"),
        "next_action": meta.get("next_action"),
        "status": meta.get("status", "open"),
        "raw_source_id": meta.get("raw_source_id"),
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def task_to_dict(item: Task, org: Organization) -> Dict[str, Any]:
    meta = dict(item.legacy_metadata or {})
    due = item.due_at.isoformat()[:10] if item.due_at else None
    return {
        "id": meta.get("legacy_task_id"),
        "lead_id": org.legacy_lead_id,
        "person_id": meta.get("person_id"),
        "title": item.title,
        "due_date": due,
        "priority": item.priority,
        "status": item.status,
        "source_interaction_id": meta.get("source_interaction_id"),
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def source_to_dict(source: Source) -> Dict[str, Any]:
    meta = dict(source.legacy_metadata or {})
    return {
        "id": source.legacy_source_id,
        "lead_id": meta.get("lead_id"),
        "source_type": source.source_type,
        "source_url": source.source_url,
        "source_filter_tier": meta.get("source_filter_tier"),
        "raw_text": source.raw_text,
        "parsed_json": meta.get("parsed_json"),
        "extraction_status": meta.get("extraction_status", "ok"),
        "confidence": meta.get("confidence", 0.0),
        "created_at": source.created_at.isoformat() if source.created_at else None,
    }


def organization_to_lead_detail(org: Organization) -> Dict[str, Any]:
    lead = organization_to_lead_row(org)
    people = [person_to_dict(p) for p in org.people]
    interactions = [interaction_to_dict(i, org) for i in org.interactions]
    tasks = [task_to_dict(t, org) for t in org.tasks]
    raw_sources = [source_to_dict(s) for s in _org_sources(org)]
    lead["people"] = people
    lead["interactions"] = interactions
    lead["tasks"] = tasks
    lead["raw_sources"] = raw_sources
    lead["canonical_source"] = raw_sources[0] if raw_sources else None
    lead["source_history"] = raw_sources[1:] if len(raw_sources) > 1 else []
    lead["source_filter_tier"] = next(
        (rs.get("source_filter_tier") for rs in raw_sources if rs.get("source_filter_tier")),
        None,
    )
    lead["people_count"] = len(people)
    lead["has_decision_maker"] = any(p.get("is_decision_maker") for p in people)
    return lead


def _org_sources(org: Organization) -> List[Source]:
    sources = list(getattr(org, "_loaded_sources", []) or [])
    sources.sort(key=lambda s: s.created_at or "", reverse=True)
    return sources
