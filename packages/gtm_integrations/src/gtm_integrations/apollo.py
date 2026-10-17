"""Apollo.io REST client + deterministic mock backed by the synthetic dataset."""

from gtm_core.config import get_settings
from gtm_core.schemas import AccountData, ContactData

from gtm_integrations.base import BaseClient
from gtm_integrations.fixtures import dataset


class ApolloClient(BaseClient):
    name = "apollo"

    def __init__(self, api_key: str | None = None, **kw):
        super().__init__(api_key or get_settings().apollo_api_key, base_url="https://api.apollo.io", **kw)

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    async def search_companies(self, filters: dict, limit: int = 25) -> list[AccountData]:
        """filters: industries, headcount_min/max, tech_keywords, funding_stages."""
        if self.mock:
            return self._mock_companies(filters)[:limit]
        body = {
            "per_page": limit,
            "organization_num_employees_ranges": [
                f"{filters.get('headcount_min', 1)},{filters.get('headcount_max', 100000)}"
            ],
            "currently_using_any_of_technology_uids": [
                t.lower() for t in filters.get("tech_keywords", [])
            ],
        }
        data = await self._request("POST", "/api/v1/mixed_companies/search", json=body, headers=self._headers())
        return [
            AccountData(
                domain=o.get("primary_domain", ""),
                name=o.get("name", ""),
                industry=o.get("industry"),
                employee_count=o.get("estimated_num_employees"),
                tech_stack=[t.get("name", "") for t in o.get("current_technologies", [])],
            )
            for o in data.get("organizations", data.get("accounts", []))
            if o.get("primary_domain")
        ]

    async def get_company(self, domain: str) -> AccountData | None:
        if self.mock:
            return next((a for a in dataset.accounts() if a.domain == domain), None)
        data = await self._request(
            "GET", "/api/v1/organizations/enrich", params={"domain": domain}, headers=self._headers()
        )
        o = data.get("organization")
        if not o:
            return None
        return AccountData(domain=domain, name=o.get("name", domain), industry=o.get("industry"),
                           employee_count=o.get("estimated_num_employees"))

    async def search_people(self, domain: str, titles: list[str] | None = None, limit: int = 3) -> list[ContactData]:
        if self.mock:
            people = dataset.contacts().get(domain, [])
            if titles:
                wanted = [t.lower() for t in titles]
                people = [p for p in people if any(w in (p.title or "").lower() for w in wanted)]
            return people[:limit]
        body = {"q_organization_domains": domain, "person_titles": titles or [], "per_page": limit}
        data = await self._request("POST", "/api/v1/mixed_people/search", json=body, headers=self._headers())
        return [
            ContactData(
                email=p.get("email"),
                first_name=p.get("first_name"),
                last_name=p.get("last_name"),
                title=p.get("title"),
                seniority=p.get("seniority"),
                linkedin_url=p.get("linkedin_url"),
                apollo_person_id=p.get("id"),
                email_status="verified" if p.get("email_status") == "verified" else "unverified",
                company_domain=domain,
            )
            for p in data.get("people", [])
        ]

    async def match_person(self, email: str) -> ContactData | None:
        if self.mock:
            for people in dataset.contacts().values():
                for p in people:
                    if p.email == email:
                        return p
            return None
        data = await self._request("POST", "/api/v1/people/match", json={"email": email}, headers=self._headers())
        p = data.get("person")
        if not p:
            return None
        return ContactData(
            email=p.get("email"),
            first_name=p.get("first_name"),
            last_name=p.get("last_name"),
            title=p.get("title"),
            apollo_person_id=p.get("id"),
        )

    @staticmethod
    def _mock_companies(filters: dict) -> list[AccountData]:
        inds = {i.lower() for i in filters.get("industries", [])}
        techs = {t.lower() for t in filters.get("tech_keywords", [])}
        stages = {s.lower() for s in filters.get("funding_stages", [])}
        lo, hi = filters.get("headcount_min", 0), filters.get("headcount_max", 10**9)
        out = []
        for a in dataset.accounts():
            if inds and (a.industry or "").lower() not in inds:
                continue
            if stages and (a.funding_stage or "").lower() not in stages:
                continue
            if not lo <= (a.employee_count or 0) <= hi:
                continue
            if techs and not techs.issubset({t.lower() for t in a.tech_stack}):
                continue
            out.append(a)
        return out
