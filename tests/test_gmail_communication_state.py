def test_agency_requires_reply_policy_is_documented_in_projection_code():
    # Regression anchor: agency state is the conservative OR of linked conversation states.
    import inspect
    import gmail_db
    assert "any(c[\"requires_reply\"] for c in conversations)" in inspect.getsource(gmail_db.rebuild_gmail_projections)
