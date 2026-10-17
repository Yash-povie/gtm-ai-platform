"""Salesforce REST/SOQL client (OAuth username-password flow) with an in-memory sandbox store."""

import itertools
from datetime import datetime

from gtm_core.config import get_settings
from gtm_core.schemas import AccountData, ContactData

from gtm_integrations.base import BaseClient

API = "/services/data/v60.0"


class SalesforceClient(BaseClient):
    name = "salesforce"

    def __init__(self, **kw):
        s = get_settings()
        super().__init__(s.salesforce_client_id, base_url="https://login.salesforce.com", **kw)
        self._s = s
        self._token: str | None = None
        self._ids = itertools.count(5000)
        self.accounts: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.tasks: list[dict] = []
        self.activities: list[dict] = []

    async def _auth(self) -> dict:
        if self._token is None:
            data = await self._request(
                "POST",
                "/services/oauth2/token",
                data={
                    "grant_type": "password",
                    "client_id": self._s.salesforce_client_id,
                    "client_secret": self._s.salesforce_client_secret,
                    "username": self._s.salesforce_username,
                    "password": self._s.salesforce_password + self._s.salesforce_security_token,
                },
            )
            self._token = data["access_token"]
            self.base_url = data["instance_url"]
            self._http = None  # rebuild client against instance_url
        return {"Authorization": f"Bearer {self._token}"}

    async def query(self, soql: str) -> list[dict]:
        if self.mock:
            return []
        data = await self._request("GET", f"{API}/query", params={"q": soql}, headers=await self._auth())
        return data.get("records", [])

    async def upsert_account(self, acct: AccountData) -> str:
        if self.mock:
            existing = self.accounts.get(acct.domain)
            sid = existing["id"] if existing else f"001{next(self._ids):012d}"
            self.accounts[acct.domain] = {"id": sid, "name": acct.name, "domain": acct.domain}
            return sid
        await self._request(
            "PATCH",
            f"{API}/sobjects/Account/Domain__c/{acct.domain}",
            json={"Name": acct.name, "Industry": acct.industry},
            headers=await self._auth(),
        )
        recs = await self.query(f"SELECT Id FROM Account WHERE Domain__c = '{acct.domain}' LIMIT 1")
        return recs[0]["Id"] if recs else ""

    async def upsert_contact(self, c: ContactData, account_id: str | None = None) -> str:
        assert c.email, "contact email required"
        if self.mock:
            existing = self.contacts.get(c.email)
            sid = existing["id"] if existing else f"003{next(self._ids):012d}"
            self.contacts[c.email] = {"id": sid, "email": c.email, "account_id": account_id}
            return sid
        data = await self._request(
            "POST",
            f"{API}/sobjects/Contact",
            json={"Email": c.email, "FirstName": c.first_name, "LastName": c.last_name or "Unknown", "AccountId": account_id},
            headers=await self._auth(),
        )
        return data["id"]

    async def create_task(self, subject: str, due_date: str | None = None, account_id: str | None = None) -> str:
        if self.mock:
            tid = f"00T{next(self._ids):012d}"
            self.tasks.append({"id": tid, "subject": subject, "due_date": due_date, "account_id": account_id})
            return tid
        data = await self._request(
            "POST",
            f"{API}/sobjects/Task",
            json={"Subject": subject, "ActivityDate": due_date, "WhatId": account_id},
            headers=await self._auth(),
        )
        return data["id"]

    async def log_email(self, contact_id: str, subject: str, body_text: str, sent_at: datetime) -> str:
        if self.mock:
            aid = f"00T{next(self._ids):012d}"
            self.activities.append({"id": aid, "contact_id": contact_id, "subject": subject, "sent_at": sent_at})
            return aid
        data = await self._request(
            "POST",
            f"{API}/sobjects/Task",
            json={"Subject": f"Email: {subject}", "Description": body_text, "WhoId": contact_id, "Status": "Completed"},
            headers=await self._auth(),
        )
        return data["id"]
