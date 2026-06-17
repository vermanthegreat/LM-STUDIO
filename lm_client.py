import os
from dotenv import load_dotenv
import openai
from typing import Any, Dict, List

load_dotenv()
LMSTUDIO_BASE_URL = os.getenv('LMSTUDIO_BASE_URL')
LMSTUDIO_API_KEY = os.getenv('LMSTUDIO_API_KEY')
LMSTUDIO_MODEL = os.getenv('LMSTUDIO_MODEL', 'qwen3-8b')

if not LMSTUDIO_BASE_URL:
    raise RuntimeError('LMSTUDIO_BASE_URL not set in environment. Please copy .env.example to .env and edit it.')

# Configure openai to point to the local LM Studio
openai.api_base = LMSTUDIO_BASE_URL
openai.api_key = LMSTUDIO_API_KEY


def get_lm_client():
    """Return the configured openai module for calls."""
    return openai


def ask_lm(messages: List[Dict[str, str]], tools: Any = None, tool_choice: str = "auto") -> Dict[str, Any]:
    """Send messages to the LM and return the raw response dict.

    messages: list of {role: 'system'|'user'|'assistant', 'content': str}
    """
    client = get_lm_client()
    try:
        resp = client.ChatCompletion.create(
            model=LMSTUDIO_MODEL,
            messages=messages,
            temperature=0.0,
            max_tokens=1500
        )
        # return the whole response
        return resp
    except Exception as e:
        # surface a clear message for connection issues
        raise RuntimeError(f"LM call failed: {e}")
