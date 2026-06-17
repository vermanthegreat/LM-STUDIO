import os
import json
from dotenv import load_dotenv
from lm_client import ask_lm
import agent_tools

load_dotenv()
SYSTEM_PROMPT = (
    "You are a local lead/contact operations assistant. Extract companies, contacts, statuses, interactions, and draft emails. "
    "Use Postgres tools as the source of truth. Do not invent emails. If data is missing, store null and add a note. Prefer structured records over prose. /no_think"
)


def _try_parse_json(text: str):
    # find first JSON object in text
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        # attempt to extract substring
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                return None
    return None


TOOL_MAP = {
    'upsert_company': agent_tools.upsert_company_tool,
    'upsert_contact': agent_tools.upsert_contact_tool,
    'create_source': agent_tools.create_source_tool,
    'log_interaction': agent_tools.log_interaction_tool,
    'create_email_draft': agent_tools.create_email_draft_tool,
    'search_companies': agent_tools.search_companies_tool,
    'search_contacts': agent_tools.search_contacts_tool,
}


def run():
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT}
    ]
    print('Local lead/contact agent. Type /exit to quit.')
    while True:
        try:
            user = input('\nUser: ').strip()
        except (KeyboardInterrupt, EOFError):
            print('\nExiting...')
            break
        if not user:
            continue
        if user.lower() in ('/exit','/quit'):
            print('Goodbye')
            break
        messages.append({'role': 'user', 'content': user})
        try:
            resp = ask_lm(messages)
        except Exception as e:
            print(f"LM error: {e}")
            continue
        # Navigate chat completion response structure
        content = ''
        try:
            content = resp['choices'][0]['message']['content']
        except Exception:
            # fallback older shape
            content = resp['choices'][0]['text']
        print('\nAssistant (raw):')
        print(content)

        # detect tool call JSON
        parsed = _try_parse_json(content)
        if parsed and isinstance(parsed, dict) and 'tool_call' in parsed:
            tc = parsed['tool_call']
            name = tc.get('name')
            args = tc.get('args', {})
            if name in TOOL_MAP:
                try:
                    result = TOOL_MAP[name](args)
                    result_msg = {'role': 'assistant', 'content': json.dumps({'tool_result': result})}
                    messages.append(result_msg)
                    # ask LM for final response after tool result
                    follow = ask_lm(messages)
                    try:
                        final = follow['choices'][0]['message']['content']
                    except Exception:
                        final = follow['choices'][0]['text']
                    print('\nAssistant (final):')
                    print(final)
                    messages.append({'role': 'assistant', 'content': final})
                except Exception as e:
                    print(f"Tool execution error: {e}")
            else:
                print(f"Unknown tool requested: {name}")
        else:
            # normal assistant response
            messages.append({'role': 'assistant', 'content': content})
            print('\n(assistant saved to history)')


if __name__ == '__main__':
    run()
