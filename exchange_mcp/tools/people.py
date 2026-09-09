"""People / directory search tools for the Exchange MCP server.

Ports the find-person.py logic into an MCP tool using OWAClient.
"""

import json

from mcp.server.fastmcp import Context

from exchange_mcp.server import mcp, AppContext
from exchange_mcp.owa_client import BearerModeRequiredError, OWAClient


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


def _parse_person(resolution: dict) -> dict:
    """Parse person data from a ResolveNames resolution entry.

    Preserves the exact logic from find-person.py parse_person().
    """
    mailbox = resolution.get("Mailbox", {})
    contact = resolution.get("Contact", {})

    person = {
        "name": mailbox.get("Name", contact.get("DisplayName", "")),
        "email": mailbox.get("EmailAddress", ""),
        "type": mailbox.get("MailboxType", ""),
        "first_name": contact.get("GivenName", ""),
        "last_name": contact.get("Surname", ""),
        "job_title": contact.get("JobTitle", ""),
        "department": contact.get("Department", ""),
        "company": contact.get("CompanyName", ""),
        "office": contact.get("OfficeLocation", ""),
        "manager": "",
        "manager_email": "",
        "phones": {},
        "address": {},
        "direct_reports": [],
        "alias": contact.get("Alias", ""),
    }

    # Phone numbers
    for phone in contact.get("PhoneNumbers", []):
        key = phone.get("Key", "")
        number = phone.get("PhoneNumber", "")
        if number:
            person["phones"][key] = number

    # Physical address
    for addr in contact.get("PhysicalAddresses", []):
        if addr.get("Key") == "Business":
            parts = []
            if addr.get("Street"):
                parts.append(addr["Street"])
            if addr.get("City"):
                parts.append(addr["City"])
            if addr.get("PostalCode"):
                parts.append(addr["PostalCode"])
            if addr.get("CountryOrRegion"):
                parts.append(addr["CountryOrRegion"])
            if parts:
                person["address"] = {
                    "street": addr.get("Street", ""),
                    "city": addr.get("City", ""),
                    "postal_code": addr.get("PostalCode", ""),
                    "country": addr.get("CountryOrRegion", ""),
                    "full": ", ".join(parts),
                }

    # Manager
    manager_data = contact.get("ManagerMailbox", {}).get("Mailbox", {})
    if manager_data:
        person["manager"] = manager_data.get("Name", "")
        person["manager_email"] = manager_data.get("EmailAddress", "")
    elif contact.get("Manager"):
        person["manager"] = contact.get("Manager", "")

    # Direct reports
    for report in contact.get("DirectReports", []):
        person["direct_reports"].append({
            "name": report.get("Name", ""),
            "email": report.get("EmailAddress", ""),
        })

    return person


def _parse_suggestion(suggestion: dict) -> dict:
    """Parse person data from a substrate /search/api/v1/suggestions entry.

    Same output shape as _parse_person() for a uniform find_person() result,
    but the suggestions API doesn't return manager/direct-reports/postal
    address at all - those stay empty, same as when ResolveNames' Contact
    data happens to be sparse.
    """
    emails = suggestion.get("EmailAddresses") or []
    person = {
        "name": suggestion.get("DisplayName", ""),
        "email": emails[0] if emails else "",
        "type": suggestion.get("PeopleType", ""),
        "first_name": suggestion.get("GivenName", ""),
        "last_name": suggestion.get("Surname", ""),
        "job_title": suggestion.get("JobTitle", ""),
        "department": suggestion.get("Department", ""),
        "company": suggestion.get("CompanyName", ""),
        "office": suggestion.get("OfficeLocation", ""),
        "manager": "",
        "manager_email": "",
        "phones": {},
        "address": {},
        "direct_reports": [],
        "alias": suggestion.get("Alias", ""),
    }

    for phone in suggestion.get("Phones", []):
        key = phone.get("Type", "")
        number = phone.get("Number", "")
        if number:
            person["phones"][key] = number

    return person


@mcp.tool()
def find_person(query: str, ctx: Context) -> str:
    """Search for people in the corporate directory.

    On the modern Outlook backend ("new Outlook" tenants), uses the same
    Substrate Search API the People app's own search box calls - EWS
    ResolveNames throws a server-side fault on those tenants (see
    PROJECT_STATUS.md #401). Falls back to ResolveNames on classic OWA
    (on-prem, or a cloud tenant not yet migrated), where it works fine.

    Args:
        query: Name, email address, or keyword to search for.

    Returns:
        JSON array of matching people with contact details (name, email,
        job_title, department, company, office, phones, address, manager,
        direct_reports, alias). manager/direct_reports/address are only
        ever populated via the ResolveNames path.
    """
    client = _get_client(ctx)

    try:
        suggestions = client.find_people(query)
        return json.dumps([_parse_suggestion(s) for s in suggestions], ensure_ascii=False)
    except BearerModeRequiredError:
        pass  # classic OWA (on-prem, or not yet on the modern backend) - fall back below
    except Exception as e:
        return json.dumps({"error": str(e)})

    try:
        resolutions = client.resolve_names(query)
    except Exception as e:
        return json.dumps({"error": str(e)})

    if not resolutions:
        return json.dumps([])

    people = [_parse_person(r) for r in resolutions]
    return json.dumps(people, ensure_ascii=False)
