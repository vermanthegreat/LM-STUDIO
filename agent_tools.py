from typing import Any, Dict, List
import json
from db_postgres import upsert_company, upsert_contact, create_source, log_interaction, create_email_draft, search_companies, search_contacts

# Tool wrappers - accept a dict (args) and return a plain dict

def upsert_company_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    name = args.get('name')
    return upsert_company(
        name=name,
        website=args.get('website'),
        industry=args.get('industry'),
        country=args.get('country'),
        status=args.get('status'),
        fit_score=args.get('fit_score'),
        extra=args.get('extra')
    )


def upsert_contact_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    return upsert_contact(
        full_name=args.get('full_name'),
        email=args.get('email'),
        role=args.get('role'),
        company_id=args.get('company_id'),
        linkedin_url=args.get('linkedin_url'),
        status=args.get('status'),
        extra=args.get('extra')
    )


def create_source_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    return create_source(raw_text=args.get('raw_text',''), extracted_json=args.get('extracted_json'))


def log_interaction_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    return log_interaction(company_id=args.get('company_id'), contact_id=args.get('contact_id'), kind=args.get('kind','note'), note=args.get('note'))


def create_email_draft_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    return create_email_draft(company_id=args.get('company_id'), contact_id=args.get('contact_id'), subject=args.get('subject',''), body=args.get('body',''), status=args.get('status','draft'))


def search_companies_tool(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    return search_companies(query=args.get('query',''))


def search_contacts_tool(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    return search_contacts(query=args.get('query',''))

# Tool schemas following a minimal OpenAI-compatible format
upsert_company = {
    'name': 'upsert_company',
    'description': 'Create or update a company record',
    'parameters': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'website': {'type': 'string'},
            'industry': {'type': 'string'},
            'country': {'type': 'string'},
            'status': {'type': 'string'},
            'fit_score': {'type': 'integer'},
            'extra': {'type': 'object'}
        }
    }
}

upsert_contact = {
    'name': 'upsert_contact',
    'description': 'Create or update a contact',
    'parameters': {
        'type': 'object',
        'properties': {
            'full_name': {'type': 'string'},
            'email': {'type': 'string'},
            'role': {'type': 'string'},
            'company_id': {'type': 'string'},
            'linkedin_url': {'type': 'string'},
            'status': {'type': 'string'},
            'extra': {'type': 'object'}
        }
    }
}

create_source = {
    'name': 'create_source',
    'description': 'Store raw source text and extracted JSON',
    'parameters': {
        'type': 'object',
        'properties': {
            'raw_text': {'type': 'string'},
            'extracted_json': {'type': 'object'}
        }
    }
}

log_interaction = {
    'name': 'log_interaction',
    'description': 'Log an interaction or note',
    'parameters': {
        'type': 'object',
        'properties': {
            'company_id': {'type': 'string'},
            'contact_id': {'type': 'string'},
            'kind': {'type': 'string'},
            'note': {'type': 'string'}
        }
    }
}

create_email_draft = {
    'name': 'create_email_draft',
    'description': 'Create an email draft',
    'parameters': {
        'type': 'object',
        'properties': {
            'company_id': {'type': 'string'},
            'contact_id': {'type': 'string'},
            'subject': {'type': 'string'},
            'body': {'type': 'string'},
            'status': {'type': 'string'}
        }
    }
}

search_companies = {
    'name': 'search_companies',
    'description': 'Search companies by query',
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {'type': 'string'}
        }
    }
}

search_contacts = {
    'name': 'search_contacts',
    'description': 'Search contacts by query',
    'parameters': {
        'type': 'object',
        'properties': {
            'query': {'type': 'string'}
        }
    }
}
