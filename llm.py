"""LM Studio client with graceful fallback when unavailable."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

_base = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").rstrip("/")
LM_ENDPOINT = os.getenv("LMSTUDIO_ENDPOINT", f"{_base}/chat/completions")
LM_MODEL = os.getenv("LMSTUDIO_MODEL", "local-model")
LM_TIMEOUT = float(os.getenv("LMSTUDIO_TIMEOUT", "30"))


def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pattern in (r"\{[\s\S]*\}", r"[[\s\S]*\]"):
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return None


def is_lm_available() -> bool:
    try:
        base = LM_ENDPOINT.rsplit("/v1/", 1)[0] + "/v1/models"
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(base)
            return resp.status_code < 500
    except Exception:
        return False


def chat_completion(
    messages: List[Dict[str, str]],
    temperature: float = 0.0,
    max_tokens: int = 2000,
) -> Optional[str]:
    """Call LM Studio. Returns assistant content or None on failure."""
    payload = {
        "model": LM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        with httpx.Client(timeout=LM_TIMEOUT) as client:
            resp = client.post(LM_ENDPOINT, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def call_lmstudio_for_text(prompt: str, timeout_s: float = 8.0) -> Optional[str]:
    """Call LM Studio for short text with quick timeout. Returns raw or None on error."""
    try:
        messages = [
            {"role": "system", "content": "You are an intent classifier."},
            {"role": "user", "content": prompt},
        ]
        payload = {
            "model": LM_MODEL,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 512,
        }
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(LM_ENDPOINT, json=payload)
            if not resp.is_success:
                return None
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            # Try direct JSON first
            try:
                parsed = json.loads(content.strip())
                return json.dumps(parsed, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                pass
        return None
    except Exception:
        return None


def extract_structured(
    system_prompt: str,
    user_prompt: str,
) -> tuple[Optional[Dict[str, Any]], str]:
    """Return (parsed_json_or_none, raw_response_or_empty)."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    raw = chat_completion(messages)
    if not raw:
        return None, ""
    parsed = _try_parse_json(raw)
    return parsed, raw


EXTRACTION_SYSTEM = """
You extract structured lead intelligence from pasted text.
Return ONLY valid JSON, no markdown or prose.

Schema:
{
  "company_name": str|null,
  "website": str|null,
  "partner_tier": str|null,
  "services": [str],
  "locations": [str],
  "industries": [str],
  "description": str|null,
  "people": [{"name": str, "title": str|null, "linkedin_url": str|null, "department": str|null}],
  "interaction": {"subject": str|null, "summary": str|null, "reply_needed": bool, "deadline": str|null, "next_action": str|null}|null,
  "confidence": float
}
""".strip()
