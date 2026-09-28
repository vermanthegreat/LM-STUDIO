"""Local knowledge ingestion and retrieval subsystem (SQLite runtime only).

Flow: original file -> deterministic type routing -> extraction -> optional
vision -> schema-validated LLM classification -> SQLite persistence + FTS ->
search/retrieval. The LLM is advisory; the application owns persistence.
"""
