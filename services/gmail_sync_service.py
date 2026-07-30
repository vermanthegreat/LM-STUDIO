"""Manual bounded Gmail label synchronization (G0)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from config import AppConfig
from gmail_runtime import GmailRuntimeUnsupportedError, require_gmail_sqlite_runtime
from gmail_db import (
    find_existing_source,
    get_message_by_source_id,
    init_gmail_db,
    insert_gmail_message,
    insert_gmail_source,
    update_gmail_message,
    update_sync_state,
)
from gmail_schemas import GmailSyncResult, SyncResultCounts
from providers.fake_gmail import FakeGmailProvider
from providers.gmail import GmailConfigurationError, GmailProviderAdapter, build_gmail_provider
from providers.gmail_normalize import content_hash_for_message
from repositories.command_log_store import get_command_log_store
from services.command_log import CommandLogEntry, CommandStatus, InMemoryCommandLog, transition
from services.email_classification_service import classify_email_message
from services.gmail_linking import LinkDecision, resolve_contact_link

logger = logging.getLogger(__name__)


@dataclass
class _SqliteGmailLinkAdapter:
    store: Any
    conn: Any | None = None

    def find_exact_email_matches(self, email: str) -> list[dict[str, Any]]:
        import db

        if self.conn is not None:
            return db.find_exact_email_matches(email, db_path=self.store.database_path, conn=self.conn)
        return db.find_exact_email_matches(email, db_path=self.store.database_path)

    def find_thread_links(
        self,
        *,
        external_account: str,
        external_thread_id: str,
        exclude_message_id: str,
    ) -> list[dict[str, Any]]:
        import db
        import gmail_db

        if self.conn is not None:
            gmail_db.ensure_gmail_tables(self.conn)
            return gmail_db.find_thread_links(
                self.conn,
                external_account=external_account,
                external_thread_id=external_thread_id,
                exclude_message_id=exclude_message_id,
            )
        with db.get_conn(self.store.database_path) as conn:
            gmail_db.ensure_gmail_tables(conn)
            return gmail_db.find_thread_links(
                conn,
                external_account=external_account,
                external_thread_id=external_thread_id,
                exclude_message_id=exclude_message_id,
            )

    def find_exact_company_name_matches(self, company_name: str) -> list[dict[str, Any]]:
        import db

        normalized = db.normalize_name(company_name)
        if not normalized:
            return []
        if self.conn is not None:
            rows = self.conn.execute(
                """
                SELECT id AS lead_id, NULL AS person_id, company_name
                FROM leads
                WHERE normalized_name = ?
                """,
                (normalized,),
            ).fetchall()
            return [dict(row) for row in rows]
        with db.get_conn(self.store.database_path) as conn:
            rows = conn.execute(
                """
                SELECT id AS lead_id, NULL AS person_id, company_name
                FROM leads
                WHERE normalized_name = ?
                """,
                (normalized,),
            ).fetchall()
        return [dict(row) for row in rows]


def _provider_from_config(cfg: AppConfig, provider: Any | None) -> Any:
    if provider is not None:
        return provider
    if not cfg.gmail_enabled:
        raise GmailConfigurationError("gmail_disabled", "Gmail integration is disabled.")
    if cfg.gmail_client_secret_path is None or cfg.gmail_token_path is None:
        raise GmailConfigurationError(
            "gmail_not_configured",
            "Gmail credentials or token path is not configured.",
        )
    return build_gmail_provider(
        client_secret_path=cfg.gmail_client_secret_path,
        token_path=cfg.gmail_token_path,
    )


def _serialize_addresses(addresses: list[Any]) -> list[dict[str, Any]]:
    return [addr.model_dump() for addr in addresses]


def _persist_message(
    conn: Any,
    store: Any,
    cfg: AppConfig,
    normalized: Any,
    counts: SyncResultCounts,
    *,
    use_llm: bool,
) -> None:
    import gmail_db
    from services.email_classification_service import determine_direction

    existing = find_existing_source(
        conn,
        external_account=normalized.account_email,
        external_message_id=normalized.message_id,
    )
    link_adapter = _SqliteGmailLinkAdapter(store, conn)
    link: LinkDecision = resolve_contact_link(link_adapter, normalized)
    if link.link_status.value == "linked":
        counts.linked += 1
    elif link.link_status.value == "ambiguous":
        counts.ambiguous += 1
    else:
        counts.unlinked += 1

    try:
        classification = classify_email_message(
            normalized,
            link_status=link.link_status.value,
            use_llm=use_llm,
            model_name=cfg.lmstudio_model,
        )
    except Exception:
        logger.exception("classification failed for message %s", normalized.message_id)
        counts.classification_failed += 1
        from gmail_schemas import ClassificationSource, PrimaryIntent
        from services.email_classification_service import classify_deterministic
        from services.email_classification_service import determine_direction as det_dir

        classification = classify_deterministic(
            normalized,
            direction=det_dir(normalized),
            link_status=link.link_status.value,
        )
        classification.primary_intent = PrimaryIntent.UNKNOWN
        classification.classification_source = ClassificationSource.FALLBACK
        classification.classification_warning = "classification_failed"

    content_hash = content_hash_for_message(normalized)
    raw_text = normalized.plain_body or normalized.subject or ""
    provider_meta = dict(normalized.provider_metadata or {})
    provider_meta["attachment_metadata"] = normalized.attachment_metadata
    occurred_at = normalized.internal_date.astimezone(timezone.utc).isoformat()
    direction = determine_direction(normalized)

    if existing is None:
        source = insert_gmail_source(
            conn,
            external_account=normalized.account_email,
            external_message_id=normalized.message_id,
            external_thread_id=normalized.thread_id,
            external_rfc_message_id=normalized.rfc_message_id,
            provider_occurred_at=occurred_at,
            provider_metadata=provider_meta,
            content_hash=content_hash,
            raw_text=raw_text,
        )
        insert_gmail_message(
            conn,
            gmail_source_id=int(source["id"]),
            subject=normalized.subject,
            direction=direction,
            occurred_at=occurred_at,
            from_address=normalized.from_address.email,
            to_addresses=_serialize_addresses(normalized.to_addresses),
            cc_addresses=_serialize_addresses(normalized.cc_addresses),
            message_role=classification.message_role,
            target_company_name=classification.target_company_name,
            primary_intent=classification.primary_intent,
            intent_confidence=classification.confidence,
            markers=classification.markers,
            temporal_signals=classification.temporal_signals,
            link_status=link.link_status,
            classification_source=classification.classification_source.value,
            classification_model=classification.classification_model,
            classification_warning=classification.classification_warning,
            lead_id=link.lead_id,
            person_id=link.person_id,
        )
        counts.imported += 1
        return

    counts.already_present += 1
    message_row = get_message_by_source_id(conn, int(existing["id"]))

    if message_row is None:
        insert_gmail_message(
            conn,
            gmail_source_id=int(existing["id"]),
            subject=normalized.subject,
            direction=direction,
            occurred_at=occurred_at,
            from_address=normalized.from_address.email,
            to_addresses=_serialize_addresses(normalized.to_addresses),
            cc_addresses=_serialize_addresses(normalized.cc_addresses),
            message_role=classification.message_role,
            target_company_name=classification.target_company_name,
            primary_intent=classification.primary_intent,
            intent_confidence=classification.confidence,
            markers=classification.markers,
            temporal_signals=classification.temporal_signals,
            link_status=link.link_status,
            classification_source=classification.classification_source.value,
            classification_model=classification.classification_model,
            classification_warning=classification.classification_warning,
            lead_id=link.lead_id,
            person_id=link.person_id,
        )
        counts.updated += 1
        return

    stored_markers = gmail_db._json_loads(message_row.get("markers_json"), [])
    semantic_changed = (
        message_row.get("subject") != normalized.subject
        or message_row.get("direction") != direction.value
        or message_row.get("from_address") != normalized.from_address.email
        or gmail_db._json_loads(message_row.get("to_addresses_json"), []) != _serialize_addresses(normalized.to_addresses)
        or gmail_db._json_loads(message_row.get("cc_addresses_json"), []) != _serialize_addresses(normalized.cc_addresses)
        or (message_row.get("message_role") or "conversation_message") != classification.message_role.value
        or message_row.get("target_company_name") != classification.target_company_name
        or message_row.get("primary_intent") != classification.primary_intent.value
        or list(stored_markers or []) != [m.value for m in classification.markers]
        or message_row.get("link_status") != link.link_status.value
        or message_row.get("classification_source") != classification.classification_source.value
        or message_row.get("classification_model") != classification.classification_model
        or message_row.get("classification_warning") != classification.classification_warning
        or message_row.get("lead_id") != link.lead_id
        or message_row.get("person_id") != link.person_id
    )
    if not semantic_changed and existing.get("content_hash") == content_hash:
        return

    update_gmail_message(
        conn,
        int(message_row["id"]),
        content_hash=content_hash,
        subject=normalized.subject,
        direction=direction,
        occurred_at=occurred_at,
        from_address=normalized.from_address.email,
        to_addresses=_serialize_addresses(normalized.to_addresses),
        cc_addresses=_serialize_addresses(normalized.cc_addresses),
        message_role=classification.message_role,
        target_company_name=classification.target_company_name,
        primary_intent=classification.primary_intent,
        intent_confidence=classification.confidence,
        markers=classification.markers,
        temporal_signals=classification.temporal_signals,
        link_status=link.link_status,
        classification_source=classification.classification_source.value,
        classification_model=classification.classification_model,
        classification_warning=classification.classification_warning,
        lead_id=link.lead_id,
        person_id=link.person_id,
        gmail_source_id=int(existing["id"]),
    )
    if semantic_changed or existing.get("content_hash") != content_hash:
        counts.updated += 1


def sync_gmail_label(
    store: Any,
    cfg: AppConfig,
    *,
    provider: Any | None = None,
    command_log_store: Any | None = None,
    use_llm: bool = True,
) -> tuple[GmailSyncResult, Optional[CommandLogEntry]]:
    import db
    import gmail_db

    try:
        require_gmail_sqlite_runtime(store)
    except GmailRuntimeUnsupportedError as exc:
        command_log = command_log_store or InMemoryCommandLog()
        entry = command_log.create("Gmail manual sync")
        entry.intent = "gmail_sync"
        entry.tool_name = "gmail_sync"
        entry.risk_class = "external"
        entry.tool_arguments = {
            "label": cfg.gmail_sync_label,
            "limit": cfg.gmail_sync_limit,
            "account": "unsupported_runtime",
        }
        transition(entry, CommandStatus.PLANNED)
        command_log.update(entry)
        transition(entry, CommandStatus.FAILED)
        entry.error_code = exc.error_code
        entry.error_message = exc.message
        entry.result_summary = {"counts": SyncResultCounts().model_dump(), "warnings": []}
        command_log.update(entry)
        return (
            GmailSyncResult(
                status="error",
                counts=SyncResultCounts(),
                error_code=exc.error_code,
                message=exc.message,
            ),
            entry,
        )

    init_gmail_db(store.database_path)
    command_log = command_log_store or get_command_log_store(store)
    entry = command_log.create("Gmail manual sync")
    entry.intent = "gmail_sync"
    entry.tool_name = "gmail_sync"
    entry.risk_class = "external"
    entry.tool_arguments = {
        "label": cfg.gmail_sync_label,
        "limit": cfg.gmail_sync_limit,
        "account": "configured",
    }
    transition(entry, CommandStatus.PLANNED)
    command_log.update(entry)

    counts = SyncResultCounts()
    warnings: list[str] = []
    error_code: Optional[str] = None
    message: Optional[str] = None
    status = "ok"
    account_email: Optional[str] = None

    try:
        gmail_provider = _provider_from_config(cfg, provider)
        transition(entry, CommandStatus.EXECUTING)
        command_log.update(entry)

        profile = gmail_provider.get_account_profile()
        account_email = (profile.get("email") or getattr(gmail_provider, "account_email", "")).lower()
        entry.tool_arguments = {
            **(entry.tool_arguments or {}),
            "account": account_email,
        }
        command_log.update(entry)

        label_id = gmail_provider.resolve_label_id(cfg.gmail_sync_label)
        if not label_id:
            raise GmailConfigurationError(
                "gmail_label_missing",
                f"Gmail label '{cfg.gmail_sync_label}' was not found. Create it manually in Gmail.",
            )
        entry.tool_arguments = {
            **(entry.tool_arguments or {}),
            "label_id": label_id,
        }
        command_log.update(entry)

        listing = gmail_provider.list_messages(label_id, cfg.gmail_sync_limit)
        refs = listing.get("messages") or []
        counts.discovered = len(refs)

        with db.get_conn(store.database_path) as conn:
            gmail_db.ensure_gmail_tables(conn)
            for ref in refs[: cfg.gmail_sync_limit]:
                message_id = ref.get("id")
                if not message_id:
                    continue
                try:
                    normalized = gmail_provider.get_message(str(message_id))
                    _persist_message(conn, store, cfg, normalized, counts, use_llm=use_llm)
                except Exception:
                    logger.exception("failed importing gmail message %s", message_id)
                    counts.failed += 1

            now = datetime.now(timezone.utc).isoformat()
            update_sync_state(
                conn,
                account_email=account_email,
                configured_label=cfg.gmail_sync_label,
                last_sync_at=now,
                last_success_at=now if counts.failed == 0 else None,
                last_status="ok" if counts.failed == 0 else "partial",
                last_result_summary=counts.model_dump(),
                last_error_code=None,
            )

        transition(entry, CommandStatus.SUCCEEDED)
        entry.result_summary = {
            "counts": counts.model_dump(),
            "warnings": warnings,
            "account": account_email,
            "label": cfg.gmail_sync_label,
            "label_id": label_id,
            "limit": cfg.gmail_sync_limit,
            "correlation_id": str(uuid4()),
        }
        command_log.update(entry)
        return (
            GmailSyncResult(status="ok", counts=counts, warnings=warnings),
            entry,
        )
    except GmailConfigurationError as exc:
        status = "error"
        error_code = exc.error_code
        message = exc.message
        transition(entry, CommandStatus.FAILED)
        entry.error_code = exc.error_code
        entry.error_message = exc.message
        entry.result_summary = {"counts": counts.model_dump(), "warnings": warnings}
        command_log.update(entry)
        with db.get_conn(store.database_path) as conn:
            gmail_db.ensure_gmail_tables(conn)
            update_sync_state(
                conn,
                account_email=account_email,
                configured_label=cfg.gmail_sync_label,
                last_sync_at=datetime.now(timezone.utc).isoformat(),
                last_success_at=None,
                last_status="error",
                last_result_summary=counts.model_dump(),
                last_error_code=exc.error_code,
            )
        return GmailSyncResult(status="error", counts=counts, warnings=warnings, error_code=error_code, message=message), entry
    except Exception as exc:
        logger.exception("gmail sync failed")
        transition(entry, CommandStatus.FAILED)
        entry.error_code = "gmail_sync_failed"
        entry.error_message = "Gmail synchronization failed."
        entry.result_summary = {"counts": counts.model_dump(), "warnings": warnings}
        command_log.update(entry)
        return (
            GmailSyncResult(
                status="error",
                counts=counts,
                warnings=warnings,
                error_code="gmail_sync_failed",
                message=str(exc),
            ),
            entry,
        )


def gmail_integration_status(store: Any, cfg: AppConfig) -> dict[str, Any]:
    import db
    import gmail_db
    from gmail_schemas import GMAIL_READONLY_SCOPE

    token_available = bool(cfg.gmail_token_path and cfg.gmail_token_path.is_file())
    credentials_available = bool(cfg.gmail_client_secret_path and cfg.gmail_client_secret_path.is_file())
    state: dict[str, Any] = {}
    if getattr(store, "backend", "sqlite") == "sqlite":
        init_gmail_db(store.database_path)
        with db.get_conn(store.database_path) as conn:
            gmail_db.ensure_gmail_tables(conn)
            state = gmail_db.get_sync_state(conn, configured_label=cfg.gmail_sync_label)

    account_email = state.get("account_email")
    if cfg.gmail_enabled and token_available and not account_email:
        try:
            if cfg.gmail_client_secret_path and cfg.gmail_token_path:
                provider = build_gmail_provider(
                    client_secret_path=cfg.gmail_client_secret_path,
                    token_path=cfg.gmail_token_path,
                )
                account_email = provider.account_email
        except GmailConfigurationError:
            account_email = None

    backend = getattr(store, "backend", "sqlite")
    runtime_capability = "sqlite_only" if backend == "sqlite" else "postgresql_unsupported"

    return {
        "enabled": cfg.gmail_enabled,
        "runtime_capability": runtime_capability,
        "runtime_backend": backend,
        "token_available": token_available,
        "credentials_available": credentials_available,
        "account_email": account_email,
        "configured_label": cfg.gmail_sync_label,
        "sync_limit": cfg.gmail_sync_limit,
        "scope": GMAIL_READONLY_SCOPE,
        "last_sync_at": state.get("last_sync_at"),
        "last_success_at": state.get("last_success_at"),
        "last_status": state.get("last_status"),
        "last_result_summary": gmail_db._json_loads(state.get("last_result_summary_json")),
        "last_error_code": state.get("last_error_code"),
        "app_timezone": cfg.app_timezone,
    }
