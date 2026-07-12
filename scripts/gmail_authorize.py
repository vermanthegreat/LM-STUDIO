#!/usr/bin/env python3
"""Local Gmail OAuth bootstrap for gmail.readonly scope."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

from config import AppConfig
from gmail_schemas import GMAIL_READONLY_SCOPE
from providers.gmail import GmailConfigurationError, run_local_authorization


def main() -> int:
    load_dotenv()
    cfg = AppConfig.from_env()
    if cfg.gmail_client_secret_path is None:
        print("ERROR: GMAIL_CLIENT_SECRET_PATH is not configured.", file=sys.stderr)
        return 1
    if cfg.gmail_token_path is None:
        print("ERROR: GMAIL_TOKEN_PATH is not configured.", file=sys.stderr)
        return 1
    try:
        result = run_local_authorization(cfg.gmail_client_secret_path, cfg.gmail_token_path)
    except GmailConfigurationError as exc:
        print(f"ERROR [{exc.error_code}]: {exc.message}", file=sys.stderr)
        return 1
    print(f"Gmail authorization complete for {result['account_email']}.")
    print(f"Token saved to {cfg.gmail_token_path}.")
    print(f"Scope: {GMAIL_READONLY_SCOPE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
