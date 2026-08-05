def test_bucket_predicates_are_mutually_exclusive_and_all_is_unfiltered():
    import inspect
    import gmail_db
    source = inspect.getsource(gmail_db.list_conversations)
    assert "c.link_status='linked' AND c.lead_id IS NOT NULL" in source
    assert "c.link_status='ambiguous'" in source
    assert "c.link_status='unlinked' AND c.lead_id IS NULL" in source
