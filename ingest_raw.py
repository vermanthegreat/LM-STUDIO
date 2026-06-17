import sys
import json
from pathlib import Path
from dotenv import load_dotenv
from lm_client import ask_lm
import agent_tools

load_dotenv()
SYSTEM = "You are a local extraction assistant. Given raw text, extract fields and return a JSON object that contains a 'tool_calls' list. Each item should be {\"name\": <tool_name>, \"args\": {...}}. Use /no_think."


def _try_parse_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find('[')
        end = text.rfind(']')
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                pass
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                pass
    return None


def main():
    if len(sys.argv) < 2:
        print('Usage: python ingest_raw.py path/to/file.txt')
        sys.exit(1)
    p = Path(sys.argv[1])
    if not p.exists():
        print(f'File not found: {p}')
        sys.exit(2)
    raw = p.read_text(encoding='utf-8')
    messages = [
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': f"Extract the following fields from the raw text and return a JSON with key 'tool_calls' (list): company name, website, industry, country, fit_score (0-100), contact full_name, role, email, linkedin_url, notes. Return only JSON. Do not add prose. /no_think\n\nRAW:\n{raw}"}
    ]
    try:
        resp = ask_lm(messages)
    except Exception as e:
        print(f'LM Studio error: {e}')
        sys.exit(3)
    try:
        content = resp['choices'][0]['message']['content']
    except Exception:
        content = resp['choices'][0]['text']
    parsed = _try_parse_json(content)
    if parsed is None:
        print('Failed to parse JSON from model response:')
        print(content)
        sys.exit(4)
    # Accept either {'tool_calls': [...]} or a list of tool calls
    tool_calls = []
    if isinstance(parsed, dict) and 'tool_calls' in parsed:
        tool_calls = parsed['tool_calls']
    elif isinstance(parsed, list):
        tool_calls = parsed
    else:
        print('Unexpected JSON shape from model: expected tool_calls list or list')
        print(json.dumps(parsed, indent=2))
        sys.exit(5)

    results = []
    for tc in tool_calls:
        name = tc.get('name')
        args = tc.get('args', {})
        if not name:
            print('Tool call without name, skipping')
            continue
        fn = getattr(agent_tools, f"{name}_tool", None)
        if not fn:
            print(f'Unknown tool: {name}, skipping')
            continue
        try:
            res = fn(args)
            results.append({'tool': name, 'result': res})
        except Exception as e:
            results.append({'tool': name, 'error': str(e)})
    print('Ingest summary:')
    print(json.dumps(results, indent=2, default=str))


if __name__ == '__main__':
    main()
