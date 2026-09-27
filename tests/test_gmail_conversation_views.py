def test_bucket_predicates_are_mutually_exclusive_and_all_is_unfiltered():
    import inspect
    import gmail_db
    source = inspect.getsource(gmail_db.list_conversations)
    assert "c.link_status='linked' AND c.lead_id IS NOT NULL" in source
    assert "c.link_status='ambiguous'" in source
    assert "c.link_status='unlinked' AND c.lead_id IS NULL" in source


def test_ambiguous_template_uses_structured_candidates_and_safe_links():
    from pathlib import Path
    template = Path(__file__).parents[1] / "templates" / "emails.html"
    text = template.read_text(encoding="utf-8")
    assert "candidate_agencies" in text and "/leads/{{ agency.id }}" in text
    assert "link_evidence_json" not in text
    assert "page_token" not in text
