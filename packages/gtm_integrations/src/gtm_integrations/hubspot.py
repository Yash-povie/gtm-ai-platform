"""HubSpot v3 CRM client with an in-memory sandbox store."""

import itertools
from datetime import datetime

from gtm_core.config import get_settings
from gtm_core.schemas import AccountData, ContactData

from gtm_integrations.base import BaseClient


class HubSpotClient(BaseClient):
    name = "hubspot"

    def __init__(self, token: str | None = None, **kw):
        super().__init__(token or get_settings().hubspot_private_app_token, base_url="https://api.hubapi.com", **kw)
        self._ids = itertools.count(1000)
        self.companies: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.activities: list[dict] = []
        self.tasks: list[dict] = []
        self.deals: list[dict] = []

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def upsert_company(self, acct: AccountData) -> str:
        if self.mock:
            existing = self.companies.get(acct.domain)
            cid = existing["id"] if existing else f"hs_co_{next(self._ids)}"
            self.companies[acct.domain] = {"id": cid, "name": acct.name, "domain": acct.domain}
            return cid
        body = {
            "inputs": [
                {
                    "idProperty": "domain",
                    "id": acct.domain,
                    "properties": {"name": acct.name, "domain": acct.domain, "industry": acct.industry or ""},
                }
            ]
        }
        data = await self._request("POST", "/crm/v3/objects/companies/batch/upsert", json=body, headers=self._auth())
        return data["results"][0]["id"]

    async def upsert_contact(self, c: ContactData, company_id: str | None = None) -> str:
        assert c.email, "contact email required"
        if self.mock:
            existing = self.contacts.get(c.email)
            hid = existing["id"] if existing else f"hs_ct_{next(self._ids)}"
            self.contacts[c.email] = {"id": hid, "email": c.email, "company_id": company_id, "title": c.title}
            return hid
        body = {
            "inputs": [
                {
                    "idProperty": "email",
                    "id": c.email,
                    "properties": {
                        "email": c.email,
                        "firstname": c.first_name or "",
                        "lastname": c.last_name or "",
                        "jobtitle": c.title or "",
                    },
                }
            ]
        }
        data = await self._request("POST", "/crm/v3/objects/contacts/batch/upsert", json=body, headers=self._auth())
        return data["results"][0]["id"]

    async def log_email(self, contact_id: str, subject: str, body_text: str, sent_at: datetime) -> str:
        if self.mock:
            aid = f"hs_em_{next(self._ids)}"
            self.activities.append({"id": aid, "contact_id": contact_id, "subject": subject, "sent_at": sent_at})
            return aid
        payload = {
            "properties": {
                "hs_timestamp": sent_at.isoformat(),
                "hs_email_direction": "EMAIL",
                "hs_email_subject": subject,
                "hs_email_text": body_text,
            }
        }
        data = await self._request("POST", "/crm/v3/objects/emails", json=payload, headers=self._auth())
        return data["id"]

    async def create_task(self, title: str, due_date: str | None = None, company_id: str | None = None) -> str:
        if self.mock:
            tid = f"hs_tk_{next(self._ids)}"
            self.tasks.append({"id": tid, "title": title, "due_date": due_date, "company_id": company_id})
            return tid
        payload = {"properties": {"hs_task_subject": title, "hs_timestamp": due_date or "", "hs_task_status": "NOT_STARTED"}}
        data = await self._request("POST", "/crm/v3/objects/tasks", json=payload, headers=self._auth())
        return data["id"]

    async def create_deal(self, title: str, amount_usd: float | None, company_id: str | None = None) -> str:
        if self.mock:
            did = f"hs_dl_{next(self._ids)}"
            self.deals.append({"id": did, "title": title, "amount": amount_usd, "company_id": company_id})
            return did
        payload = {"properties": {"dealname": title, "amount": str(amount_usd or 0)}}
        data = await self._request("POST", "/crm/v3/objects/deals", json=payload, headers=self._auth())
        return data["id"]
