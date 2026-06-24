"""BetterCo REST API client — wraps the verified 8-step onboarding flow."""

import os
import time
import hashlib
import logging
import datetime
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("betterco")

BASE_URL = os.getenv("BETTERCO_BASE_URL", "https://editor.betterco.ai/bcapi")
API_KEY = os.getenv("BETTERCO_API_KEY")
API_SECRET = os.getenv("BETTERCO_API_SECRET")
WORKSPACE_ID = os.getenv("BETTERCO_WORKSPACE_ID")
ORG_ID = os.getenv("BETTERCO_ORG_ID")

# URL prefix for org-scoped endpoints
_ORG = f"/restapi/v1/workspaces/{WORKSPACE_ID}/organizations/{ORG_ID}"


class BetterCoClient:
    """Thin wrapper around the BetterCo REST API."""

    def __init__(self, base_url=None, api_key=None, api_secret=None,
                 workspace_id=None, org_id=None, user_email=None,
                 user_password=None):
        # Instance-scoped config (env globals are the fallback defaults), so a
        # single process can hold one client per workspace and switch at runtime.
        self.base_url = base_url or BASE_URL
        self.api_key = api_key or API_KEY
        self.api_secret = api_secret or API_SECRET
        self.workspace_id = workspace_id or WORKSPACE_ID
        self.org_id = org_id or ORG_ID
        self.user_email = user_email or os.getenv("BETTERCO_USER_EMAIL")
        self.user_password = user_password or os.getenv("BETTERCO_USER_PASSWORD")
        self._org = f"/restapi/v1/workspaces/{self.workspace_id}/organizations/{self.org_id}"
        self.token = None
        self.token_expiry = 0
        self.session = requests.Session()
        self.session.verify = os.getenv("BETTERCO_SSL_VERIFY", "true").lower() not in ("false", "0", "no")

    @classmethod
    def from_workspace_env(cls, env_path):
        """Build a client scoped to a workspace credential file (workspaces/<name>.env)."""
        from dotenv import dotenv_values
        v = dotenv_values(env_path)
        client = cls(
            base_url=v.get("BETTERCO_BASE_URL"),
            api_key=v.get("BETTERCO_API_KEY"),
            api_secret=v.get("BETTERCO_API_SECRET"),
            workspace_id=v.get("BETTERCO_WORKSPACE_ID"),
            org_id=v.get("BETTERCO_ORG_ID"),
            user_email=v.get("BETTERCO_USER_EMAIL"),
            user_password=v.get("BETTERCO_USER_PASSWORD"),
        )
        ssl = (v.get("BETTERCO_SSL_VERIFY") or "true").lower()
        client.session.verify = ssl not in ("false", "0", "no")
        return client

    # ── Auth ─────────────────────────────────────────────────────────
    def _ensure_auth(self):
        if self.token and time.time() < self.token_expiry - 60:
            return
        url = f"{self.base_url}/restapi/v1/auth/login"
        r = self.session.post(url, json={"key": self.api_key, "secret": self.api_secret})
        r.raise_for_status()
        data = r.json()
        self.token = data["token"]
        # Tokens are valid for 1h; refresh 5min early
        self.token_expiry = time.time() + 3600 - 300
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        log.info("Authenticated (token valid ~55min)")

    def _url(self, path: str, org_id: str = None) -> str:
        if org_id:
            prefix = f"/restapi/v1/workspaces/{self.workspace_id}/organizations/{org_id}"
            return f"{self.base_url}{prefix}{path}"
        return f"{self.base_url}{self._org}{path}"

    def set_org(self, org_id: str):
        """Switch the default organization context."""
        self.org_id = org_id
        self._org = f"/restapi/v1/workspaces/{self.workspace_id}/organizations/{org_id}"
        log.info("Switched to organization %s", org_id)

    # ── Step 1: Create customer ──────────────────────────────────────
    def create_customer(self, legal_name: str, customer_type: str = "ENTITY",
                        legal_info: dict = None) -> str:
        """Create minimal customer, return customer ID. customer_type is
        ENTITY (default) or INDIVIDUAL — set it at creation since type cannot
        be changed by a later legalInfo PATCH. For INDIVIDUAL the API requires
        firstName; pass it via legal_info (falls back to legal_name)."""
        self._ensure_auth()
        li = {"legalName": legal_name}
        if legal_info:
            li.update({k: v for k, v in legal_info.items() if v is not None})
        if customer_type == "INDIVIDUAL" and not li.get("firstName"):
            li["firstName"] = legal_name
        body = {"type": customer_type, "legalInfo": li}
        r = self.session.post(self._url("/customers"), json=body)
        r.raise_for_status()
        cid = r.json()["id"]
        log.info("Created customer %s (%s)", cid, legal_name)
        return cid

    # ── Step 2: Update customer (PATCH) ──────────────────────────────
    def update_customer(self, cid: str, patch_body: dict) -> dict:
        """PATCH customer with legalInfo, addressData, additionalData etc."""
        self._ensure_auth()
        r = self.session.patch(self._url(f"/customers/{cid}"), json=patch_body)
        if r.status_code >= 400:
            log.error("PATCH failed (%d): %s", r.status_code, r.text[:500])
        r.raise_for_status()
        log.info("Patched customer %s", cid)
        return r.json()

    # GwG/KYC risk fields writable on the ACTOR via REST PATCH /customers/{cid}.
    # This is actor.riskSummary — a DIFFERENT storage location than the
    # process-level riskSummary written by PATCH /api/client/onboarding (FullData,
    # approval-gated, blocked in update_full_data). Setting these here is NOT
    # approval-gated.
    _ACTOR_RISK_SUMMARY_KEYS = frozenset({
        "identificationDate",                    # LocalDate yyyy-MM-dd — GwG KYC date (fallback for lastKycDate)
        "identificationComment",
        "kycStatus",                             # COMPLETED | NOT_COMPLETED | NOT_STARTED
        "riskSummaryManualClassificationType",   # LOW | MEDIUM | HIGH
        "riskSummaryApprovedDate",               # LocalDate
        "riskSummaryReasons",
        "transparencyRegisterCheckStatus",       # NO_INSPECTION | CONSISTENT | DISCREPANCIES
        "transparencyRegisterCheckedOn",         # LocalDate
        "transparencyRegisterComment",
    })

    def set_actor_risk_summary(self, cid: str, **fields) -> dict:
        """Write GwG/KYC risk data to actor.riskSummary via REST PATCH /customers/{cid}.

        NOT approval-gated (unlike the process-level riskSummary in FullData).
        Only the fields passed are written; the rest are untouched. Requires
        write on BusinessRelation, and the customer must NOT yet have an
        OPEN/initialized matter (REST PATCH 500s under the edit lock).

        Allowed fields: identificationDate, identificationComment, kycStatus
        (COMPLETED|NOT_COMPLETED|NOT_STARTED), riskSummaryManualClassificationType
        (LOW|MEDIUM|HIGH), riskSummaryApprovedDate, riskSummaryReasons,
        transparencyRegisterCheckStatus (NO_INSPECTION|CONSISTENT|DISCREPANCIES),
        transparencyRegisterCheckedOn, transparencyRegisterComment."""
        bad = sorted(k for k in fields if k not in self._ACTOR_RISK_SUMMARY_KEYS)
        if bad:
            raise ValueError(
                f"Unknown actor.riskSummary field(s): {bad}. "
                f"Allowed: {sorted(self._ACTOR_RISK_SUMMARY_KEYS)}")
        return self.update_customer(cid, {"riskSummary": dict(fields)})

    # AML risk-profile fields on actor.amlProfile.riskProfile — fully readable AND
    # writable via REST PATCH /customers/{cid} (NOT FullData-only). On REST,
    # taxGeography is a RAW ISO alpha-2 code ("DE"); in FullData the top-level
    # riskProfile.taxGeography is a TaxGeography enum (DOMESTIC/EU_EEA/...).
    _AML_RISK_PROFILE_KEYS = frozenset({
        "isActiveDomestic", "inHighRiskCountry",
        "businessGeography",            # GEOGRAPHY_EU | GEOGRAPHY_ROW | GEOGRAPHY_EUROW
        "countryEU", "countryEEA", "countryThird",
        "countryListDE", "countryListEU", "countryListFATF", "countryListFIU",
        "isActiveFIUList", "isActiveFIUListEU", "isActiveFIUListROW",
        "countryListFIURealEstate",
        "activeFIUListExplanation", "activeFIUListEUExplanation",
        "activeFIUListROWExplanation",
        "taxAnomalies",                 # List<String>
        "taxGeography",                 # REST: raw ISO alpha-2 ("DE")
        "taxIndustry",                  # free-text label or WZ code
        "taxIndustryRisk",              # LOW|MID_TO_LOW|MID|HIGH|VERY_HIGH|NOT_PROFILED|NONE
        "anyPep",
        "kycNote",                      # free-text KYC note
        "riskCountry",                  # LOW|MEDIUM|HIGH — mirrors additionalActorData.riskFlag_High_03
    })

    def set_aml_risk_profile(self, cid: str, **fields) -> dict:
        """Write AML risk-profile fields to actor.amlProfile.riskProfile via REST
        PATCH /customers/{cid}. Fully writable on REST (NOT FullData-only).

        Only the fields passed are written; the rest of the actor is untouched.
        Setting `riskCountry` (LOW|MEDIUM|HIGH) auto-syncs
        additionalActorData.riskFlag_High_03 to the same value (and vice-versa if
        you instead PATCH additionalData [{key:'riskFlag_High_03', value:'HIGH'}]).
        On read, riskCountry falls back to riskFlag_High_03 when null.

        ⚠ taxGeography here is a RAW ISO alpha-2 code ("DE"). The FullData
          top-level riskProfile.taxGeography is a different TaxGeography ENUM
          (DOMESTIC|EU_EEA|AML_THIRD_COUNTRIES|HIGH_RISK_STATES) — don't mix them.
        Allowed fields: isActiveDomestic, inHighRiskCountry, businessGeography,
        countryEU/EEA/Third, countryListDE/EU/FATF/FIU, isActiveFIUList[/EU/ROW],
        countryListFIURealEstate, activeFIUList[/EU/ROW]Explanation, taxAnomalies,
        taxGeography, taxIndustry, taxIndustryRisk, anyPep, kycNote, riskCountry."""
        bad = sorted(k for k in fields if k not in self._AML_RISK_PROFILE_KEYS)
        if bad:
            raise ValueError(
                f"Unknown amlProfile.riskProfile field(s): {bad}. "
                f"Allowed: {sorted(self._AML_RISK_PROFILE_KEYS)}")
        return self.update_customer(cid, {"amlProfile": {"riskProfile": dict(fields)}})

    # ── Step 3: Create contact (PUT!) ────────────────────────────────
    def create_contact(self, cid: str, contact_body: dict) -> str:
        """PUT a contact on the customer. Returns contact ID."""
        self._ensure_auth()
        r = self.session.put(self._url(f"/customers/{cid}/contacts"), json=contact_body)
        if r.status_code >= 400:
            log.error("Contact creation failed (%d): %s", r.status_code, r.text[:500])
        r.raise_for_status()
        contact_id = r.json().get("id", "unknown")
        log.info("Created contact %s on customer %s", contact_id, cid)
        return contact_id

    # ── Step 4: Upload customer-level documents ──────────────────────
    def upload_customer_document(self, cid: str, file_path: str, name: str, doc_type: str) -> dict:
        """Upload a KYC source document (HR, Gesellschafterliste, TR) to customer."""
        self._ensure_auth()
        import mimetypes
        # Explicit per-file MIME type — else the server stores application/octet-stream and the
        # browser inline viewer refuses to preview (download-only). .pdf -> application/pdf.
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/pdf"
        with open(file_path, "rb") as f:
            files = {"file": (Path(file_path).name, f, ctype)}
            data = {"name": name, "type": doc_type}
            # Remove JSON content-type for multipart
            headers = {k: v for k, v in self.session.headers.items() if k.lower() != "content-type"}
            r = requests.post(
                self._url(f"/customers/{cid}/documents"),
                files=files, data=data, headers=headers,
                verify=self.session.verify,
            )
        r.raise_for_status()
        log.info("Uploaded customer doc '%s' (%s)", name, doc_type)
        return r.json() if r.text else {}

    # ── Step 5: Create case ──────────────────────────────────────────
    def create_case(self, cid: str, case_name: str) -> str:
        """Create a case/matter under the customer. Returns case ID."""
        self._ensure_auth()
        r = self.session.post(self._url(f"/customers/{cid}/cases"), json={"name": case_name})
        r.raise_for_status()
        case_id = r.json()["id"]
        log.info("Created case %s (%s)", case_id, case_name)
        return case_id

    # ── Step 6: Create process ───────────────────────────────────────
    def create_process(self, cid: str, case_id: str, process_name: str = "F1800_OnboardingEntity_A") -> dict:
        """Create an onboarding process under a case. Returns {id, ...}."""
        self._ensure_auth()
        body = {"name": process_name, "isInitiator": True}
        r = self.session.post(
            self._url(f"/customers/{cid}/cases/{case_id}/processes"), json=body,
        )
        r.raise_for_status()
        data = r.json()
        log.info("Created process %s", data.get("id"))
        return data

    # ── Step 7: Upload process-level documents ───────────────────────
    def upload_process_document(self, cid: str, case_id: str, pid: str, file_path: str, name: str, doc_type: str) -> dict:
        """Upload a document to the process (P1875 'Sonstige Dokumente'). doc_type must use OTHERKYCDOCS_ prefix."""
        self._ensure_auth()
        import mimetypes
        # Explicit per-file MIME type — else the server stores application/octet-stream and the browser
        # inline viewer refuses to preview (download-only). .pdf -> application/pdf. (Same fix as
        # upload_customer_document; process-level upload had been missing it.)
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/pdf"
        with open(file_path, "rb") as f:
            files = {"file": (Path(file_path).name, f, ctype)}
            data = {"name": name, "type": doc_type}
            headers = {k: v for k, v in self.session.headers.items() if k.lower() != "content-type"}
            r = requests.post(
                self._url(f"/customers/{cid}/cases/{case_id}/processes/{pid}/documents"),
                files=files, data=data, headers=headers,
                verify=self.session.verify,
            )
        r.raise_for_status()
        log.info("Uploaded process doc '%s' (%s)", name, doc_type)
        return r.json() if r.text else {}

    # ── Step 8: Upload ID documents (Client API) ─────────────────────
    def get_process_token(self, cid: str, case_id: str, pid: str) -> str:
        """Get or generate a process token for Client API identity endpoint."""
        self._ensure_auth()
        # Generate a fresh token via POST (most reliable)
        r = self.session.post(
            self._url(f"/customers/{cid}/cases/{case_id}/processes/{pid}/token"),
            json={},
        )
        r.raise_for_status()
        token = r.json().get("token")
        if not token:
            # Fallback: extract from process details
            r2 = self.session.get(self._url(f"/customers/{cid}/cases/{case_id}/processes/{pid}"))
            r2.raise_for_status()
            token_data = r2.json().get("token", {})
            token = token_data.get("token") if isinstance(token_data, dict) else token_data
        log.info("Got process token for %s", pid)
        return token

    def upload_id_document(self, process_token: str, pid: str, contact_id: str, file_path: str, id_doc_type: str) -> dict:
        """Upload an ID document via the Client API identity endpoint.

        id_doc_type: PASSPORT, ID_CARD_FRONT, ID_CARD_BACK, ID_CARD_BOTH_IN_FILE, UTILITY
        """
        url = f"{self.base_url}/api/client/onboarding/documents/identity"
        with open(file_path, "rb") as f:
            files = {"file": (Path(file_path).name, f)}
            data = {
                "processId": pid,
                "contactId": contact_id,
                "idDocType": id_doc_type,
            }
            headers = {"Authorization": f"Bearer {process_token}"}
            r = requests.patch(url, files=files, data=data, headers=headers,
                               verify=self.session.verify)
        if r.status_code >= 400:
            log.error("ID doc upload failed (%d): %s", r.status_code, r.text[:500])
        r.raise_for_status()
        log.info("Uploaded ID doc %s for contact %s (%s)", id_doc_type, contact_id, Path(file_path).name)
        return r.json() if r.text else {}

    # ── Share link (standalone client onboarding URL) ────────────────
    def create_share_link(self, cid: str, case_id: str, pid: str,
                          expiration_period: str = "P3650D",
                          language: str = "DEF",
                          is_initiator: bool = False) -> str:
        """Create a standalone share link for a process (ANONYMOUS_USER token).

        Returns the full URL that can be shared with a client.
        Default expiration is ~10 years (effectively lives until process is closed).
        Language: EN, DE (informal du), DEF (formal Sie), DEG (Swiss German).

        is_initiator: which relation side the runner renders. Default False = the
        TARGET (customer-fill) side. True = the INITIATOR (client/advisor) side —
        the shares body needs `isInitiator=true`.
        """
        self._ensure_auth()
        r = self.session.post(
            self._url(f"/customers/{cid}/cases/{case_id}/processes/{pid}/shares"),
            json={"expiration_period": expiration_period, "language": language,
                  "isInitiator": is_initiator},
        )
        r.raise_for_status()
        share_id = r.json()["id"]
        # Fetch the share to get the full URL
        r2 = self.session.get(
            self._url(f"/customers/{cid}/cases/{case_id}/processes/{pid}/shares/{share_id}"),
        )
        r2.raise_for_status()
        url = r2.json()["url"]
        log.info("Created share link for process %s (expires %s)", pid, r2.json().get("expiration"))
        return url

    # ── Helpers ───────────────────────────────────────────────────────
    def get_customer(self, cid: str) -> dict:
        self._ensure_auth()
        r = self.session.get(self._url(f"/customers/{cid}"))
        r.raise_for_status()
        return r.json()

    def list_contacts(self, cid: str) -> list:
        """List all contacts under a customer (paginated, fetches all pages)."""
        self._ensure_auth()
        all_contacts = []
        url = self._url(f"/customers/{cid}/contacts")
        while url:
            r = self.session.get(url)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "results" in data:
                all_contacts.extend(data["results"])
                url = data.get("next")
            else:
                return data
        return all_contacts

    def get_contact_detail(self, contact_url: str) -> dict:
        """Fetch full contact detail via its URL."""
        self._ensure_auth()
        r = self.session.get(contact_url)
        r.raise_for_status()
        return r.json()

    def list_relations(self, cid: str) -> list:
        """List all active relations for a customer via GET /api/relations (KYC contacts view)."""
        company_id = self.get_entity_actor_id(cid)
        params = {
            "workspaceId": self.workspace_id,
            "resourceId": cid,
            "companyId": company_id,
            "page": 0,
            "step": 200,
            "sortField": "contactName",
            "sortOrder": "ASC",
            "types": "",
            "from": "",
            "to": "",
            "relationsTypes": "",
            "statuses": "",
            "relationStatuses": "",
            "createdBy": "",
            "isCurrent": True,
            "statusTypes": "",
            "searchQuery": "",
        }
        log.info("GET %s/api/relations params=%s", self.base_url, params)
        r = requests.get(
            f"{self.base_url}/api/relations",
            headers=self._user_headers(),
            params=params,
            verify=self.session.verify,
        )
        log.info("GET /api/relations -> %d %s", r.status_code, r.text[:300])
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data.get("results") or data.get("content") or data.get("relations") or []
        return data if isinstance(data, list) else []

    def delete_relation(self, relation_id: str, cid: str) -> int:
        """Delete a relation by ID via DELETE /api/relations/{id}."""
        company_id = self.get_entity_actor_id(cid)
        r = requests.delete(
            f"{self.base_url}/api/relations/{relation_id}",
            headers=self._user_headers(),
            params={"companyId": company_id},
            verify=self.session.verify,
        )
        if r.status_code >= 400:
            log.error("delete_relation failed (%d): %s", r.status_code, r.text[:400])
        r.raise_for_status()
        return r.status_code

    def add_contact_relation(self, cid: str, contact_id: str, relation_code: str) -> dict:
        company_id = self.get_entity_actor_id(cid)
        body = {
            "contactId": contact_id,
            "resourceId": cid,
            "relationType": relation_code,
            "relationCategoryId": "1150",
            "relationTypeId": relation_code,
            "title": "",
            "relationStatus": {
                "statusType": "ACTIVE",
                "isCurrent": True,
                "current": True,
                "statusDate": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            },
        }
        r = requests.put(
            f"{self.base_url}/api/relations",
            headers=self._user_headers(),
            params={"companyId": company_id, "contactId": contact_id},
            json=body,
            verify=self.session.verify,
        )
        if r.status_code >= 400:
            log.error("add_contact_relation failed (%d): %s", r.status_code, r.text[:400])
        r.raise_for_status()
        return r.json() if r.text else {}

    def get_process(self, cid: str, case_id: str, pid: str) -> dict:
        self._ensure_auth()
        r = self.session.get(self._url(f"/customers/{cid}/cases/{case_id}/processes/{pid}"))
        r.raise_for_status()
        return r.json()

    def list_organizations(self) -> list:
        """List all organizations in the workspace."""
        self._ensure_auth()
        r = self.session.get(f"{self.base_url}/restapi/v1/workspaces/{self.workspace_id}/organizations")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def list_customers(self, org_id: str = None) -> list:
        """List all customers in an organization (paginated, fetches all pages)."""
        self._ensure_auth()
        oid = org_id or self.org_id
        all_customers = []
        url = f"{self.base_url}/restapi/v1/workspaces/{self.workspace_id}/organizations/{oid}/customers?size=50"
        while url:
            r = self.session.get(url)
            r.raise_for_status()
            data = r.json()
            all_customers.extend(data.get("results", []))
            url = data.get("next")
        return all_customers

    def delete_customer(self, cid: str) -> bool:
        """Delete a customer by ID. Returns True if deleted."""
        self._ensure_auth()
        r = self.session.delete(self._url(f"/customers/{cid}"))
        if r.status_code == 404:
            log.warning("Customer %s not found (already deleted?)", cid)
            return False
        r.raise_for_status()
        log.info("Deleted customer %s", cid)
        return True

    def delete_customer_document(self, cid: str, doc_id: str) -> bool:
        """Delete a single customer document by ID (REST). Returns True if deleted.
        Used to REPLACE a slot doc: upload the new file, then delete the stale same-type one."""
        self._ensure_auth()
        r = self.session.delete(self._url(f"/customers/{cid}/documents/{doc_id}"))
        if r.status_code == 404:
            log.warning("Document %s not found on %s", doc_id, cid)
            return False
        r.raise_for_status()
        log.info("Deleted customer doc %s on %s", doc_id, cid)
        return True

    def delete_process_document(self, cid: str, case_id: str, pid: str, doc_id: str) -> bool:
        """Delete a single process-level document by ID (REST). Returns True if deleted.
        Used to REPLACE a process 'Sonstige Dokumente' file (uploads append — delete the stale one)."""
        self._ensure_auth()
        r = self.session.delete(
            self._url(f"/customers/{cid}/cases/{case_id}/processes/{pid}/documents/{doc_id}"))
        if r.status_code == 404:
            log.warning("Process document %s not found on %s", doc_id, cid)
            return False
        r.raise_for_status()
        log.info("Deleted process doc %s on %s", doc_id, cid)
        return True

    def find_customers_by_criteria(self, name_contains: str = None,
                                    name_has_4digits: bool = False) -> list:
        """Find customers matching criteria.

        Criteria (OR logic — matches any):
          name_contains: customer name includes this substring (case-insensitive)
          name_has_4digits: customer name contains a 4-digit number (e.g. "CheckAlpha 3330 GmbH")

        Returns list of {id, legalName, matched_rule}.
        """
        import re
        customers = self.list_customers()
        matches = []
        for c in customers:
            name = c.get("displayName", "") or (c.get("legalInfo", {}) or {}).get("legalName", "") or ""
            reasons = []
            if name_contains and name_contains.lower() in name.lower():
                reasons.append(f"name contains '{name_contains}'")
            if name_has_4digits and re.search(r"\d{4}", name):
                reasons.append("name has 4-digit number")
            if reasons:
                matches.append({
                    "id": c["id"],
                    "legalName": name,
                    "matched_rules": reasons,
                })
        return matches

    def delete_customers_by_criteria(self, name_contains: str = None,
                                      name_has_4digits: bool = False,
                                      dry_run: bool = True) -> dict:
        """Find and delete customers matching criteria.

        Args:
            name_contains: delete if name includes this substring (case-insensitive)
            name_has_4digits: delete if name contains a 4-digit number
            dry_run: if True, only list matches without deleting (default: True)

        Returns dict with {matched, deleted, failed, details}.
        """
        matches = self.find_customers_by_criteria(name_contains, name_has_4digits)
        result = {"matched": len(matches), "deleted": 0, "failed": 0,
                  "dry_run": dry_run, "details": []}
        for m in matches:
            entry = {"id": m["id"], "name": m["legalName"],
                     "rules": m["matched_rules"]}
            if dry_run:
                entry["action"] = "would_delete"
            else:
                try:
                    self.delete_customer(m["id"])
                    entry["action"] = "deleted"
                    result["deleted"] += 1
                except Exception as e:
                    entry["action"] = "failed"
                    entry["error"] = str(e)
                    result["failed"] += 1
            result["details"].append(entry)
        return result

    def list_cases(self, cid: str) -> list:
        """List cases under a customer."""
        self._ensure_auth()
        r = self.session.get(self._url(f"/customers/{cid}/cases"))
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def list_processes(self, cid: str, case_id: str) -> list:
        """List processes under a case."""
        self._ensure_auth()
        r = self.session.get(self._url(f"/customers/{cid}/cases/{case_id}/processes"))
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def list_all_processes(self) -> dict:
        """List all processes across all customers in the workspace.

        Returns dict with:
        - processes: list of {process_id, status, customer_name, customer_id,
                              flow_name, case_name, tasks_completed, tasks_total, updated_at, updated_by}
        - elapsed_ms: total wall-clock time in milliseconds
        - api_calls: number of API calls made
        """
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        t_start = time.time()
        api_calls = 0

        # Step 1: list customers → cases → process refs (lightweight)
        process_refs = []
        customers = self.list_customers()
        api_calls += 1
        for cust in customers:
            cid = cust.get("id")
            if not cid:
                continue
            cust_name = cust.get("legalInfo", {}).get("legalName", cid)
            cases = self.list_cases(cid)
            api_calls += 1
            for case in cases:
                case_id = case.get("id")
                if not case_id:
                    continue
                procs = self.list_processes(cid, case_id)
                api_calls += 1
                for p in procs:
                    di = p.get("displayInformation", {})
                    process_refs.append({
                        "customer_id": cid,
                        "customer_name": di.get("customerName", cust_name),
                        "case_id": case_id,
                        "case_name": di.get("caseName", ""),
                        "process_id": p["id"],
                        "flow_name": di.get("processName", {}).get("enValue", p.get("displayName", "")),
                        "url": p.get("url"),
                    })

        # Step 2: fetch each process detail in parallel for status + progress
        def _fetch_detail(ref):
            self._ensure_auth()
            r = self.session.get(ref["url"])
            r.raise_for_status()
            return ref, r.json()

        results = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_detail, ref): ref for ref in process_refs}
            for fut in as_completed(futures):
                api_calls += 1
                try:
                    ref, detail = fut.result()
                    results.append({
                        "process_id": ref["process_id"],
                        "status": detail.get("status", "?"),
                        "customer_name": ref["customer_name"],
                        "customer_id": ref["customer_id"],
                        "flow_name": ref["flow_name"],
                        "case_name": ref["case_name"],
                        "tasks_completed": detail.get("numberOfCompletedTasks", 0),
                        "tasks_total": detail.get("totalTasks", 0),
                        "updated_at": detail.get("updatedAt", ""),
                        "updated_by": detail.get("updatedBy", ""),
                    })
                except Exception as e:
                    ref = futures[fut]
                    log.warning("Failed to fetch process %s: %s", ref["process_id"], e)

        # Sort by customer name, then flow name
        results.sort(key=lambda r: (r["customer_name"], r["flow_name"]))

        elapsed_ms = int((time.time() - t_start) * 1000)
        return {"processes": results, "elapsed_ms": elapsed_ms, "api_calls": api_calls}

    def list_all_processes_detail(self) -> dict:
        """List all processes with detailed fields for reporting.

        Returns dict with:
        - processes: list of {creation_date, customer_name, first_name, last_name,
                              customer_type, flow_name, wegen, bemerkung, status,
                              process_id, tasks_completed, tasks_total}
        - elapsed_ms: wall-clock time in milliseconds
        - api_calls: number of API calls made

        Wegen/Bemerkung come from the 'Second Page' process (F60045) linked
        to the main process via the same case. Fields are additionalData.because
        and additionalData.remark.
        """
        import time
        from datetime import datetime, timezone
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _oid_to_date(oid: str) -> str:
            dt = datetime.fromtimestamp(int(oid[:8], 16), tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M")

        t_start = time.time()
        api_calls = 0

        # Step 1: list customers with detail (need type, firstName, lastName)
        customers = self.list_customers()
        api_calls += 1
        cust_detail = {}

        def _fetch_customer(c):
            self._ensure_auth()
            r = self.session.get(c["url"])
            r.raise_for_status()
            return c["id"], r.json()

        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(_fetch_customer, c): c for c in customers if c.get("url")}
            for fut in as_completed(futs):
                api_calls += 1
                try:
                    cid, detail = fut.result()
                    cust_detail[cid] = detail
                except Exception:
                    pass

        # Step 2: list cases + processes per customer
        case_processes = []  # (cid, case_id, process_ref_list)

        def _fetch_cases_and_procs(cid):
            self._ensure_auth()
            results = []
            r = self.session.get(self._url(f"/customers/{cid}/cases"))
            r.raise_for_status()
            cases = r.json().get("results", []) if isinstance(r.json(), dict) else r.json()
            calls = 1
            for case in cases:
                case_id = case["id"]
                r2 = self.session.get(self._url(f"/customers/{cid}/cases/{case_id}/processes"))
                r2.raise_for_status()
                procs = r2.json().get("results", []) if isinstance(r2.json(), dict) else r2.json()
                calls += 1
                results.append((cid, case_id, procs))
            return results, calls

        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(_fetch_cases_and_procs, c["id"]): c for c in customers}
            for fut in as_completed(futs):
                try:
                    results, calls = fut.result()
                    api_calls += calls
                    case_processes.extend(results)
                except Exception:
                    pass

        # Step 3: fetch all process details in parallel
        all_proc_refs = []
        for cid, case_id, procs in case_processes:
            for p in procs:
                all_proc_refs.append((cid, case_id, p))

        proc_details = {}  # process_id -> detail

        def _fetch_proc(ref):
            self._ensure_auth()
            _, _, p = ref
            r = self.session.get(p["url"])
            r.raise_for_status()
            return p["id"], r.json()

        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(_fetch_proc, ref): ref for ref in all_proc_refs}
            for fut in as_completed(futs):
                api_calls += 1
                try:
                    pid, detail = fut.result()
                    proc_details[pid] = detail
                except Exception:
                    pass

        # Step 4: build result rows — match Second Page processes to main processes via case
        # Group processes by (customer_id, case_id)
        case_groups = {}
        for cid, case_id, procs in case_processes:
            for p in procs:
                pid = p["id"]
                if pid in proc_details:
                    case_groups.setdefault((cid, case_id), []).append(proc_details[pid])

        results = []
        for (cid, case_id), procs_in_case in case_groups.items():
            # Find Second Page process(es) for wegen/bemerkung
            wegen = ""
            bemerkung = ""
            for proc in procs_in_case:
                name = proc.get("name", "")
                if "Page2" in name or "SearchEntityIndividual" in name:
                    ad = proc.get("data", {}).get("additionalData", {})
                    wegen = wegen or (ad.get("because", "") or "")
                    bemerkung = bemerkung or (ad.get("remark", "") or "")

            # Build rows for non-Second-Page processes
            cd = cust_detail.get(cid, {})
            li = cd.get("legalInfo", {})
            cust_type = cd.get("type", "")
            first_name = li.get("firstName", "")
            last_name = li.get("lastName", "")
            cust_name = li.get("legalName", "") or cd.get("displayName", cid)

            for proc in procs_in_case:
                name = proc.get("name", "")
                if "Page2" in name or "SearchEntityIndividual" in name:
                    continue  # skip the Second Page helper process
                di = proc.get("displayInformation", {})
                flow = di.get("processName", {}).get("enValue", name)
                pid = proc["id"]

                results.append({
                    "creation_date": _oid_to_date(pid),
                    "customer_name": cust_name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "customer_type": cust_type,
                    "flow_name": flow,
                    "wegen": wegen.strip(),
                    "bemerkung": bemerkung.strip(),
                    "status": proc.get("status", "?"),
                    "process_id": pid,
                    "tasks_completed": proc.get("numberOfCompletedTasks", 0),
                    "tasks_total": proc.get("totalTasks", 0),
                })

        results.sort(key=lambda r: r["creation_date"], reverse=True)
        elapsed_ms = int((time.time() - t_start) * 1000)
        return {"processes": results, "elapsed_ms": elapsed_ms, "api_calls": api_calls}

    def list_customer_documents(self, cid: str) -> list:
        """List documents at the customer level (KYC source docs)."""
        self._ensure_auth()
        r = self.session.get(self._url(f"/customers/{cid}/documents"))
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def list_process_documents(self, cid: str, case_id: str, pid: str) -> list:
        """List all documents at the process level (paginated)."""
        self._ensure_auth()
        all_docs = []
        url = self._url(f"/customers/{cid}/cases/{case_id}/processes/{pid}/documents")
        while url:
            r = self.session.get(url)
            r.raise_for_status()
            data = r.json()
            all_docs.extend(data.get("results", []))
            url = data.get("next")
        return all_docs

    def get_document_detail(self, doc_url: str) -> dict:
        """Get document metadata (fileName, type, downloadURI)."""
        self._ensure_auth()
        r = self.session.get(doc_url)
        r.raise_for_status()
        return r.json()

    def check_process_sla(self, cid: str, case_id: str, pid: str,
                          reminder_hours: float = 24,
                          escalation_hours: float = 72) -> dict:
        """Check SLA status for a process.

        Uses three timestamps to determine SLA clock:
        - Process creation (from ObjectID) — when company started the process
        - Share link creation — when company sent it to the client (ball passed)
        - Process updatedAt/updatedBy — last action by either side

        The SLA clock starts from the latest relevant handoff:
        - Waiting on client: max(share_link_created, last_company_update)
        - Waiting on company: last_client_update (updatedAt when updatedBy=anonymous)

        Returns dict with: waiting_on, idle_hours, sla_status, sla_clock_start, etc.
        """
        from datetime import datetime, timezone

        def _parse_ts(ts_str):
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(ts_str.rstrip("Z"), fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            return None

        def _oid_to_dt(oid):
            return datetime.fromtimestamp(int(oid[:8], 16), tz=timezone.utc)

        now = datetime.now(timezone.utc)

        proc = self.get_process(cid, case_id, pid)
        updated_by = proc.get("updatedBy", "")
        updated_at = proc.get("updatedAt", "")
        status = proc.get("status", "UNKNOWN")
        total = proc.get("totalTasks", 0)
        completed = proc.get("numberOfCompletedTasks", 0)
        open_tasks = proc.get("numberOfOpenTasks", 0)
        display = proc.get("displayInformation", {})
        customer_name = display.get("customerName", cid)

        last_update_ts = _parse_ts(updated_at) or now
        process_created_ts = _oid_to_dt(pid)

        # Get share link creation (the moment ball was passed to client)
        share_created_ts = None
        try:
            shares = self.list_shares(cid, case_id, pid)
            if shares:
                # Use the most recent valid share
                for s in sorted(shares, key=lambda x: x.get("creationDate", ""), reverse=True):
                    if s.get("valid"):
                        ts = _parse_ts(s["creationDate"])
                        if ts:
                            share_created_ts = ts
                            break
        except Exception:
            pass

        # Classify who acted last
        if updated_by.startswith("anonymous("):
            last_actor_side = "client"
            waiting_on = "company"
        elif "@" in updated_by:
            last_actor_side = "company"
            waiting_on = "client"
        else:
            last_actor_side = "company"
            waiting_on = "client"

        # Determine SLA clock start based on who we're waiting on
        if waiting_on == "client":
            # Client's clock starts from when they were given access:
            # the later of share link creation or last company update
            candidates = [last_update_ts]
            if share_created_ts:
                candidates.append(share_created_ts)
            sla_clock_start = max(candidates)
            sla_clock_reason = "share link created" if sla_clock_start == share_created_ts else "last company update"
        else:
            # Company's clock starts from when client last acted
            sla_clock_start = last_update_ts
            sla_clock_reason = "last client update"

        idle_hours = max(0, (now - sla_clock_start).total_seconds() / 3600)

        # SLA status
        if idle_hours >= escalation_hours:
            sla_status = "escalation"
        elif idle_hours >= reminder_hours:
            sla_status = "reminder"
        else:
            sla_status = "ok"

        return {
            "customer_name": customer_name,
            "process_id": pid,
            "process_status": status,
            "tasks_completed": completed,
            "tasks_total": total,
            "tasks_open": open_tasks,
            "last_actor": updated_by,
            "last_actor_side": last_actor_side,
            "waiting_on": waiting_on,
            "updated_at": updated_at,
            "process_created": process_created_ts.isoformat(),
            "share_created": share_created_ts.isoformat() if share_created_ts else None,
            "sla_clock_start": sla_clock_start.isoformat(),
            "sla_clock_reason": sla_clock_reason,
            "idle_hours": round(idle_hours, 1),
            "sla_status": sla_status,
            "reminder_threshold_h": reminder_hours,
            "escalation_threshold_h": escalation_hours,
        }

    def check_all_open_slas(self, reminder_hours: float = 24,
                            escalation_hours: float = 72) -> list:
        """Check SLA status for ALL open processes across all customers.

        Returns list of SLA results sorted by idle_hours descending (most overdue first).
        """
        results = []
        customers = self.list_customers()
        for cust in customers:
            cid = cust.get("id")
            if not cid:
                continue
            cases = self.list_cases(cid)
            for case in cases:
                case_id = case.get("id")
                if not case_id:
                    continue
                processes = self.list_processes(cid, case_id)
                for proc in processes:
                    pid = proc.get("id")
                    proc_status = proc.get("status", "")
                    if not pid or proc_status == "CLOSED":
                        continue
                    try:
                        sla = self.check_process_sla(cid, case_id, pid,
                                                     reminder_hours, escalation_hours)
                        sla["customer_id"] = cid
                        sla["case_id"] = case_id
                        results.append(sla)
                    except Exception as e:
                        log.warning("SLA check failed for process %s: %s", pid, e)

        results.sort(key=lambda r: r["idle_hours"], reverse=True)
        return results

    def list_shares(self, cid: str, case_id: str, pid: str) -> list:
        """List share links for a process."""
        self._ensure_auth()
        r = self.session.get(self._url(f"/customers/{cid}/cases/{case_id}/processes/{pid}/shares"))
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def get_process_timeline(self, cid: str, case_id: str, pid: str) -> list:
        """Build a chronological timeline of all events in a process.

        Collects events from: customer creation, case start, process creation,
        task completions, document uploads, share links, and last updates.
        Returns a sorted list of dicts: {timestamp, who, what, elapsed}.
        """
        from datetime import datetime, timezone

        def _oid_to_dt(oid: str) -> datetime:
            """Extract creation timestamp from a MongoDB ObjectID."""
            return datetime.fromtimestamp(int(oid[:8], 16), tz=timezone.utc)

        def _parse_ts(ts_str: str) -> datetime:
            """Parse an ISO timestamp string (with or without timezone)."""
            ts_str = ts_str.rstrip("Z")
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            raise ValueError(f"Cannot parse timestamp: {ts_str}")

        def _classify_user(user: str) -> str:
            """Classify updatedBy/createdBy into a readable label."""
            if not user:
                return "system"
            if user.startswith("anonymous("):
                return f"client (share link)"
            if "@" in user:
                return user.split("@")[0]
            return user  # service account name

        events = []

        # 1. Customer creation & last update
        cust = self.get_customer(cid)
        if "createdAt" in cust:
            events.append({
                "timestamp": _parse_ts(cust["createdAt"]),
                "who": _classify_user(cust.get("createdBy", "")),
                "what": f"Customer created: {cust.get('legalInfo', {}).get('legalName', cid)}",
            })
        if "updatedAt" in cust:
            events.append({
                "timestamp": _parse_ts(cust["updatedAt"]),
                "who": _classify_user(cust.get("updatedBy", "")),
                "what": "Customer last updated",
            })

        # 2. Case
        case_url = self._url(f"/customers/{cid}/cases/{case_id}")
        r = self.session.get(case_url)
        if r.ok:
            case = r.json()
            if "startDate" in case:
                events.append({
                    "timestamp": _parse_ts(case["startDate"]),
                    "who": "system",
                    "what": f"Case opened: {case.get('name', case_id)}",
                })

        # 3. Process creation (from ObjectID) & last update
        proc = self.get_process(cid, case_id, pid)
        events.append({
            "timestamp": _oid_to_dt(pid),
            "who": _classify_user(proc.get("createdBy", "")),
            "what": f"Process created: {proc.get('displayInformation', {}).get('processName', {}).get('enValue', proc.get('name', pid))}",
        })
        if "updatedAt" in proc:
            events.append({
                "timestamp": _parse_ts(proc["updatedAt"]),
                "who": _classify_user(proc.get("updatedBy", "")),
                "what": f"Process last updated ({proc.get('numberOfCompletedTasks', '?')}/{proc.get('totalTasks', '?')} tasks done)",
            })

        # 4. Tasks — ObjectID gives creation time only; API has no per-task
        #    updatedBy/completedAt, so who & when completed is unknown.
        for task in proc.get("tasks", []):
            status = task.get("status", "HIDDEN")
            spec = task.get("taskSpec", "")
            if status == "COMPLETED":
                events.append({
                    "timestamp": _oid_to_dt(task["id"]),
                    "who": "?",
                    "what": f"Task created+completed: {spec} (who/when completed unknown)",
                })

        # 5. Customer-level documents
        cust_docs = self.list_customer_documents(cid)
        for d in cust_docs:
            detail = self.get_document_detail(d["url"])
            ts = detail.get("uploadDate")
            if ts:
                events.append({
                    "timestamp": _parse_ts(ts),
                    "who": _classify_user(detail.get("uploadedBy", "")),
                    "what": f"Doc uploaded (customer): {detail.get('fileName', '?')} [{detail.get('type', '')}]",
                })

        # 6. Process-level documents
        proc_docs = self.list_process_documents(cid, case_id, pid)
        for d in proc_docs:
            detail = self.get_document_detail(d["url"])
            ts = detail.get("uploadDate")
            if ts:
                events.append({
                    "timestamp": _parse_ts(ts),
                    "who": _classify_user(detail.get("uploadedBy", "")),
                    "what": f"Doc uploaded (process): {detail.get('fileName', '?')} [{detail.get('type', '')}]",
                })

        # 7. Share links
        shares = self.list_shares(cid, case_id, pid)
        for s in shares:
            ts = s.get("creationDate")
            if ts:
                exp = s.get("expiration", "?")
                events.append({
                    "timestamp": _parse_ts(ts),
                    "who": _classify_user(s.get("createdBy", "")),
                    "what": f"Share link created (expires {exp})",
                })

        # Sort chronologically
        events.sort(key=lambda e: e["timestamp"])

        # Calculate elapsed time between consecutive events
        for i, ev in enumerate(events):
            if i == 0:
                ev["elapsed"] = "-"
            else:
                delta = ev["timestamp"] - events[i - 1]["timestamp"]
                total_secs = int(delta.total_seconds())
                if total_secs < 0:
                    ev["elapsed"] = "-"
                elif total_secs < 60:
                    ev["elapsed"] = f"{total_secs}s"
                elif total_secs < 3600:
                    ev["elapsed"] = f"{total_secs // 60}m {total_secs % 60}s"
                elif total_secs < 86400:
                    h = total_secs // 3600
                    m = (total_secs % 3600) // 60
                    ev["elapsed"] = f"{h}h {m}m"
                else:
                    d = total_secs // 86400
                    h = (total_secs % 86400) // 3600
                    ev["elapsed"] = f"{d}d {h}h"

        return events

    def complete_task(self, cid: str, case_id: str, pid: str, task_id: str) -> dict:
        """Complete (close) an open task on a process.

        Uses the editor internal API (POST /api/editor/processes/{pid}/tasks/{tid}/complete).
        Requires user-session auth — set BETTERCO_USER_EMAIL and BETTERCO_USER_PASSWORD
        in .env.  Falls back to API-key auth (which BetterCo currently rejects with 403).
        """
        self._ensure_auth()
        user_token = self._get_user_token()
        url = f"{self.base_url}/api/editor/processes/{pid}/tasks/{task_id}/complete"
        headers = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}
        r = requests.post(url, json={}, headers=headers, verify=self.session.verify)
        if r.status_code == 401 or r.status_code == 403:
            log.warning(
                "Task completion returned %d — this endpoint requires user-session auth "
                "(BETTERCO_USER_EMAIL / BETTERCO_USER_PASSWORD). "
                "API-key auth is not sufficient.", r.status_code,
            )
        r.raise_for_status()
        log.info("Completed task %s on process %s", task_id, pid)
        return r.json() if r.text else {}

    def _get_user_token(self) -> str:
        """Get a token valid for User API endpoints (/api/).

        Uses POST /auth/sign-in (form-encoded) with BETTERCO_USER_EMAIL/PASSWORD.
        Falls back to the API-key token.
        """
        if getattr(self, "_user_token", None) and time.time() < getattr(self, "_user_token_expiry", 0):
            return self._user_token
        email = self.user_email
        password = self.user_password
        if email and password:
            r = requests.post(
                f"{self.base_url}/auth/sign-in",
                data={"email": email, "password": password},
                verify=self.session.verify,
            )
            if r.ok:
                data = r.json()
                self._user_token = data["token"]
                # Token valid ~3.3h, refresh after 3h
                self._user_token_expiry = time.time() + 10800
                log.info("Authenticated to User API via email/password")
                return self._user_token
            log.warning("User API login failed (%d), falling back to API-key token", r.status_code)
        return self.token

    def _user_headers(self) -> dict:
        """Headers for User API calls (/api/ scope — auth + workspaceId)."""
        return {
            "Authorization": f"Bearer {self._get_user_token()}",
            "Content-Type": "application/json",
            "workspaceId": self.workspace_id,
        }

    def list_companies(self) -> list:
        """List the organizations in the workspace (advisor + client companies) via the User API.
        GET /api/companies?workspaceId&page&step&sortField&sortOrder. The accountant/advisor org's
        id is the value BETTERCO_ORG_ID must hold."""
        r = requests.get(
            f"{self.base_url}/api/companies",
            params={"workspaceId": self.workspace_id, "page": 0, "step": 200,
                    "sortField": "name", "sortOrder": "ASC"},
            headers=self._user_headers(), verify=self.session.verify,
        )
        r.raise_for_status()
        j = r.json()
        return j.get("companies") or j.get("content") or (j if isinstance(j, list) else [])

    def verify_env(self) -> dict:
        """Validate an .env end-to-end before using it. Checks: (1) REST auth (key+secret),
        (2) User-API auth (email+password), and (3) that BETTERCO_ORG_ID is a REAL organization in
        the workspace — not just any valid org.

        Why (3) matters: a wrong self.org_id is SILENT. REST is org-scoped by URL, so calls still
        succeed and may even list customers, but everything you create lands under the wrong
        advisor org and is invisible in the UI / User-API (which are scoped to the true org).
        Always confirm self.org_id is in list_companies() before importing. Returns a report dict."""
        rep = {"restAuth": False, "userAuth": False, "orgIdValid": False,
               "orgId": self.org_id, "orgName": None, "workspace": None, "companies": []}
        try:
            self._ensure_auth(); rep["restAuth"] = True
        except Exception as e:
            rep["restAuthError"] = str(e)[:140]
        try:
            tok = self._get_user_token(); rep["userAuth"] = bool(tok and tok != self.token)
        except Exception as e:
            rep["userAuthError"] = str(e)[:140]
        try:
            ws = [w for w in (self.list_workspaces() or []) if w.get("id") == self.workspace_id]
            rep["workspace"] = ws[0].get("name") if ws else None
        except Exception:
            pass
        try:
            comps = self.list_companies()
            rep["companies"] = [{"id": o.get("id"), "name": o.get("name") or o.get("legalName")} for o in comps]
            match = next((o for o in comps if o.get("id") == self.org_id), None)
            rep["orgIdValid"] = bool(match)
            rep["orgName"] = (match.get("name") or match.get("legalName")) if match else None
        except Exception as e:
            rep["orgError"] = str(e)[:140]
        rep["ok"] = rep["restAuth"] and rep["userAuth"] and rep["orgIdValid"]
        return rep

    # ── Onboarding step submit (the flow-runner "save" call) ────────

    def submit_step(self, business_relation_id: str, process_id: str,
                    step_id: str, values: dict,
                    workspace_id: str = None) -> dict:
        """Persist a flow page's field values — the same PATCH the runner sends
        on save. PATCH /api/client/onboarding?processId&businessRelationId&
        workspaceId&stepId (POST → 405).

        `values` is keyed by fieldName; the data containers are (verified on
        sbx 2026-06-09):
          • additionalProcessData.<topic>.<field>  → reads back at
            get_process().data.additionalData.<topic>.<field> ("Process" dropped)
          • contacts.contacts[i].additionalData.<field>  (form-array; only
            persists if step_id OWNS that form-array AND the contact carries the
            FA's applicableRoles F1 relation — else withRoles filters it out)
            → reads back at get_process().data.contacts[i].additionalData
          • additionalActorData.<field>  → reads back at
            get_full_data().additionalActorData.<field>
          • entityLegalInfo / contactData → actor master (full-data)
        `step_id` MUST be a valid taskSpec of the process (e.g.
        "P83040_kaufvertragStep"), not the task instance id. A taskStatuses
        entry for step_id is auto-added if absent. Returns the JSON ack
        {processId, businessRelationId, isNewKyc, isProcessClosed}."""
        body = dict(values)
        body.setdefault("taskStatuses", [{"taskId": step_id, "visible": True,
                                          "relationSide": "BIDIRECTIONAL"}])
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        r = requests.patch(
            f"{self.base_url}/api/client/onboarding",
            headers=headers,
            params={"processId": process_id,
                    "businessRelationId": business_relation_id,
                    "workspaceId": workspace_id or self.workspace_id,
                    "stepId": step_id},
            json=body,
            verify=self.session.verify,
        )
        if r.status_code >= 400:
            log.error("submit_step failed (%d): %s", r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json() if r.text else {}

    # ── Safe bulk full-data update (no step side effects) ───────────

    # Fields that must NEVER appear in a bulk full-data PATCH — they corrupt the
    # contact graph, clear records, or are system-managed/read-only/approval-gated.
    # Source: bulk-update-fulldata-guide.md "Fields to never send".
    _FULLDATA_BLOCKED = frozenset({
        # contact graph + relation containers (duplicate / orphan links, clear records)
        "contacts", "legalReps", "ubos", "investorContacts", "bankAccounts",
        "idSignatories",
        # system-managed / read-only / computed
        "processState", "visibleFlows", "createdViaRestApi", "currentProcessName",
        "actorRiskSnapshot",
        # PROCESS-level riskSummary is approval-gated — only via the dedicated risk
        # flow. (ACTOR.riskSummary is separate and IS writable via
        # update_customer / set_actor_risk_summary — REST, not approval-gated.)
        "riskSummary",
        # silently removes compliance-doc requirements — explicit intent only
        "docsToSkip",
    })

    def update_full_data(self, business_relation_id: str, process_id: str,
                         patch: dict, *, action: str = "SKIP",
                         allow: tuple = (), workspace_id: str = None) -> dict:
        """Safe bulk update of actor/process data via PATCH /api/client/onboarding
        (the "write all client data" endpoint). Applies ONLY the fields present in
        `patch` — each section merges independently, it is NOT a full replace.

        This wrapper hard-codes the zero-step-side-effect contract:
          * NO taskId, NO stepId, NO taskStatuses → step pre/post processors never run
          * action=SKIP (belt-and-suspenders processor suppression)
          * refuses the never-send fields in `_FULLDATA_BLOCKED` (contacts, relation
            containers, system-managed, riskSummary, docsToSkip). Pass a field name
            in `allow=(...)` ONLY if you are explicitly managing that relation/field.

        ⚠ ACTOR IS SHARED: actor-side fields (entityLegalInfo, addressInfo,
          contactData, additionalActorData, taxIDs, ...) live on the actor, not the
          process — updating via ANY one processId changes the actor for EVERY
          process that shares it (last write wins). Dedupe by actor across a batch.
        ⚠ additionalActorData / additionalProcessData DEEP-MERGE: send only the keys
          you want to change; a `null` value NULLS the key; omitting the field is the
          only true no-op; never reconstruct the full map from a GET (stale overwrite).
          Don't write back read-time-injected keys (privacyLink, registerDoc, etc.).
        ⚠ riskSummary here is the PROCESS-level one and is approval-gated (validator
          throws before writing); use the dedicated risk process, not this. The
          ACTOR-level actor.riskSummary is a DIFFERENT store and IS writable (not
          gated) via update_customer / set_actor_risk_summary (REST PATCH).
        ⚠ A CLOSED process throws IllegalStateException — pre-check stateType via
          list_customer_processes().
        ⚠ No server dry-run. Verify with GET → PATCH → GET-diff on ONE record before
          scripting hundreds.

        Returns the JSON ack {processId, businessRelationId, isNewKyc, isProcessClosed}."""
        allow_set = set(allow)
        blocked = sorted(k for k in patch if k in self._FULLDATA_BLOCKED and k not in allow_set)
        if blocked:
            raise ValueError(
                f"Refusing to send blocked full-data field(s): {blocked}. These corrupt the "
                f"contact graph or are system-managed/approval-gated. Pass allow={blocked} "
                f"ONLY if you are explicitly managing them.")
        if "taskStatuses" in patch:
            raise ValueError(
                "Do not send taskStatuses in a bulk update — it can move step state. "
                "Omit it (no taskId → no processor runs).")
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        r = requests.patch(
            f"{self.base_url}/api/client/onboarding",
            headers=headers,
            params={"processId": process_id,
                    "businessRelationId": business_relation_id,
                    "workspaceId": workspace_id or self.workspace_id,
                    "action": action},
            json=dict(patch),
            verify=self.session.verify,
        )
        if r.status_code >= 400:
            log.error("update_full_data failed (%d): %s", r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json() if r.text else {}

    def list_customer_processes(self, business_relation_id: str,
                                include_tasks_info: bool = False,
                                workspace_id: str = None) -> list:
        """List all processes for a customer with their state — the discovery call
        for safe bulk update AND process close.
        GET /api/tasks/customer/{brId}?includeTasksInfo=false  (User API auth).

        Returns the `processRows` list; each row carries: processId, processNameCode
        (e.g. F1400_RiskEvaluation), stateType (OPEN/COMPLETE/LOCKED/CLOSED/...),
        businessRelationId, caseId, updatedAt. Filter `stateType != 'CLOSED'` to pick
        a PATCHable processId. For riskSummary writes, pick the row whose
        processNameCode is a risk flow (F700/F1400/F3200_RiskEvaluation,
        F1600_RiskAMLScreening). include_tasks_info=True loads per-task detail
        (slower) — needed to confirm all tasks complete before close_process()."""
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        r = requests.get(
            f"{self.base_url}/api/tasks/customer/{business_relation_id}",
            headers=headers,
            params={"includeTasksInfo": str(include_tasks_info).lower()},
            verify=self.session.verify,
        )
        r.raise_for_status()
        j = r.json() if r.text else {}
        return j.get("processRows", []) if isinstance(j, dict) else (j or [])

    # ── Process close (IRREVERSIBLE) ────────────────────────────────

    def close_process(self, process_id: str, workspace_id: str = None) -> int:
        """Close a process — IRREVERSIBLE (no reopen endpoint).
        PATCH /api/questionnaire/{processId} (no body, no params) → 204.

        Effects: stateType→CLOSED, all non-HIDDEN tasks set CLOSED+locked, a process
        report is generated, and a historical snapshot of the actor + linked contacts
        is frozen onto the process (later actor edits are invisible to this process).
        After close, update_full_data() on this processId throws.

        PRECONDITION: every non-HIDDEN task must already be complete, else 403
        ForbiddenRequestException — check via list_customer_processes(brId,
        include_tasks_info=True) first. To close with open steps, use
        force_close_process() (auto-submits them). Requires write /
        cross_workspace_write on the BetterCoProcess."""
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        r = requests.patch(
            f"{self.base_url}/api/questionnaire/{process_id}",
            headers=headers, verify=self.session.verify,
        )
        if r.status_code >= 400:
            log.error("close_process failed (%d): %s", r.status_code, r.text[:300])
        r.raise_for_status()
        return r.status_code

    def force_close_process(self, customer_id: str, case_id: str, process_id: str,
                            org_id: str = None) -> int:
        """Force-close a process even with OPEN steps (REST API key+secret auth).
        PATCH /restapi/v1/workspaces/{ws}/organizations/{org}/customers/{cid}/
              cases/{caseId}/processes/{pid}?force=true

        `force=true` calls autoSubmitOpenSteps() — it AUTO-SUBMITS every open task
        WITHOUT user input, then runs the normal close. Bypasses the completion
        requirement that the plain close_process() (/api/questionnaire) enforces.
        IRREVERSIBLE; takes the same frozen historical snapshot. Use with caution.

        Endpoint (per dev apidoc, operationId closeProcessById):
            POST /restapi/v1/workspaces/{ws}/organizations/{org}/customers/{cid}/
                 cases/{caseId}/processes/{pid}/close?force=true
        `force=true` = "Force closing ignoring open tasks". (NOT a PATCH on the
        process resource — that returns 405.)"""
        self._ensure_auth()
        oid = org_id or self.org_id
        url = self._url(f"/customers/{customer_id}/cases/{case_id}/processes/{process_id}/close",
                        org_id=oid)
        r = self.session.post(url, params={"force": "true"}, verify=self.session.verify)
        if r.status_code >= 400:
            log.error("force_close_process failed (%d): %s", r.status_code, r.text[:300])
        r.raise_for_status()
        return r.status_code

    # ── Full Data (Client API path, User API auth) ──────────────────

    def get_full_data(self, business_relation_id: str,
                      role_types: list = None,
                      sorting_strategy: str = "SHARES",
                      limit: int = 50,
                      workspace_id: str = None) -> dict:
        """Get complete customer data including contacts, roles, and risk.

        Uses GET /api/client/onboarding/full-data (Client API path but
        works with User API auth token).

        workspace_id: target workspace (default: self.workspace_id from .env).
        Returns master entity data + all contacts with roles + actorRiskSnapshot.
        """
        if role_types is None:
            role_types = ["LEGAL_REP", "SHAREHOLDER", "UBO", "ACTING_PERSON", "MAIN_CONTACT"]
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        r = requests.get(
            f"{self.base_url}/api/client/onboarding/full-data",
            headers=headers,
            params={
                "businessRelationId": business_relation_id,
                "sortingStrategy": sorting_strategy,
                "roleTypes": role_types,
                "limit": limit,
            },
            verify=self.session.verify,
        )
        r.raise_for_status()
        return r.json()

    # ── AML Screening (PEP / sanction / SOE — User API) ─────────────
    #
    # Endpoints reverse-engineered from the editor SPA request-registry:
    #   scan      POST /api/customers/{brId}/contacts/{cid}/screening/scan      (no body)
    #   monitor   POST /api/customers/{brId}/contacts/{cid}/screening/monitor?enable=
    #   c-details POST /api/customers/{brId}/contacts/{cid}/screening/details   (body)
    #   e-details POST /api/customers/{brId}/screening/details                  (body)
    #   matches   GET  /api/customers/{brId}/search-results
    # All use User API auth (Bearer user token + workspaceId header). Results
    # land in screeningProfile (matchStatus, riskLevel, totalMatches, hitsPerCategory).

    def _screen_post(self, path: str, workspace_id: str = None,
                     params: dict = None, body: dict = None) -> dict:
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        r = requests.post(f"{self.base_url}{path}", headers=headers, params=params,
                          json=body, verify=self.session.verify)
        if r.status_code >= 400:
            log.error("screening POST %s failed (%d): %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
        return r.json() if r.text else {}

    def _screen_patch(self, path: str, body: dict, workspace_id: str = None,
                      params: dict = None) -> dict:
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        r = requests.patch(f"{self.base_url}{path}", headers=headers, params=params,
                           json=body, verify=self.session.verify)
        if r.status_code >= 400:
            log.error("screening PATCH %s failed (%d): %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
        return r.json() if r.text else {}

    def scan_contact(self, business_relation_id: str, contact_id: str,
                     workspace_id: str = None) -> dict:
        """Trigger a PEP/sanction/SOE screening scan of one contact.
        POST /api/customers/{brId}/contacts/{cid}/screening/scan (no body).

        REQUIRES the contact to have a birthDate (and ideally nationality):
        the scan fetches provider candidates AND commits a screeningProfile;
        if the contact is missing a birthDate the commit fails with
        400 "Input data is corrupted" and screeningProfile stays empty —
        i.e. the contact reads as "not screened" even though raw candidates
        were fetched (verified on editor 2026-06-13 with Olaf Scholz).
        After a successful scan, read matches via get_screening_matches()."""
        return self._screen_post(
            f"/api/customers/{business_relation_id}/contacts/{contact_id}/screening/scan",
            workspace_id=workspace_id)

    def scan_entity(self, business_relation_id: str, workspace_id: str = None) -> dict:
        """Scan the ENTITY itself. Uses the same scan route with the entity's
        actorId as the contact id (verified). Read results via
        get_screening_matches(brId) (no contact_id)."""
        actor_id = self.get_entity_actor_id(business_relation_id, workspace_id)
        return self.scan_contact(business_relation_id, actor_id, workspace_id=workspace_id)

    def run_actor_detailed_screening(self, business_relation_id: str,
                                     body: dict = None, workspace_id: str = None) -> dict:
        """Run a detailed screening of the ENTITY/actor itself.
        POST /api/customers/{brId}/screening/details."""
        return self._screen_post(
            f"/api/customers/{business_relation_id}/screening/details",
            workspace_id=workspace_id, body=body or {})

    def run_contact_detailed_screening(self, business_relation_id: str, contact_id: str,
                                       body: dict = None, workspace_id: str = None) -> dict:
        """Run a detailed (refined-identity) screening of one contact.
        POST /api/customers/{brId}/contacts/{cid}/screening/details."""
        return self._screen_post(
            f"/api/customers/{business_relation_id}/contacts/{contact_id}/screening/details",
            workspace_id=workspace_id, body=body or {})

    def update_monitor_status(self, business_relation_id: str, contact_id: str,
                              enable: bool, workspace_id: str = None) -> dict:
        """Toggle perpetual re-screening monitoring for a contact.
        POST /api/customers/{brId}/contacts/{cid}/screening/monitor?enable=."""
        return self._screen_post(
            f"/api/customers/{business_relation_id}/contacts/{contact_id}/screening/monitor",
            workspace_id=workspace_id, params={"enable": str(bool(enable)).lower()})

    # ── Reviewing candidates & marking match / no-match ─────────────
    #
    # matchStatus ∈ {MATCH, NO_MATCH, FALSE_POSITIVE, POTENTIAL_MATCH}
    # riskLevel   ∈ {NONE, LOW, MEDIUM, HIGH, VERY_HIGH}
    #
    # Two ways to record a decision:
    #  A) Per-candidate selection (the dropdown in the UI) — picks ONE candidate
    #     from search-results as the match, or marks "No Match":
    #        mark_contact_match(brId, cid, candidate)  /  mark_no_match(brId, cid)
    #     (entity: mark_entity_match / mark_entity_no_match)
    #  B) Direct tag override on a contact — set matchStatus / riskLevel straight:
    #        set_contact_match_status(brId, cid, "FALSE_POSITIVE")
    #        set_contact_risk_level(brId, cid, "LOW")

    def get_aml_match_details(self, business_relation_id: str, search_id: str,
                              contact_id: str = None, workspace_id: str = None) -> dict:
        """Full AML profile for ONE candidate (sanctions, political functions,
        linked persons/businesses, datasets, DOB, address).
        GET /api/customers/{brId}/details/{searchId}[?contactId=].
        `search_id` is the candidate's `id` from get_screening_matches()."""
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        params = {"contactId": contact_id} if contact_id else {}
        r = requests.get(
            f"{self.base_url}/api/customers/{business_relation_id}/details/{search_id}",
            headers=headers, params=params, verify=self.session.verify)
        r.raise_for_status()
        return r.json() if r.text else {}

    def mark_contact_match(self, business_relation_id: str, contact_id: str,
                           candidate: dict, workspace_id: str = None) -> dict:
        """Mark a contact as MATCHing a specific candidate.
        `candidate` is the full match object from
        get_screening_matches()[...]["searchResults"]["data"][i].
        POST /api/customers/{brId}/contacts/{cid}/screening/details."""
        return self._screen_post(
            f"/api/customers/{business_relation_id}/contacts/{contact_id}/screening/details",
            workspace_id=workspace_id, body=candidate)

    def mark_no_match(self, business_relation_id: str, contact_id: str,
                      workspace_id: str = None) -> dict:
        """Mark a contact as NO MATCH (clears the selected candidate).
        POST /api/customers/{brId}/contacts/{cid}/screening/details with null body."""
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        # UI sends a literal JSON null for "No Match"
        r = requests.post(
            f"{self.base_url}/api/customers/{business_relation_id}/contacts/{contact_id}/screening/details",
            headers=headers, data="null", verify=self.session.verify)
        if r.status_code >= 400:
            log.error("mark_no_match failed (%d): %s", r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json() if r.text else {}

    def mark_entity_match(self, business_relation_id: str, candidate: dict,
                          workspace_id: str = None) -> dict:
        """Mark the ENTITY as MATCHing a candidate.
        POST /api/customers/{brId}/screening/details."""
        return self._screen_post(
            f"/api/customers/{business_relation_id}/screening/details",
            workspace_id=workspace_id, body=candidate)

    def mark_entity_no_match(self, business_relation_id: str,
                             workspace_id: str = None) -> dict:
        """Mark the ENTITY as NO MATCH. POST /api/customers/{brId}/screening/details (null)."""
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        r = requests.post(f"{self.base_url}/api/customers/{business_relation_id}/screening/details",
                          headers=headers, data="null", verify=self.session.verify)
        if r.status_code >= 400:
            log.error("mark_entity_no_match failed (%d): %s", r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json() if r.text else {}

    def get_entity_actor_id(self, business_relation_id: str, workspace_id: str = None) -> str:
        """Resolve a customer's ENTITY actor id (≠ businessRelationId) — needed as
        the `companyId` for screening-tag PATCHes. From the customer's `actorId`."""
        for cust in self.get_workspace_customers(workspace_id or self.workspace_id):
            if (cust.get("businessRelationId") or cust.get("id")) == business_relation_id:
                aid = cust.get("actorId")
                if aid:
                    return aid
        # fallback: the screening result carries the entity actorId
        return self.get_screening_matches(business_relation_id, workspace_id=workspace_id).get("actorId")

    def set_contact_match_status(self, company_actor_id: str, contact_id: str,
                                 match_status: str, workspace_id: str = None) -> dict:
        """Set a contact's match-status tag — the verified way to mark a verdict.
        match_status ∈ MATCH | NO_MATCH | FALSE_POSITIVE | POTENTIAL_MATCH.
        PATCH /api/contacts/{cid}/screening?companyId={company_actor_id}.
        NOTE: `company_actor_id` is the ENTITY's actorId (customer["actorId"] /
        get_entity_actor_id()), NOT the businessRelationId — wrong id → 403."""
        return self._screen_patch(
            f"/api/contacts/{contact_id}/screening", body={"matchStatus": match_status},
            params={"companyId": company_actor_id}, workspace_id=workspace_id)

    def set_contact_risk_level(self, company_actor_id: str, contact_id: str,
                               risk_level: str, workspace_id: str = None) -> dict:
        """Set a contact's risk-level tag.
        risk_level ∈ NONE | LOW | MEDIUM | HIGH | VERY_HIGH.
        PATCH /api/contacts/{cid}/screening?companyId={company_actor_id}
        (company_actor_id = entity actorId, NOT businessRelationId)."""
        return self._screen_patch(
            f"/api/contacts/{contact_id}/screening", body={"riskLevel": risk_level},
            params={"companyId": company_actor_id}, workspace_id=workspace_id)

    _AML_STEP_TASKS = ["P1615_amlScreeningDefinition",
                       "P1616_amlScreeningSearchMatching", "P1620_amlScreening"]

    def save_aml_review(self, business_relation_id: str, process_id: str,
                        entity_match_status: str = None, entity_risk_level: str = None,
                        contact_verdicts: dict = None, step_id: str = "P1620_amlScreening",
                        workspace_id: str = None) -> dict:
        """Commit the AML screening review (entity + contacts) the way the editor does:
        save the flow step P1620_amlScreening via submit_step (PATCH /api/client/onboarding).
        This is the canonical "review & mark" action and the ONLY way to set the
        ENTITY's verdict (the /contacts endpoint 404s for the entity actor).

        entity_match_status/entity_risk_level → the entity's screeningProfile.
        contact_verdicts: {contactId: {"matchStatus": ..., "riskLevel": ...}}.
        Only the keys you pass are sent (server deep-merges by container/contactId);
        contacts you omit are left untouched. matchStatus ∈ MATCH|NO_MATCH|
        FALSE_POSITIVE|POTENTIAL_MATCH; riskLevel ∈ NONE|LOW|MEDIUM|HIGH|VERY_HIGH.
        Always read back with get_full_data() — submit_step 200s even on dropped fields."""
        values = {}
        sp = {}
        if entity_match_status:
            sp["matchStatus"] = entity_match_status
        if entity_risk_level:
            sp["riskLevel"] = entity_risk_level
        if sp:
            values["amlProfile"] = {"screeningProfile": sp}
        if contact_verdicts:
            values["contacts"] = {"contacts": [
                {"contactId": cid, "screeningProfile": v}
                for cid, v in contact_verdicts.items()]}
        values["taskStatuses"] = [
            {"taskId": t, "visible": True, "relationSide": "BIDIRECTIONAL"}
            for t in self._AML_STEP_TASKS]
        return self.submit_step(business_relation_id, process_id, step_id, values,
                                workspace_id=workspace_id)

    # ── Screening review (powers the HTML review app) ───────────────

    def find_screening_process(self, business_relation_id: str, workspace_id: str = None) -> str:
        """Return the customer's OPEN F1600_RiskAMLScreening process id (or None).

        Resolved from brId via GET /api/tasks/customer/{brId} → cases[].processRows[], matching
        processNameCode == 'F1600_RiskAMLScreening'. NOTE: the old approach (zip activeProcesses
        names with activeProcessesIds from get_workspace_customers) is BROKEN — those two lists are
        NOT order-aligned, so it returned the wrong processId (e.g. the F1800 id) and run_screening
        then 404'd PATCHing P1615 onto a process without that step."""
        ws = workspace_id or self.workspace_id
        r = requests.get(
            f"{self.base_url}/api/tasks/customer/{business_relation_id}",
            headers=self._user_headers_for(ws) if ws else self._user_headers(),
            params={"includeTasksInfo": "false", "workspaceId": ws},
            verify=self.session.verify)
        if r.status_code >= 400:
            return None
        rows = [row for ca in (r.json().get("cases") or []) for row in (ca.get("processRows") or [])]
        f1600 = [row for row in rows if row.get("processNameCode") == "F1600_RiskAMLScreening"]
        # prefer the EARLIEST non-CLOSED F1600 (the original import-era process — its id sorts before
        # any today-created shell from an earlier broken run); fall back to any.
        open_f = sorted((row for row in f1600 if row.get("stateType") != "CLOSED"),
                        key=lambda r: r.get("processId") or "")
        pick = open_f[0] if open_f else (f1600[0] if f1600 else None)
        return pick.get("processId") if pick else None

    def list_screening_customers(self, workspace_id: str = None) -> list:
        """Customers in the workspace that have an AML screening process running.
        Returns [{cid, name, actorId, processId}]."""
        out = []
        for cust in self.get_workspace_customers(workspace_id or self.workspace_id):
            names = cust.get("activeProcesses") or []
            ids = cust.get("activeProcessesIds") or []
            pid = next((i for n, i in zip(names, ids) if n == "F1600_RiskAMLScreening"), None)
            if pid:
                out.append({
                    "cid": cust.get("businessRelationId") or cust.get("id"),
                    "name": cust.get("legalName") or cust.get("displayName") or "?",
                    "actorId": cust.get("actorId"),
                    "processId": pid,
                })
        return out

    def ensure_screening_process(self, business_relation_id: str,
                                 workspace_id: str = None) -> str:
        """Return the customer's F1600_RiskAMLScreening process id, creating it
        if absent (a fresh customer only has the F1800 onboarding process)."""
        pid = self.find_screening_process(business_relation_id, workspace_id)
        if pid:
            return pid
        self._ensure_auth()
        cases = (self.session.get(self._url(f"/customers/{business_relation_id}/cases"))
                 .json() or {}).get("results") or []
        if not cases:
            raise RuntimeError(f"customer {business_relation_id} has no case")
        proc = self.create_process(business_relation_id, cases[0]["id"],
                                   "F1600_RiskAMLScreening")
        return proc["id"]

    _DECIDED = {"MATCH", "NO_MATCH", "FALSE_POSITIVE"}

    _RELATION_ROLE = {"9010": "Legal Rep", "9030": "UBO", "9060": "Acting Person",
                      "3010": "Owner", "1520": "Managing Director", "3040": "Shareholder",
                      "3050": "Shareholder"}

    def get_screening_scope(self, business_relation_id: str,
                            roles=("LEGAL_REP", "UBO", "ACTING_PERSON"),
                            workspace_id: str = None) -> dict:
        """Preview WHO would be screened — the entity + in-scope contacts (legal
        rep / UBO / acting person) — WITHOUT triggering any screening. Read-only.

        Returns {businessRelationId, customerName, entityActorId,
                 actors:[{actorId, contactId, name, type, roles[], birthDate}]}."""
        ws = workspace_id
        fd = self.get_full_data(business_relation_id, role_types=list(roles), workspace_id=ws)
        entity_name = (fd.get("entityLegalInfo") or {}).get("legalName") or "ENTITY"
        entity_actor = self.get_entity_actor_id(business_relation_id, ws)
        actors = [{"actorId": entity_actor, "contactId": None, "name": entity_name,
                   "type": "ENTITY", "roles": ["Entity"], "birthDate": None}]
        for ct in self._inscope_contacts((fd.get("contacts") or {}).get("contacts") or [], roles):
            rels = ct.get("relations") or []
            actors.append({
                "actorId": ct.get("contactId"), "contactId": ct.get("contactId"),
                "name": (f"{ct.get('firstName','')} {ct.get('lastName','')}".strip()
                         or ct.get("entityName") or ct.get("contactId")),
                "type": ct.get("contactType", "INDIVIDUAL"),
                "roles": list(dict.fromkeys(
                    self._RELATION_ROLE[r] for r in rels if r in self._RELATION_ROLE)),
                "birthDate": ct.get("birthDate"),
            })
        return {"businessRelationId": business_relation_id, "customerName": entity_name,
                "entityActorId": entity_actor, "actors": actors}

    def get_screening_review(self, business_relation_id: str, process_id: str = None,
                             roles=("LEGAL_REP", "UBO", "ACTING_PERSON"),
                             only_open: bool = False, with_detail: bool = True,
                             workspace_id: str = None) -> dict:
        """Build a review packet for the screening UI: the entity + EVERY in-scope
        contact (legal rep / UBO / acting person), each with its screening status,
        verdict, role labels, and full Acuris candidate detail.

        Per actor `status`: 'flagged' (has candidates, undecided) | 'decided'
        (verdict set) | 'clean' (screened, no candidates) | 'unscreened'.
        only_open=True keeps only the 'flagged' ones.

        Returns {businessRelationId, processId, customerName, entityActorId,
                 summary:{...}, items:[{actorId, contactId(None=entity), name, type,
                 roles[], status, matchStatus, riskLevel, screened, candidates:[...]}]}."""
        ws = workspace_id
        if process_id is None:
            process_id = self.find_screening_process(business_relation_id, ws)
        fd = self.get_full_data(business_relation_id, role_types=list(roles), workspace_id=ws)
        entity_actor = self.get_entity_actor_id(business_relation_id, ws)
        entity_name = (fd.get("entityLegalInfo") or {}).get("legalName") or "ENTITY"

        def build(actor_id, contact_id, name, atype, sp, relations):
            payload = self.get_screening_matches(business_relation_id, contact_id, workspace_id=ws)
            rows = self._match_rows(payload)
            screened = bool(payload.get("id") or payload.get("searchResults"))
            ms = (sp or {}).get("matchStatus")
            role_labels = (list(dict.fromkeys(
                            self._RELATION_ROLE[r] for r in (relations or []) if r in self._RELATION_ROLE))
                           if atype != "ENTITY" else ["Entity"])
            cands = []
            for m in rows:
                a = m.get("attributes", {})
                cand = {"name": a.get("match") or a.get("name"),
                        "score": a.get("score"), "pepTier": a.get("pepTier"),
                        "datasets": a.get("datasets"), "countries": a.get("countries"),
                        "dob": a.get("datesOfBirth"), "profileImage": a.get("profileImage")}
                if with_detail:
                    det = self.get_aml_match_details(
                        business_relation_id, m["id"], contact_id=contact_id, workspace_id=ws)
                    cand.update({
                        "pepEntries": det.get("pepEntries"), "sanEntries": det.get("sanEntries"),
                        "rreEntries": det.get("rreEntries"), "poiEntries": det.get("poiEntries"),
                        "addresses": det.get("addresses"), "aliases": det.get("aliases"),
                        "nationalities": det.get("nationalitiesISOCodes")})
                cands.append(cand)
            if cands:
                status = "decided" if ms in self._DECIDED else "flagged"
            elif screened:
                status = "clean"
            else:
                status = "unscreened"
            return {"actorId": actor_id, "contactId": contact_id, "name": name,
                    "type": atype, "roles": role_labels, "status": status,
                    "screened": screened, "matchStatus": ms,
                    "riskLevel": (sp or {}).get("riskLevel"), "candidates": cands}

        items = [build(entity_actor, None, entity_name, "ENTITY",
                       (fd.get("amlProfile") or {}).get("screeningProfile"), None)]
        for ct in self._inscope_contacts((fd.get("contacts") or {}).get("contacts") or [], roles):
            items.append(build(
                ct.get("contactId"), ct.get("contactId"),
                (f"{ct.get('firstName','')} {ct.get('lastName','')}".strip()
                 or ct.get("entityName") or ct.get("contactId")),
                ct.get("contactType", "INDIVIDUAL"), ct.get("screeningProfile"),
                ct.get("relations")))
        if only_open:
            items = [i for i in items if i["status"] == "flagged"]
        summary = {s: sum(1 for i in items if i["status"] == s)
                   for s in ("flagged", "decided", "clean", "unscreened")}
        return {"businessRelationId": business_relation_id, "processId": process_id,
                "customerName": entity_name, "entityActorId": entity_actor,
                "summary": summary, "items": items}

    # Relation codes that put a contact IN SCOPE for AML screening.
    # (full-data's roleTypes param does NOT filter — must filter on `relations`.)
    _ROLE_RELATION_CODES = {
        "LEGAL_REP": {"9010", "3010"},      # 3010 = Owner (legal rep + UBO)
        "UBO": {"9030", "3010"},
        "ACTING_PERSON": {"9060"},
    }

    @classmethod
    def _inscope_contacts(cls, contacts: list, roles) -> list:
        """Contacts whose `relations` include a code for one of `roles`.
        Excludes pure shareholders (3040/3050) etc. that should not be screened."""
        codes = set()
        for r in roles:
            codes |= cls._ROLE_RELATION_CODES.get(r, set())
        return [ct for ct in contacts
                if codes & set(ct.get("relations") or [])]

    def run_screening(self, business_relation_id: str, process_id: str,
                      roles=("LEGAL_REP", "UBO", "ACTING_PERSON"),
                      workspace_id: str = None) -> dict:
        """Trigger AML screening for the whole customer (entity + in-scope contacts)
        HEADLESSLY — the way the editor's AML 'definition' step does.

        PATCH /api/client/onboarding?...&stepId=P1615_amlScreeningDefinition
        &roleTypes=PROCESS with the full actor payload (echoed from full-data) and
        screeningProfile.isRescreeningEnabled=true on the entity + every contact.
        Runs the provider search for every actor that has screenable data
        (name + birthDate); a contact with no birthDate just won't get a result.
        This is the reliable trigger — the bare POST .../screening/scan endpoint
        returns 400 "Input data is corrupted" for most actors.
        Returns the ack; read results via get_screening_matches().

        IMPORTANT: `process_id` MUST be an **F1600_RiskAMLScreening** process
        (its steps are P1615/P1616/P1620), NOT the F1800 onboarding process —
        a wrong process 404s "Object not found". A fresh customer has only the
        F1800 process; create the screening process first:
            proc = self.create_process(cid, case_id, "F1600_RiskAMLScreening")
        then pass proc["id"] here."""
        ws = workspace_id
        fd = self.get_full_data(business_relation_id, role_types=list(roles), workspace_id=ws)
        # full-data's roleTypes param does NOT filter — restrict to actual in-scope
        # relations (legal rep / UBO / acting person); shareholders are excluded.
        contacts = self._inscope_contacts((fd.get("contacts") or {}).get("contacts") or [], roles)
        for ct in contacts:
            ct.setdefault("screeningProfile", {})["isRescreeningEnabled"] = True
        body = {
            "entityLegalInfo": fd.get("entityLegalInfo"),
            "clientType": fd.get("clientType"),
            "amlProfile": {"screeningProfile": {"isRescreeningEnabled": True}},
            "contacts": {"contacts": contacts},
            "taskStatuses": [{"taskId": t, "visible": True, "relationSide": "BIDIRECTIONAL"}
                             for t in self._AML_STEP_TASKS],
        }
        headers = self._user_headers_for(ws) if ws else self._user_headers()
        r = requests.patch(
            f"{self.base_url}/api/client/onboarding", headers=headers,
            params={"businessRelationId": business_relation_id, "processId": process_id,
                    "stepId": "P1615_amlScreeningDefinition", "roleTypes": "PROCESS",
                    "workspaceId": ws or self.workspace_id},
            json=body, verify=self.session.verify, timeout=120)   # bound the provider scan (heavy entities hang otherwise)
        if r.status_code >= 400:
            log.error("run_screening failed (%d): %s", r.status_code, r.text[:300])
        r.raise_for_status()
        log.info("run_screening triggered for %s (%d contacts)",
                 business_relation_id, len(contacts))
        return r.json() if r.text else {}

    @staticmethod
    def _match_rows(matches_payload: dict) -> list:
        return ((matches_payload or {}).get("searchResults") or {}).get("data") or []

    def auto_screen_customer(self, business_relation_id: str, process_id: str,
                             roles=("LEGAL_REP", "UBO", "ACTING_PERSON"),
                             clean_match_status: str = "NO_MATCH",
                             clean_risk_level: str = "LOW",
                             screen: bool = True, include_entity: bool = True,
                             screen_wait: float = 6.0,
                             dry_run: bool = True, workspace_id: str = None) -> dict:
        """Fully headless AML screening for a customer — run, clear the clean, flag the rest.

          0. (if screen=True) run_screening() for the whole customer via the flow,
          then for the entity + every in-scope contact (by `roles`):
          1. read its provider matches,
          2. ZERO matches on an actor that WAS screened → auto-set
             matchStatus=clean_match_status (NO_MATCH) + riskLevel=clean_risk_level
             (LOW) — no human needed,
          3. ANY matches → leave it and FLAG it for a human decision,
          4. not screened / no birthDate → skip + report why.
        Clean verdicts are committed in ONE save_aml_review() call. No UI.

        screen=True (default): trigger the screening first via run_screening()
        (PATCH P1615_amlScreeningDefinition) and wait `screen_wait` s for results.
        screen=False: skip the trigger and just resolve already-run results.

        dry_run=True (default): classify and RETURN the plan WITHOUT writing.
        Set dry_run=False to commit the auto-clears.

        Returns {dry_run, committed, auto_cleared[], needs_review[], skipped[]}.
        Each needs_review item lists top matches (name/score/pepTier/datasets) so
        a human can decide via save_aml_review / set_contact_match_status."""
        ws = workspace_id
        if screen:
            self.run_screening(business_relation_id, process_id, roles=roles, workspace_id=ws)
            if screen_wait:
                time.sleep(screen_wait)
        fd = self.get_full_data(business_relation_id, role_types=list(roles), workspace_id=ws)
        # only screen/resolve in-scope contacts (legal rep / UBO / acting person);
        # roleTypes param doesn't filter, so filter on relation codes.
        contacts = self._inscope_contacts((fd.get("contacts") or {}).get("contacts") or [], roles)
        # risk-list rows carry birthDate per contact (used to gate scanning)
        dob_by_id = {}
        try:
            for rec in self._risk_records(business_relation_id, ws):
                dob_by_id[rec.get("id")] = rec.get("birthDate")
        except Exception:
            pass

        report = {"dry_run": dry_run, "committed": False,
                  "auto_cleared": [], "needs_review": [], "skipped": []}
        contact_verdicts = {}
        entity_verdict = {}

        def classify(scan_id, matches_contact_id, atype):
            """Read matches for `matches_contact_id` (None = entity), classify.
            Returns ('clear'|'review'|'skip', detail)."""
            try:
                payload = self.get_screening_matches(
                    business_relation_id, matches_contact_id, workspace_id=ws)
            except requests.HTTPError:
                return "skip", "no screening result available"
            # an empty payload (no result object) = not screened — don't auto-clear
            if not (payload.get("searchResults") or payload.get("id")):
                reason = ("not screened — likely no birthDate"
                          if atype == "INDIVIDUAL" and not dob_by_id.get(scan_id)
                          else "not screened (run with screen=True)")
                return "skip", reason
            rows = self._match_rows(payload)
            if not rows:
                return "clear", None
            top = [{"name": r["attributes"].get("match") or r["attributes"].get("name"),
                    "score": r["attributes"].get("score"),
                    "pepTier": r["attributes"].get("pepTier"),
                    "datasets": r["attributes"].get("datasets")} for r in rows[:5]]
            return "review", top

        # entity — scan via its actorId, read matches with no contactId
        if include_entity:
            entity_actor = self.get_entity_actor_id(business_relation_id, ws)
            verdict, detail = classify(entity_actor, None, "ENTITY")
            row = {"actor_id": entity_actor, "contact_id": None, "name": "ENTITY", "type": "ENTITY"}
            if verdict == "clear":
                entity_verdict = {"matchStatus": clean_match_status, "riskLevel": clean_risk_level}
                report["auto_cleared"].append({**row, **entity_verdict})
            elif verdict == "review":
                report["needs_review"].append({**row, "match_count": len(detail), "top_matches": detail})
            else:
                report["skipped"].append({**row, "reason": detail})

        # contacts
        for ct in contacts:
            cid = ct.get("contactId")
            name = (f"{ct.get('firstName','')} {ct.get('lastName','')}".strip()
                    or ct.get("entityName") or cid)
            atype = ct.get("contactType", "INDIVIDUAL")
            verdict, detail = classify(cid, cid, atype)
            row = {"actor_id": cid, "contact_id": cid, "name": name, "type": atype}
            if verdict == "clear":
                contact_verdicts[cid] = {"matchStatus": clean_match_status,
                                         "riskLevel": clean_risk_level}
                report["auto_cleared"].append({**row, **contact_verdicts[cid]})
            elif verdict == "review":
                report["needs_review"].append({**row, "match_count": len(detail),
                                                "top_matches": detail})
            else:
                report["skipped"].append({**row, "reason": detail})

        if not dry_run and (contact_verdicts or entity_verdict):
            self.save_aml_review(
                business_relation_id, process_id,
                entity_match_status=entity_verdict.get("matchStatus"),
                entity_risk_level=entity_verdict.get("riskLevel"),
                contact_verdicts=contact_verdicts or None, workspace_id=ws)
            report["committed"] = True
        log.info("auto_screen %s: cleared=%d review=%d skipped=%d (dry_run=%s)",
                 business_relation_id, len(report["auto_cleared"]),
                 len(report["needs_review"]), len(report["skipped"]), dry_run)
        return report

    def _risk_records(self, business_relation_id: str, workspace_id: str = None) -> list:
        """All screening/risk rows for a customer (paged GET /api/contacts/risk).
        Each row: id, contactType, firstName/lastName/entityName, birthDate,
        matchStatus, riskLevel, counterOfScreenings, isMonitoringEnabled."""
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        out, page = [], 0
        while True:
            r = requests.get(f"{self.base_url}/api/contacts/risk", headers=headers,
                             params={"businessRelationId": business_relation_id,
                                     "page": page, "step": 50}, verify=self.session.verify)
            r.raise_for_status()
            batch = (r.json() or {}).get("contacts") or []
            out += batch
            if len(batch) < 50:
                break
            page += 1
        return out

    def get_screening_matches(self, business_relation_id: str, contact_id: str = None,
                              workspace_id: str = None) -> dict:
        """Get screening matches (search results) for the entity or a contact.
        GET /api/customers/{brId}/search-results[?contactId=].

        No contact_id → the entity/actor's own matches.
        With contact_id → that contact's matches.
        Returns the raw payload; each match is searchResults.data[].attributes
        with {match, name, score, datasets, countries}. Datasets include
        PEP-CURRENT, PEP-LINKED, SAN-CURRENT, SAN-FORMER, SOE-CURRENT, POI, RRE."""
        headers = self._user_headers_for(workspace_id) if workspace_id else self._user_headers()
        params = {"contactId": contact_id} if contact_id else {}
        r = requests.get(f"{self.base_url}/api/customers/{business_relation_id}/search-results",
                         headers=headers, params=params, verify=self.session.verify)
        r.raise_for_status()
        return r.json() if r.text else {}

    def screen_customer(self, business_relation_id: str,
                        roles=("LEGAL_REP", "UBO", "ACTING_PERSON"),
                        include_entity: bool = True, detailed: bool = False,
                        workspace_id: str = None) -> dict:
        """Screen the entity and every contact holding one of `roles`.

        Drives off full-data's role-filtered contacts.contacts list (the API
        filters by role server-side). Returns
        {entity, contacts: {contactId: result}, screened: N}.
        NOTE: each call is a billable provider hit — N contacts = N+1 screenings.
        """
        fd = self.get_full_data(business_relation_id, role_types=list(roles),
                                workspace_id=workspace_id)
        contacts = (fd.get("contacts") or {}).get("contacts") or []
        out = {"entity": None, "contacts": {}, "screened": 0}
        if include_entity:
            out["entity"] = self.run_actor_detailed_screening(
                business_relation_id, workspace_id=workspace_id)
            out["screened"] += 1
        for ct in contacts:
            cid = ct.get("contactId")
            if not cid:
                continue
            if detailed:
                out["contacts"][cid] = self.run_contact_detailed_screening(
                    business_relation_id, cid, workspace_id=workspace_id)
            else:
                out["contacts"][cid] = self.scan_contact(
                    business_relation_id, cid, workspace_id=workspace_id)
            out["screened"] += 1
        log.info("Screened %d actors for %s", out["screened"], business_relation_id)
        return out

    # ── Multi-Workspace Dashboard (User API) ────────────────────────

    def _user_headers_for(self, workspace_id: str) -> dict:
        """Headers for User API calls targeting a specific workspace."""
        return {
            "Authorization": f"Bearer {self._get_user_token()}",
            "Content-Type": "application/json",
            "workspaceId": workspace_id,
        }

    def get_workspace_customers(self, workspace_id: str, step: int = 500) -> list:
        """List all customers in a workspace via User API (auto-paginated).

        Returns richer data than REST API: capital, proxyPolicy,
        activeProcesses, responsiblePartner/Manager, etc.
        """
        all_customers = []
        page = 0
        while True:
            r = requests.get(
                f"{self.base_url}/api/customers",
                headers=self._user_headers_for(workspace_id),
                params={
                    "workspaceId": workspace_id,
                    "step": step,
                    "page": page,
                    "sortField": "updatedAt",
                    "sortOrder": "DESC",
                },
                verify=self.session.verify,
            )
            r.raise_for_status()
            data = r.json()
            customers = data.get("customers", data) if isinstance(data, dict) else data
            if not customers:
                break
            all_customers.extend(customers)
            if len(customers) < step:
                break
            page += 1
        return all_customers

    def get_workspace_orgs(self, workspace_id: str) -> list:
        """Get all organizations for a workspace."""
        r = requests.get(
            f"{self.base_url}/api/companies",
            headers=self._user_headers_for(workspace_id),
            params={
                "workspaceId": workspace_id,
                "step": 50,
                "page": 0,
                "sortOrder": "ASC",
                "sortField": "name",
            },
            verify=self.session.verify,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data.get("content", data.get("companies", []))
        return data

    def get_dashboard_data(self) -> list:
        """Pull summary data across all accessible workspaces.

        Returns list of dicts, one per workspace:
        {workspace_id, workspace_name, role, org_id, org_name,
         customer_count, customers: [{businessRelationId, legalName,
         categoryType, activeProcessesCount, customerStageType, ...}]}
        """
        workspaces = self.list_workspaces()
        dashboard = []
        for ws in workspaces:
            ws_id = ws.get("id")
            ws_name = ws.get("name", "?")
            role = ws.get("userWorkspaceRole", "?")
            log.info("Scanning workspace: %s (%s)", ws_name, ws_id)
            try:
                orgs = self.get_workspace_orgs(ws_id)
                customers = self.get_workspace_customers(ws_id)
                dashboard.append({
                    "workspace_id": ws_id,
                    "workspace_name": ws_name,
                    "role": role,
                    "status": ws.get("status"),
                    "orgs": [{"id": o.get("id"), "name": o.get("legalName", o.get("name"))} for o in orgs],
                    "customer_count": len(customers),
                    "customers": customers,
                })
            except Exception as e:
                log.warning("Failed to scan workspace %s: %s", ws_name, e)
                dashboard.append({
                    "workspace_id": ws_id,
                    "workspace_name": ws_name,
                    "role": role,
                    "status": ws.get("status"),
                    "error": str(e),
                    "customer_count": 0,
                    "customers": [],
                })
        return dashboard

    @staticmethod
    def _tokenize(name: str) -> str:
        """Deterministic 8-char token from a name. Same name -> same token."""
        if not name:
            return "UNKNOWN"
        return hashlib.sha256(name.encode("utf-8")).hexdigest()[:8].upper()

    @staticmethod
    def _initials(name: str) -> str:
        """Extract initials from a full name. 'Christoph Buck' -> 'CB'."""
        if not name:
            return ""
        parts = name.split()
        return "".join(p[0].upper() for p in parts if p)

    @staticmethod
    def _classify_process(process_name: str) -> str:
        """Map a process name to a category."""
        if not process_name:
            return "other"
        pn = process_name.lower()
        if "onboarding" in pn or "f1800" in pn or "f1400" in pn:
            return "kyc"
        if "identification" in pn or "f4100" in pn:
            return "identification"
        if "risk" in pn:
            return "risk"
        if "lead" in pn:
            return "lead"
        if "monitor" in pn or "review" in pn:
            return "monitoring"
        return "other"

    def get_workspace_members(self, workspace_id: str) -> dict:
        """Get workspace members as {id: {name, role, initials}}.

        Uses GET /api/members?workspaceId=...
        """
        r = requests.get(
            f"{self.base_url}/api/members",
            headers=self._user_headers_for(workspace_id),
            params={"workspaceId": workspace_id},
            verify=self.session.verify,
        )
        r.raise_for_status()
        members = r.json() if isinstance(r.json(), list) else []
        return {
            m["id"]: {
                "name": m.get("name", ""),
                "initials": self._initials(m.get("name", "")),
                "role": m.get("role", ""),
            }
            for m in members
        }

    def get_dashboard_detail(self) -> list:
        """Flat detail rows across all workspaces for pivoting.

        Returns list of dicts, one per (customer × active process):
        {workspace_id, workspace_name, org_id, org_name, customer_id,
         customer_token, customer_type, category_type, process_id,
         process_name, process_category, created_at, updated_at, ...}

        Customers with no active processes get one row with empty process fields.
        Customer names are tokenized (SHA-256 prefix) for privacy.
        """
        workspaces = self.list_workspaces()
        rows = []
        for ws in workspaces:
            ws_id = ws.get("id")
            ws_name = ws.get("name", "?")
            log.info("Detail scan: %s", ws_name)
            try:
                orgs = self.get_workspace_orgs(ws_id)
                org_lookup = {o.get("id"): o.get("legalName", o.get("name", ""))
                              for o in orgs}
                members = self.get_workspace_members(ws_id)
                customers = self.get_workspace_customers(ws_id)
            except Exception as e:
                log.warning("Failed workspace %s: %s", ws_name, e)
                continue

            log.info("  %d orgs, %d customers", len(orgs), len(customers))
            for o in orgs:
                oname = o.get("legalName", o.get("name", "?"))
                ocust = sum(1 for c in customers if c.get("advisorActorId") == o.get("id"))
                log.info("    Org: %s (%d customers)", oname, ocust)

            for cust in customers:
                cid = cust.get("businessRelationId", "")

                # Resolve org from advisorActorId
                cust_org_id = cust.get("advisorActorId", "")
                cust_org_name = org_lookup.get(cust_org_id, "")

                # Resolve partner/manager to initials
                partner = cust.get("responsiblePartner") or {}
                manager = cust.get("responsibleManager") or {}
                partner_initials = self._initials(partner.get("name", ""))
                manager_initials = self._initials(manager.get("name", ""))

                # Resolve caseTeamMembers to initials
                team_ids = cust.get("caseTeamMembers") or []
                team_initials = ",".join(
                    members.get(tid, {}).get("initials", "?") for tid in team_ids
                )

                base_row = {
                    "workspace_id": ws_id,
                    "workspace_name": ws_name,
                    "org_id": cust_org_id,
                    "org_name": cust_org_name,
                    "customer_id": cid,
                    "customer_token": self._tokenize(cust.get("legalName", "")),
                    "customer_type": cust.get("type", ""),
                    "category_type": cust.get("categoryType", ""),
                    "partner": partner_initials,
                    "manager": manager_initials,
                    "team": team_initials,
                    "register_city": cust.get("registerCity", ""),
                    "register_country": cust.get("registerCountry", ""),
                    "source": cust.get("source", ""),
                    "created_at": cust.get("createdAt", ""),
                    "updated_at": cust.get("updatedAt", ""),
                    "active_process_count": cust.get("activeProcessesCount", 0),
                }

                proc_names = cust.get("activeProcesses", []) or []
                proc_ids = cust.get("activeProcessesIds", []) or []

                if not proc_names:
                    # No active processes — one row with empty process fields
                    rows.append({
                        **base_row,
                        "process_id": "",
                        "process_name": "",
                        "process_category": "",
                        "process_status": "none",
                    })
                else:
                    for i, pname in enumerate(proc_names):
                        rows.append({
                            **base_row,
                            "process_id": proc_ids[i] if i < len(proc_ids) else "",
                            "process_name": pname,
                            "process_category": self._classify_process(pname),
                            "process_status": "active",
                        })

        return rows

    def get_import_queue(self, workspace_id: str, business_relation_id: str = None,
                         operation_type: str = "CREATE", ready_to_import: bool = False,
                         organization_id: str = None) -> list:
        """Get DATEV import queue items for a workspace.

        operation_type: 'CREATE' or 'UPDATE'
        ready_to_import: True or False

        Scope: provide either `business_relation_id` (old API) or `organization_id`
        (new API, BCP-8019 resolved 2026-04). The new API also reaches orgs that
        have no customers — previously invisible.

        Returns items with schema (new API):
          id, externalId, clientName, workspaceId, organizationId, createdAt, updatedAt,
          readyForImport, type (CREATE|UPDATE),
          clientType (constant "CUSTOMER" in new API — marker that new API is live),
          customerCategoryType (ENTITY=Client/Mandant | INDIVIDUAL=Contact/Person),
          imported, ubo, shareholder, legalRep, actingPerson
        """
        params = {
            "operationType": operation_type,
            "readyToImport": str(ready_to_import).lower(),
        }
        if organization_id:
            params["organizationId"] = organization_id
        elif business_relation_id:
            params["businessRelationId"] = business_relation_id
        else:
            raise ValueError("Either business_relation_id or organization_id is required")
        r = requests.get(
            f"{self.base_url}/api/import/queues",
            headers=self._user_headers_for(workspace_id),
            params=params,
            verify=self.session.verify,
        )
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []

    def get_import_queue_rest(self, workspace_id: str, organization_id: str,
                                ready_for_import: bool = None,
                                operation_type: str = None,
                                query: str = None,
                                page_size: int = 200) -> list:
        """REST queue view including CONTACT items (the legacy /api/import/queues hides them).

        GET /restapi/v1/workspaces/{ws}/organizations/{org}/import/queues
        Auth: workspace API-key Bearer token (same as /auth/login flow).

        Item schema (QueueListResultResultsInner):
          id, externalId, externalContactId,
          target ('CUSTOMER' | 'CONTACT'),
          displayName, readyForImport,
          type ('CREATE' | 'UPDATE'),
          isLegalRep, isUBO, isActingPerson, isShareholder

        externalContactId is non-null iff target == 'CONTACT'. Filter args map to
        query params ready_for_import, operation_type, query (clientName search).
        Returns the flattened `results` list across all pages.
        """
        self._ensure_auth()
        url = (f"{self.base_url}/restapi/v1/workspaces/{workspace_id}"
               f"/organizations/{organization_id}/import/queues")
        params = {"size": page_size, "page": 0}
        if ready_for_import is not None:
            params["ready_for_import"] = str(ready_for_import).lower()
        if operation_type is not None:
            params["operation_type"] = operation_type
        if query is not None:
            params["query"] = query
        items = []
        while True:
            r = self.session.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            items.extend(data.get("results") or [])
            if not data.get("next"):
                break
            params["page"] += 1
        return items

    def post_error_log(self, workspace_id: str, message: str, path: str,
                       code: str = None, http_method: str = None,
                       organization_id: str = None, customer_id: str = None,
                       case_id: str = None, process_id: str = None,
                       occurred_at: str = None, stack_trace: str = None,
                       details: dict = None) -> dict:
        """POST an error log entry — fire-and-forget endpoint for connectors.

        POST /restapi/v1/workspaces/{ws}/error-logs
        Auth: workspace API-key Bearer token.

        Required: message, path. All other args optional. `details` is a
        Map<string,string> — values must be strings; non-string values will be
        coerced via str().

        `occurred_at` should be ISO-8601 UTC, e.g. '2026-04-30T10:47:31Z'.

        Returns the API's 201 body: {id, referenceId, createdAt}.

        Note: there is no public GET endpoint to read what was POSTed back —
        admin reads go through super-user (TBD pattern, mirrors heartbeat).
        """
        self._ensure_auth()
        body = {"message": message, "path": path}
        if code is not None: body["code"] = code
        if http_method is not None: body["httpMethod"] = http_method
        if organization_id is not None: body["organizationId"] = organization_id
        if customer_id is not None: body["customerId"] = customer_id
        if case_id is not None: body["caseId"] = case_id
        if process_id is not None: body["processId"] = process_id
        if occurred_at is not None: body["occurredAt"] = occurred_at
        if stack_trace is not None: body["stackTrace"] = stack_trace
        if details is not None:
            body["details"] = {k: str(v) for k, v in details.items()}
        r = self.session.post(
            f"{self.base_url}/restapi/v1/workspaces/{workspace_id}/error-logs",
            json=body,
        )
        r.raise_for_status()
        return r.json()

    def get_import_queue_detail(self) -> list:
        """Flat detail rows of DATEV import queues across all workspaces.

        Returns one row per queue item with workspace/org context,
        tokenized client names, and cross-reference to existing customers.
        """
        import re
        workspaces = self.list_workspaces()
        rows = []
        for ws in workspaces:
            ws_id = ws.get("id")
            ws_name = ws.get("name", "?")
            log.info("Import queue scan: %s", ws_name)
            try:
                orgs = self.get_workspace_orgs(ws_id)
                customers = self.get_workspace_customers(ws_id)
            except Exception as e:
                log.warning("Failed workspace %s: %s", ws_name, e)
                continue

            if not customers:
                log.info("  No customers, skipping queue (businessRelationId required)")
                continue

            log.info("  %d orgs, %d customers", len(orgs), len(customers))
            for o in orgs:
                oname = o.get("legalName", o.get("name", "?"))
                ocust = sum(1 for c in customers if c.get("advisorActorId") == o.get("id"))
                log.info("    Org: %s (%d customers)", oname, ocust)

            # Build org lookup and customer actor set
            org_lookup = {o.get("id"): o.get("legalName", "") for o in orgs}
            cust_actors = {c.get("actorId") for c in customers}

            # One anchor br_id per org (API scopes queue by br_id's org)
            org_anchor = {}
            for cx in customers:
                oid = cx.get("advisorActorId", "")
                if oid and oid not in org_anchor:
                    org_anchor[oid] = cx["businessRelationId"]

            # Query all 4 combinations per org anchor
            all_items = []
            for anchor_org_id, br_id in org_anchor.items():
                for op_type in ("CREATE", "UPDATE"):
                    for ready in (False, True):
                        try:
                            items = self.get_import_queue(ws_id, br_id, op_type, ready)
                            all_items.extend([(item, op_type, ready) for item in items])
                        except Exception as e:
                            log.warning("  Queue %s/%s for org %s failed: %s",
                                        op_type, ready, org_lookup.get(anchor_org_id, "?"), e)
                log.info("    Queue for %s: queried", org_lookup.get(anchor_org_id, "?"))

            for item, op_type, ready in all_items:
                client_name = item.get("clientName", "")
                m = re.match(r"^(\d+)", client_name)
                datev_num = int(m.group(1)) if m else 0
                actor_id = item.get("actorId", "")

                rows.append({
                    "workspace_id": ws_id,
                    "workspace_name": ws_name,
                    "org_id": item.get("organizationId", ""),
                    "org_name": org_lookup.get(item.get("organizationId", ""), ""),
                    "queue_id": item.get("id", ""),
                    "external_id": item.get("externalId", ""),
                    "datev_number": datev_num,
                    "client_name_token": self._tokenize(client_name),
                    "operation_type": op_type,
                    "ready_to_import": ready,
                    "enrich_source": item.get("enrichDataSource", ""),
                    "actor_id": actor_id,
                    "is_existing_customer": actor_id in cust_actors if actor_id else False,
                    "created_at": item.get("createdAt", ""),
                    "updated_at": item.get("updatedAt", ""),
                })

            log.info("  %s: %d queue items", ws_name, sum(1 for r in rows if r["workspace_id"] == ws_id))

        return rows

    _RISK_SCORE = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "VERY_HIGH": 4}

    def get_legal_types(self) -> dict:
        """Get legal type code -> label mapping from /api/legal-types.

        Returns flat dict like {"1070": "GmbH", "1020": "AG", ...}.
        """
        r = requests.get(
            f"{self.base_url}/api/legal-types",
            headers={"Authorization": f"Bearer {self._get_user_token()}"},
            verify=self.session.verify,
        )
        r.raise_for_status()
        mapping = {}
        for group in r.json():
            for lt in group.get("legalNames", []):
                mapping[lt["id"]] = lt["name"]
        return mapping

    def export_workspace_risk(self, workspace_id: str, workspace_name: str = "",
                              legal_types: dict = None) -> dict:
        """Export risk data for a single workspace per the Risk Dashboard spec.

        Calls get_full_data() per customer to extract amlProfile fields.
        Returns JSON structure ready for dashboard consumption.

        legal_types: optional pre-fetched code->label mapping (avoids extra API call).
        """
        from datetime import datetime

        if legal_types is None:
            legal_types = self.get_legal_types()

        customers = self.get_workspace_customers(workspace_id)
        members = self.get_workspace_members(workspace_id)
        orgs = self.get_workspace_orgs(workspace_id)
        org_lookup = {o.get("id"): o.get("legalName", o.get("name", ""))
                      for o in orgs}
        log.info("  %d orgs, %d customers", len(orgs), len(customers))
        for o in orgs:
            oname = o.get("legalName", o.get("name", "?"))
            ocust = sum(1 for c in customers if c.get("advisorActorId") == o.get("id"))
            log.info("    Org: %s (%d customers)", oname, ocust)
        risk_dist = {"NONE": 0, "LOW": 0, "MEDIUM": 0, "HIGH": 0, "VERY_HIGH": 0, "unknown": 0}
        customer_rows = []

        for i, cust in enumerate(customers):
            br_id = cust.get("businessRelationId", "")
            log.info("  [%d/%d] %s", i + 1, len(customers), br_id)
            try:
                fd = self.get_full_data(br_id, workspace_id=workspace_id)
            except Exception as e:
                log.warning("  Failed full-data for %s: %s", br_id, e)
                fd = {}

            aml = fd.get("amlProfile", {})
            sp = aml.get("screeningProfile", {})
            rp = aml.get("riskProfile", {})

            # Risk level
            risk_level = sp.get("riskLevel")
            if risk_level and risk_level in self._RISK_SCORE:
                risk_score = self._RISK_SCORE[risk_level]
                risk_dist[risk_level] += 1
            else:
                risk_level = None
                risk_score = -1
                risk_dist["unknown"] += 1

            # Legal type — code from full-data, resolved via mapping
            eli = fd.get("entityLegalInfo", {})
            lt = eli.get("legalType", "")
            if isinstance(lt, dict):
                legal_type = lt.get("typeDe", lt.get("typeEn", lt.get("id", "")))
            elif lt and str(lt) in legal_types:
                legal_type = legal_types[str(lt)]
            else:
                legal_type = str(lt) if lt else ""

            # WZ code and financial sectors
            seg = eli.get("segmentCodes", {})
            wz_codes = seg.get("wz", []) if isinstance(seg, dict) else []
            matter = fd.get("matter", {})
            fin_sectors = matter.get("financialSectors", []) if isinstance(matter, dict) else []

            # Country from address
            ai = fd.get("addressInfo", {})
            addrs = ai.get("addresses", [])
            country = addrs[0].get("country", "") if addrs else cust.get("registerCountry", "")

            # Partner / manager / team initials
            partner = cust.get("responsiblePartner") or {}
            manager = cust.get("responsibleManager") or {}
            team_ids = cust.get("caseTeamMembers") or []
            team_initials = ",".join(
                members.get(tid, {}).get("initials", "?") for tid in team_ids
            )

            cust_org_id = cust.get("advisorActorId", "")
            customer_rows.append({
                "customer_id": br_id,
                "name_token": self._tokenize(cust.get("legalName", "")),
                "org_id": cust_org_id,
                "org_name": org_lookup.get(cust_org_id, ""),
                "type": cust.get("type", ""),
                "category": cust.get("categoryType", ""),
                "legal_type": legal_type,
                "wz_code": wz_codes[0] if wz_codes else "",
                "financial_sectors": fin_sectors,
                "country": country,
                "partner": self._initials(partner.get("name", "")),
                "manager": self._initials(manager.get("name", "")),
                "team": team_initials,
                "risk_level": risk_level or "unknown",
                "risk_score": risk_score,
                "match_status": sp.get("matchStatus", ""),
                "total_matches": sp.get("totalMatches", 0),
                "last_screening": sp.get("lastScreeningDate", ""),
                "monitoring": sp.get("isMonitoringEnabled", False),
                "pep": rp.get("anyPep", False),
                "cash_sensitive": aml.get("isCashSensitive", False),
                "arms_trade": aml.get("isArmsTrade", False),
                "regulated": aml.get("isRegulated", False),
            })

        return {
            "workspace": workspace_name or workspace_id,
            "workspace_id": workspace_id,
            "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            "total_customers": len(customers),
            "total_orgs": len(orgs),
            "orgs": [{"id": o.get("id"), "name": o.get("legalName", o.get("name", ""))}
                     for o in orgs],
            "risk_distribution": risk_dist,
            "customers": customer_rows,
        }

    def export_all_risk(self, workspace_ids: list = None) -> list:
        """Export risk data across all (or selected) workspaces.

        Returns list of workspace risk dicts.
        """
        legal_types = self.get_legal_types()
        workspaces = self.list_workspaces()
        results = []
        seen_names = {}
        for ws in workspaces:
            ws_id = ws.get("id")
            ws_name = ws.get("name", "?")
            if workspace_ids and ws_id not in workspace_ids:
                continue

            # Deduplicate workspace names by appending short ID
            if ws_name in seen_names:
                ws_label = f"{ws_name}_{ws_id[:6]}"
            else:
                ws_label = ws_name
            seen_names[ws_name] = True

            log.info("Risk export: %s (%s)", ws_label, ws_id)
            try:
                customers = self.get_workspace_customers(ws_id)
                if not customers:
                    log.info("  No customers, skipping")
                    continue
                result = self.export_workspace_risk(ws_id, ws_label, legal_types=legal_types)
                results.append(result)
            except Exception as e:
                log.warning("  Failed: %s", e)
        return results

    def generate_morning_report(self, stale_minutes: int = 30) -> dict:
        """Comprehensive morning report across all workspaces.

        Combines: workspace overview, customer/process stats, import queue,
        and connector health into a single report.

        stale_minutes: connector considered down if last sync > this many minutes ago.
        """
        import re
        from datetime import datetime

        now = datetime.utcnow()
        workspaces = self.list_workspaces()
        report = {
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "workspace_count": len(workspaces),
            "total_orgs": 0,
            "total_customers": 0,
            "total_active_processes": 0,
            "total_queue_items": 0,
            "connector_live": 0,
            "connector_down": 0,
            "connector_none": 0,
            "connector_unknown": 0,
            "workspaces": [],
        }

        for ws in workspaces:
            ws_id = ws.get("id")
            ws_name = ws.get("name", "?")
            ws_role = ws.get("userWorkspaceRole", "?")
            log.info("Morning report: %s", ws_name)

            ws_report = {
                "workspace_id": ws_id,
                "workspace_name": ws_name,
                "role": ws_role,
                "status": ws.get("status"),
            }

            # Customers & orgs
            try:
                orgs = self.get_workspace_orgs(ws_id)
                members = self.get_workspace_members(ws_id)
                customers = self.get_workspace_customers(ws_id)
            except Exception as e:
                log.warning("  Failed: %s", e)
                ws_report.update({"error": str(e), "customer_count": 0})
                report["workspaces"].append(ws_report)
                continue

            org_lookup = {o.get("id"): o.get("legalName", o.get("name", ""))
                          for o in orgs}
            ws_report["orgs"] = [{"id": o.get("id"), "name": o.get("legalName", o.get("name", ""))} for o in orgs]
            ws_report["org_count"] = len(orgs)
            ws_report["member_count"] = len(members)
            ws_report["customer_count"] = len(customers)
            report["total_orgs"] += len(orgs)
            report["total_customers"] += len(customers)
            log.info("  %d orgs, %d customers, %d members", len(orgs), len(customers), len(members))
            for o in orgs:
                oname = o.get("legalName", o.get("name", "?"))
                ocust = sum(1 for c in customers if c.get("advisorActorId") == o.get("id"))
                log.info("    Org: %s (%d customers)", oname, ocust)

            # Customer breakdown
            clients = sum(1 for c in customers if c.get("type") == "CLIENT")
            leads = sum(1 for c in customers if c.get("type") == "LEAD")
            entities = sum(1 for c in customers if c.get("categoryType") == "ENTITY")
            individuals = sum(1 for c in customers if c.get("categoryType") == "INDIVIDUAL")
            ws_report["clients"] = clients
            ws_report["leads"] = leads
            ws_report["entities"] = entities
            ws_report["individuals"] = individuals

            # Per-org breakdown — start with ALL orgs (not just those with customers)
            org_stats = {}
            for o in orgs:
                oid = o.get("id", "")
                org_stats[oid] = {"id": oid, "name": o.get("legalName", o.get("name", "")),
                                  "customer_count": 0, "active_processes": 0}
            for c in customers:
                oid = c.get("advisorActorId", "")
                if oid not in org_stats:
                    org_stats[oid] = {"id": oid, "name": org_lookup.get(oid, ""),
                                      "customer_count": 0, "active_processes": 0}
                org_stats[oid]["customer_count"] += 1
                org_stats[oid]["active_processes"] += len(c.get("activeProcesses") or [])
            ws_report["org_breakdown"] = list(org_stats.values())

            # Process breakdown
            process_counts = {}
            total_active = 0
            for c in customers:
                for pname in (c.get("activeProcesses") or []):
                    cat = self._classify_process(pname)
                    process_counts[cat] = process_counts.get(cat, 0) + 1
                    total_active += 1
            ws_report["active_processes"] = total_active
            ws_report["processes_by_category"] = process_counts
            report["total_active_processes"] += total_active

            # Import queue — iterate ALL orgs via organizationId (new API, BCP-8019).
            # Previously we needed a customer's businessRelationId per org, which missed
            # orgs without customers entirely. The new API accepts organizationId directly,
            # so every org in the workspace is now reachable.
            queue_stats = {"create_pending": 0, "create_ready": 0,
                           "update_pending": 0, "update_ready": 0,
                           "latest_sync": None,
                           # New-API breakdown — only meaningful when items carry clientType
                           "clientType_CUSTOMER": 0,      # clientType == "CUSTOMER"
                           "clientType_other": 0,         # any other value (incl. missing = old API)
                           "category_ENTITY": 0,          # customerCategoryType ENTITY = Client
                           "category_INDIVIDUAL": 0,      # customerCategoryType INDIVIDUAL = Contact
                           "is_new_api": False}           # true when any item has clientType field
            queue_per_org = {}          # org_id -> count
            queue_breakdown_per_org = {} # org_id -> {ENTITY, INDIVIDUAL, clientType_CUSTOMER}

            def _poll_queue():
                """Query every org in ws via organizationId, all 4 states. Returns aggregates."""
                totals = {"create_pending": 0, "create_ready": 0,
                          "update_pending": 0, "update_ready": 0}
                latest = None
                per_org = {}
                per_org_bd = {}
                ct_custom = ct_other = cat_ent = cat_ind = 0
                is_new_api = False
                n = 0
                for o in orgs:
                    oid = o.get("id")
                    if not oid:
                        continue
                    per_org_bd.setdefault(oid, {"clientType_CUSTOMER": 0, "category_ENTITY": 0, "category_INDIVIDUAL": 0})
                    for op_type in ("CREATE", "UPDATE"):
                        for ready in (False, True):
                            try:
                                items = self.get_import_queue(ws_id, organization_id=oid,
                                                              operation_type=op_type, ready_to_import=ready)
                            except Exception as e:
                                log.debug("    queue fetch failed org=%s op=%s: %s", oid[:8], op_type, e)
                                continue
                            key = f"{op_type.lower()}_{'ready' if ready else 'pending'}"
                            totals[key] += len(items)
                            n += len(items)
                            for item in items:
                                ua = item.get("updatedAt", "")
                                if ua and (latest is None or ua > latest):
                                    latest = ua
                                qorg = item.get("organizationId", oid)
                                per_org[qorg] = per_org.get(qorg, 0) + 1
                                per_org_bd.setdefault(qorg, {"clientType_CUSTOMER": 0, "category_ENTITY": 0, "category_INDIVIDUAL": 0})
                                ct = item.get("clientType")
                                if ct is not None:
                                    is_new_api = True
                                    if ct == "CUSTOMER":
                                        ct_custom += 1
                                        per_org_bd[qorg]["clientType_CUSTOMER"] += 1
                                    else:
                                        ct_other += 1
                                cat = item.get("customerCategoryType")
                                if cat == "ENTITY":
                                    cat_ent += 1
                                    per_org_bd[qorg]["category_ENTITY"] += 1
                                elif cat == "INDIVIDUAL":
                                    cat_ind += 1
                                    per_org_bd[qorg]["category_INDIVIDUAL"] += 1
                return n, latest, totals, per_org, per_org_bd, ct_custom, ct_other, cat_ent, cat_ind, is_new_api

            # First pass
            (total_items, latest_sync, per_op, per_org, per_org_bd,
             ct_cust, ct_other, cat_ent, cat_ind, is_new_api) = _poll_queue()

            # If pass 1 returned 0 items, wait 30s and retry once. Don't over-explain —
            # queues of workspaces with an active connector should not read 0, so a
            # single retry is the cheapest defensive check. Pass 1 with items = done.
            if total_items == 0 and len(orgs) > 0:
                import time as _t
                _t.sleep(30)
                (t2, l2, p2, po2, pob2, c2, o2, e2, i2, na2) = _poll_queue()
                if t2 > 0:
                    log.info("    Queue resample caught %d items after 30s lazy-load warmup", t2)
                    (total_items, latest_sync, per_op, per_org, per_org_bd,
                     ct_cust, ct_other, cat_ent, cat_ind, is_new_api) = \
                        (t2, l2, p2, po2, pob2, c2, o2, e2, i2, na2)

            for k in ("create_pending", "create_ready", "update_pending", "update_ready"):
                queue_stats[k] = per_op[k]
            queue_stats["latest_sync"] = latest_sync
            queue_stats["clientType_CUSTOMER"] = ct_cust
            queue_stats["clientType_other"] = ct_other
            queue_stats["category_ENTITY"] = cat_ent
            queue_stats["category_INDIVIDUAL"] = cat_ind
            queue_stats["is_new_api"] = is_new_api
            report["total_queue_items"] += total_items
            queue_per_org = per_org

            # Report-wide aggregates for new-API breakdown
            report["total_queue_clients"] = report.get("total_queue_clients", 0) + cat_ent
            report["total_queue_contacts"] = report.get("total_queue_contacts", 0) + cat_ind
            report["total_queue_customer_type"] = report.get("total_queue_customer_type", 0) + ct_cust

            for o in orgs:
                oid = o.get("id")
                org_name = org_lookup.get(oid, "?")
                log.info("    Queue for %s: %d items (clients=%d contacts=%d)",
                         org_name, queue_per_org.get(oid, 0),
                         per_org_bd.get(oid, {}).get("category_ENTITY", 0),
                         per_org_bd.get(oid, {}).get("category_INDIVIDUAL", 0))

            # Attach queue counts + new-API breakdown to org_breakdown
            for ob in ws_report.get("org_breakdown", []):
                oid = ob["id"]
                bd = per_org_bd.get(oid, {})
                ob["queue_items"] = queue_per_org.get(oid, 0)
                ob["queue_clients"]  = bd.get("category_ENTITY", 0)       # Mandanten
                ob["queue_contacts"] = bd.get("category_INDIVIDUAL", 0)   # Kontakte
                ob["queue_clientType_CUSTOMER"] = bd.get("clientType_CUSTOMER", 0)
                # Every org is queryable under the new API — old flag kept for compat
                ob["queue_queryable"] = True

            ws_report["queue"] = queue_stats
            queue_total = queue_stats["create_pending"] + queue_stats["create_ready"] \
                        + queue_stats["update_pending"] + queue_stats["update_ready"]
            ws_report["queue_total"] = queue_total

            # Connector health
            latest = queue_stats["latest_sync"]
            if queue_total == 0 and len(customers) > 0:
                ws_report["connector_status"] = "no_connector"
                report["connector_none"] += 1
            elif queue_total == 0 and len(customers) == 0:
                ws_report["connector_status"] = "unknown"
                report["connector_unknown"] += 1
            elif latest:
                try:
                    last_sync = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S")
                    age_minutes = (now - last_sync).total_seconds() / 60
                    ws_report["connector_age_minutes"] = round(age_minutes)
                    if age_minutes <= stale_minutes:
                        ws_report["connector_status"] = "live"
                        report["connector_live"] += 1
                    else:
                        ws_report["connector_status"] = "down"
                        report["connector_down"] += 1
                except Exception:
                    ws_report["connector_status"] = "unknown"
                    report["connector_unknown"] += 1
            else:
                ws_report["connector_status"] = "unknown"
                report["connector_unknown"] += 1

            report["workspaces"].append(ws_report)

        return report

    @staticmethod
    def format_morning_report(report: dict) -> str:
        """Format morning report as human-readable text."""
        lines = []
        lines.append("=" * 80)
        lines.append("BETTERCO MORNING REPORT")
        lines.append("Generated: {}".format(report["generated_at"]))
        lines.append("=" * 80)
        lines.append("")

        # Summary
        lines.append("SUMMARY")
        lines.append("-" * 40)
        lines.append("  Workspaces:        {:>6d}".format(report["workspace_count"]))
        lines.append("  Total customers:   {:>6d}".format(report["total_customers"]))
        lines.append("  Active processes:  {:>6d}".format(report["total_active_processes"]))
        lines.append("  Queue items:       {:>6,d}".format(report["total_queue_items"]))
        lines.append("")

        # Connector health summary
        lines.append("CONNECTOR HEALTH")
        lines.append("-" * 40)
        lines.append("  Live:              {:>6d}".format(report["connector_live"]))
        lines.append("  Down:              {:>6d}".format(report["connector_down"]))
        lines.append("  No connector:      {:>6d}".format(report["connector_none"]))
        lines.append("  Unknown:           {:>6d}".format(report["connector_unknown"]))
        lines.append("")

        # Down connectors (alert)
        down = [w for w in report["workspaces"] if w.get("connector_status") == "down"]
        if down:
            lines.append("!! CONNECTORS DOWN !!")
            for w in sorted(down, key=lambda x: x.get("connector_age_minutes", 0), reverse=True):
                age = w.get("connector_age_minutes", 0)
                if age > 1440:
                    age_str = "{}d".format(age // 1440)
                elif age > 60:
                    age_str = "{}h".format(age // 60)
                else:
                    age_str = "{}m".format(age)
                lines.append("  {:30s} last sync {} ago".format(w["workspace_name"], age_str))
            lines.append("")

        # Per-workspace detail
        lines.append("WORKSPACE DETAIL")
        lines.append("-" * 80)
        lines.append("{:30s} {:>5s} {:>5s} {:>6s} {:>7s} {:>8s} {:>8s}".format(
            "Workspace", "Cust", "Proc", "Queue", "Connect", "Partner", "Members"))
        lines.append("-" * 80)

        for w in sorted(report["workspaces"], key=lambda x: x.get("customer_count", 0), reverse=True):
            if w.get("error"):
                lines.append("{:30s} ERROR: {}".format(w["workspace_name"], w["error"]))
                continue

            cust = w.get("customer_count", 0)
            procs = w.get("active_processes", 0)
            queue = w.get("queue_total", 0)
            conn = w.get("connector_status", "?")
            conn_icon = {"live": "OK", "down": "DOWN", "no_connector": "NONE", "unknown": "?"}.get(conn, "?")
            members = w.get("member_count", 0)

            # Type breakdown
            cl = w.get("clients", 0)
            ld = w.get("leads", 0)
            type_str = "{}c/{}l".format(cl, ld) if ld > 0 else str(cl)

            lines.append("{:30s} {:>5s} {:>5d} {:>6,d} {:>7s} {:>8s} {:>8d}".format(
                w["workspace_name"], type_str, procs, queue, conn_icon,
                w.get("orgs", [{}])[0].get("name", "")[:8] if w.get("orgs") else "",
                members))

        lines.append("-" * 80)
        lines.append("")

        # Process category breakdown
        all_cats = {}
        for w in report["workspaces"]:
            for cat, count in w.get("processes_by_category", {}).items():
                all_cats[cat] = all_cats.get(cat, 0) + count
        if all_cats:
            lines.append("PROCESS CATEGORIES")
            lines.append("-" * 40)
            for cat in sorted(all_cats, key=all_cats.get, reverse=True):
                lines.append("  {:20s} {:>6d}".format(cat, all_cats[cat]))
            lines.append("")

        return "\n".join(lines)

    # ── Workspace Configuration (User API) ─────────────────────────

    def list_workspaces(self) -> list:
        """List all workspaces the user has access to.

        Requires User API auth (BETTERCO_USER_EMAIL/PASSWORD).
        """
        r = requests.get(
            f"{self.base_url}/api/workspaces",
            headers={"Authorization": f"Bearer {self._get_user_token()}"},
            verify=self.session.verify,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("workspaces", [data])

    def get_workspace(self) -> dict:
        """Get workspace info (name, status, type, language, logo, role).

        Requires User API auth (BETTERCO_USER_EMAIL/PASSWORD).
        """
        self._ensure_auth()
        r = requests.get(
            f"{self.base_url}/api/workspaces/{self.workspace_id}",
            headers=self._user_headers(),
            verify=self.session.verify,
        )
        r.raise_for_status()
        return r.json()

    def get_workspace_settings(self) -> dict:
        """Get all workspace settings with titles, descriptions, and values.

        Returns dict keyed by setting name, each with settingsTitle,
        settingsDescription, value ("true"/"false"), and order.
        Requires User API auth.
        """
        self._ensure_auth()
        r = requests.get(
            f"{self.base_url}/api/workspaces/{self.workspace_id}/settings",
            headers=self._user_headers(),
            verify=self.session.verify,
        )
        r.raise_for_status()
        return r.json()

    def update_workspace_settings(self, settings: dict) -> None:
        """Update workspace settings.

        settings: flat dict of {key: "true"/"false"} — NOT the nested objects
        from get_workspace_settings(). Only include keys you want to change;
        omitted keys keep their current values.

        Example: update_workspace_settings({"autoKYC": "false", "captchaEnabled": "true"})

        Requires User API auth.
        """
        self._ensure_auth()
        # Merge with current settings so omitted keys are preserved
        current = self.get_workspace_settings()
        merged = {k: v["value"] for k, v in current.items()}
        merged.update(settings)
        r = requests.put(
            f"{self.base_url}/api/workspaces/{self.workspace_id}/settings",
            headers=self._user_headers(),
            json=merged,
            verify=self.session.verify,
        )
        r.raise_for_status()
        log.info("Updated workspace settings: %s", list(settings.keys()))

    # ── Workspace Creation ─────────────────────────────────────────

    @staticmethod
    def sign_up(base_url, email, password, first_name, last_name,
                pricing_plan_id="S1_ADVANCE_M_EUR"):
        """Create a new workspace via sign-up. Pre-auth — no token needed.

        Returns the sign-up response (typically includes user/workspace info).
        After sign-up, sign in with the same email/password to get a token,
        then call GET /api/workspaces and GET /api/companies to get IDs.
        """
        r = requests.post(f"{base_url}/auth/sign-up", json={
            "firstName": first_name,
            "lastName": last_name,
            "email": email,
            "password": password,
            "confirmPassword": password,
            "pricingPlanId": pricing_plan_id,
        })
        r.raise_for_status()
        data = r.json()
        log.info("Sign-up OK for %s", email)
        return data

    def list_tasks(self, cid: str, case_id: str, pid: str) -> list:
        """List all tasks on a process with their status."""
        proc = self.get_process(cid, case_id, pid)
        return proc.get("tasks", [])

    def search_registry(self, query: str, domain: str = "ENTITY") -> list:
        """Search NorthData registry for a company or person.

        domain: 'ENTITY' or 'PERSON'
        Returns list of matches with externalRegistryId, legalName, address, etc.
        """
        r = requests.get(
            f"{self.base_url}/api/registry/search",
            params={"domain": domain, "query": query},
            headers=self._user_headers(),
            verify=self.session.verify,
        )
        r.raise_for_status()
        return r.json()

    def create_customer_from_registry(self, external_registry_id: str, name: str,
                                      domain: str = "ENTITY",
                                      category_type: str = "ENTITY",
                                      as_lead: bool = False,
                                      poll_timeout: float = 60,
                                      purchase_documents: bool = False) -> dict:
        """Create a fully populated customer via NorthData/company.info.

        Uses POST /api/customers (CLIENT, full structure chart) or
        POST /api/leads (LEAD, NorthData only).

        purchase_documents: when True, BetterCo also purchases the UBO document
        (Transparenzregister extract) as part of the create call.

        Polls until isFullyInitialized, then returns {businessRelationId, contacts, documents, ...}.
        """
        org_id = os.getenv("BETTERCO_ORG_ID")
        endpoint = f"{self.base_url}/api/{'leads' if as_lead else 'customers'}"
        payload = {
            "clientActorExternalId": external_registry_id,
            "advisorActorId": org_id,
            "customerCategoryType": category_type,
            "clientActorName": name,
            "domain": domain,
            "purchaseDocuments": purchase_documents,
        }
        headers = self._user_headers()
        r = requests.post(endpoint, json=payload, headers=headers, verify=self.session.verify)
        r.raise_for_status()
        data = r.json()
        cid = data["businessRelationId"]
        log.info("Created %s %s (%s) — polling for completion",
                 "lead" if as_lead else "customer", cid, name)

        # Poll until fully initialized
        t_start = time.time()
        while time.time() - t_start < poll_timeout:
            r2 = requests.get(
                f"{self.base_url}/api/customers/business-relation",
                params={"businessRelationId": cid},
                headers=headers,
                verify=self.session.verify,
            )
            if r2.ok and r2.json().get("isFullyInitialized"):
                break
            time.sleep(2)
        else:
            log.warning("Timed out waiting for isFullyInitialized on %s", cid)

        elapsed = time.time() - t_start
        self._ensure_auth()
        contacts = self.list_contacts(cid)
        docs = self.list_customer_documents(cid)
        log.info("Customer %s ready in %.1fs — %d contacts, %d docs", cid, elapsed, len(contacts), len(docs))
        return {
            "businessRelationId": cid,
            "type": data.get("type"),
            "contacts": len(contacts),
            "documents": len(docs),
            "elapsed_s": round(elapsed, 1),
            **data,
        }

    def create_customer_from_contact(self, source_cid: str, contact_id: str,
                                     as_lead: bool = False) -> dict:
        """Create a new customer from an existing contact using NorthData lookup.

        Searches the registry for the contact's name, then creates a fully
        populated customer via create_customer_from_registry.

        Returns dict with businessRelationId, contacts count, etc.
        """
        self._ensure_auth()
        # Fetch contact detail
        contact_url = self._url(f"/customers/{source_cid}/contacts/{contact_id}")
        r = self.session.get(contact_url)
        r.raise_for_status()
        contact = r.json()

        ctype = contact.get("type", "ENTITY")
        legal_name = contact.get("legalName", "")
        first_name = contact.get("firstName", "")
        last_name = contact.get("lastName", "")

        if ctype == "INDIVIDUAL":
            query = f"{first_name} {last_name}".strip()
            domain = "PERSON"
            category_type = "INDIVIDUAL"
        else:
            query = legal_name
            domain = "ENTITY"
            category_type = "ENTITY"

        # Search registry
        results = self.search_registry(query, domain)
        if not results:
            raise ValueError(f"No registry results for '{query}' (domain={domain})")

        # Use first match
        match = results[0]
        log.info("Registry match: %s (id=%s)", match.get("legalName"), match.get("externalRegistryId"))

        return self.create_customer_from_registry(
            external_registry_id=match["externalRegistryId"],
            name=match.get("legalName", query),
            domain=domain,
            category_type=category_type,
            as_lead=as_lead,
        )

    def download_document(self, download_uri: str, dest_path: str):
        """Download a document to a local file."""
        self._ensure_auth()
        r = self.session.get(download_uri, stream=True)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        log.info("Downloaded %s", Path(dest_path).name)

    # ── Super-User scope: Heartbeat Monitoring ───────────────────────
    #
    # Cross-tenant DATEV connector heartbeats. Auth uses a separate
    # super-user credential pool (NOT the per-workspace user).
    #
    # Setup — provide super-user credentials in ONE of these ways:
    #   1. Add to your workspace .env (loaded by dotenv on import):
    #        BETTERCO_SUPERUSER=superuser
    #        BETTERCO_SUPERUSER_PASSWORD=<secret>
    #   2. Export as shell env vars before running.
    #   3. Pass `super_env_path=Path(...)` to any heartbeat_* method.
    #      The file is parsed line-by-line, tolerating non-KV junk lines
    #      (some legacy super-user env files contain them).
    #   4. Pass `email=`/`password=` directly.

    @staticmethod
    def _parse_super_env(path):
        """Read BETTERCO_SUPERUSER / BETTERCO_SUPERUSER_PASSWORD from a
        dotenv-malformed-tolerant file. Returns (email, password)."""
        path = Path(path)
        email = pw = None
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                if line.startswith("BETTERCO_SUPERUSER="):
                    email = line.split("=", 1)[1]
                elif line.startswith("BETTERCO_SUPERUSER_PASSWORD="):
                    pw = line.split("=", 1)[1]
        if not email or not pw:
            raise RuntimeError(
                f"Missing BETTERCO_SUPERUSER / BETTERCO_SUPERUSER_PASSWORD in {path}"
            )
        return email, pw

    def _get_super_token(self, super_env_path=None, email=None, password=None):
        """Get a super-user bearer token (cross-tenant scope).

        Resolution order: explicit email+password > super_env_path file >
        BETTERCO_SUPERUSER + BETTERCO_SUPERUSER_PASSWORD env vars.
        Cached for ~3h on the instance.
        """
        if (getattr(self, "_super_token", None)
                and time.time() < getattr(self, "_super_token_expiry", 0)):
            return self._super_token

        if not (email and password):
            if super_env_path:
                email, password = self._parse_super_env(super_env_path)
            else:
                email = os.getenv("BETTERCO_SUPERUSER")
                password = os.getenv("BETTERCO_SUPERUSER_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "Super-user credentials missing. Set BETTERCO_SUPERUSER and "
                "BETTERCO_SUPERUSER_PASSWORD in your .env, or pass "
                "super_env_path=Path('.../prod-eckhard-super.env')."
            )

        # Super-user routes through the same /auth/sign-in endpoint with
        # ?superUser=true. Without the flag, super-user emails return 404.
        r = requests.post(
            f"{self.base_url}/auth/sign-in",
            params={"superUser": "true"},
            data={"email": email, "password": password},
            verify=self.session.verify,
            timeout=20,
        )
        r.raise_for_status()
        self._super_token = r.json()["token"]
        self._super_token_expiry = time.time() + 10800
        log.info("Authenticated as super-user")
        return self._super_token

    def _super_headers(self, super_env_path=None):
        return {
            "Authorization": f"Bearer {self._get_super_token(super_env_path)}",
            "Accept": "application/json",
        }

    def ci_enrichment(self, workspace_id: str, *, recalculate_kyc: bool = False,
                      super_env_path=None) -> dict:
        """Generate shareholder graphs for every German ENTITY client in a workspace
        that doesn't yet have one. SUPER_USER role required.

        POST /api/dashboard/workspaces/{workspaceId}/ci-enrichment

        For each in-scope actor it tries the Company Info (CI) shareholder API
        first, then falls back to a synthetic graph built from the actor's local
        shareholder/UBO/intermediate relations (recursive). Locally-built graphs
        are tagged additionalData.shareholderGraphSource="local" and
        shareholderProfile.shareholderGraphConfirmed=false. Skips non-German,
        non-entity (individual), and already-graphed actors.

        recalculate_kyc=True re-runs AutoKYC for each actor after a graph is built.

        Returns {total, enrichedFromCi, enrichedLocally, skippedNoData, failed,
        failedActors:["actorId (legalName)", ...]} — total counts only the German
        entity clients without a graph that were actually processed."""
        headers = self._super_headers(super_env_path)
        r = requests.post(
            f"{self.base_url}/api/dashboard/workspaces/{workspace_id}/ci-enrichment",
            headers=headers,
            params={"recalculateKyc": "true" if recalculate_kyc else "false"},
            verify=self.session.verify,
            timeout=300,
        )
        if r.status_code >= 400:
            log.error("ci_enrichment failed (%d): %s", r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json() if r.text else {}

    def heartbeat_logs(self, hours: int = 48, super_env_path=None,
                       page_size: int = 500) -> list:
        """Fetch heartbeat log entries within the last `hours`.

        Pages through GET /api/monitoring/heartbeat/logs (sorted receivedAt
        desc) and stops when entries cross the cutoff. Cross-tenant: returns
        every partner's pings.
        """
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        headers = self._super_headers(super_env_path)
        items, page = [], 0
        while True:
            r = requests.get(
                f"{self.base_url}/api/monitoring/heartbeat/logs",
                headers=headers,
                params={"page": page, "size": page_size},
                verify=self.session.verify,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            content = data.get("content") or []
            if not content:
                break
            for it in content:
                ts_raw = it.get("receivedAt")
                if ts_raw:
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        if ts < since:
                            return items
                    except ValueError:
                        pass
                items.append(it)
            if data.get("last"):
                break
            page += 1
        return items

    def heartbeat_alerts(self, super_env_path=None, resolved=None,
                         page_size: int = 500) -> list:
        """Fetch heartbeat alerts. Pass `resolved=False` for open alerts only."""
        headers = self._super_headers(super_env_path)
        items, page = [], 0
        params = {"page": 0, "size": page_size}
        if resolved is not None:
            params["resolved"] = "true" if resolved else "false"
        while True:
            params["page"] = page
            r = requests.get(
                f"{self.base_url}/api/monitoring/heartbeat/alerts",
                headers=headers,
                params=params,
                verify=self.session.verify,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            content = data.get("content") or []
            items.extend(content)
            if data.get("last") or not content:
                break
            page += 1
        return items

    def get_error_logs(self, hours: int = 48, super_env_path=None,
                       workspace_id: str = None, partner_id: str = None,
                       page_size: int = 500) -> list:
        """Fetch connector error-log entries within the last `hours`.

        Pages through GET /api/monitoring/error-logs (super-user scope, sorted
        occurredAt desc) and stops when entries cross the cutoff. Cross-tenant
        by default; pass `workspace_id` or `partner_id` to filter.
        """
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        headers = self._super_headers(super_env_path)
        items, page = [], 0
        while True:
            params = {"page": page, "size": page_size, "sort": "occurredAt,desc"}
            if workspace_id:
                params["workspaceId"] = workspace_id
            if partner_id:
                params["partnerId"] = partner_id
            r = requests.get(
                f"{self.base_url}/api/monitoring/error-logs",
                headers=headers,
                params=params,
                verify=self.session.verify,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            content = data.get("content") or []
            if not content:
                break
            for it in content:
                ts_raw = it.get("occurredAt") or it.get("createdAt")
                if ts_raw:
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < since:
                            return items
                    except ValueError:
                        pass
                items.append(it)
            if data.get("last"):
                break
            page += 1
        return items

    def heartbeat_snapshot(self, hours: int = 48, super_env_path=None,
                           workspaces=None,
                           default_cadence_sec: int = 600) -> dict:
        """Run a full heartbeat snapshot. Classifies each partner UP / LATE /
        DOWN / UNKNOWN. If a partner has too few pings to compute its own
        median cadence, falls back to `default_cadence_sec` (10 min — the
        observed prod baseline).

        An open server-side alert is only honoured if it was raised AFTER
        the most recent ping; an alert older than the last_seen is treated
        as stale (the partner has recovered).

            UP       recent ping AND no active (newer-than-last-ping) alert
            LATE     2 x cadence <= seconds_since_last < 4 x cadence
            DOWN     active alert OR seconds_since_last >= 4 x cadence
            UNKNOWN  zero pings in window AND no active alert (truly idle)

        If `workspaces` is provided (list of {id, name}), each row is
        enriched with workspace_id / workspace_name. Returns the same dict
        shape as dashboard/heartbeat_snapshot.py:
            {generated_at, window_hours, counts, total_partners, rows[]}
        """
        import re
        import statistics
        from datetime import datetime, timezone

        logs = self.heartbeat_logs(hours=hours, super_env_path=super_env_path)
        alerts = self.heartbeat_alerts(super_env_path=super_env_path)

        def _parse(ts):
            if not ts:
                return None
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                return None

        now = datetime.now(timezone.utc)
        by_partner = {}
        for it in logs:
            pid = it.get("partnerId")
            ts = _parse(it.get("receivedAt"))
            if not pid or not ts:
                continue
            by_partner.setdefault(pid, []).append((ts, it))

        open_alerts = {}
        for a in alerts:
            if a.get("resolved"):
                continue
            pid = a.get("partnerId")
            if not pid:
                continue
            ts = _parse(a.get("alertSentAt"))
            prev = open_alerts.get(pid)
            if prev is None or (ts and ts > prev):
                open_alerts[pid] = ts

        rows = []
        for pid, entries in by_partner.items():
            entries.sort(key=lambda x: x[0], reverse=True)
            timestamps = [e[0] for e in entries]
            last_seen = timestamps[0]
            delta_sec = (now - last_seen).total_seconds()
            deltas = [
                (timestamps[i] - timestamps[i + 1]).total_seconds()
                for i in range(min(len(timestamps), 30) - 1)
            ]
            median_interval = statistics.median(deltas) if deltas else None
            cadence = median_interval if median_interval else default_cadence_sec
            alert_ts = open_alerts.get(pid)
            # Stale alert: predates the most recent ping → partner recovered.
            alert_active = bool(alert_ts) and alert_ts > last_seen
            if alert_active or delta_sec >= 4 * cadence:
                status = "DOWN"
            elif delta_sec >= 2 * cadence:
                status = "LATE"
            else:
                status = "UP"
            rows.append({
                "partnerId": pid,
                "status": status,
                "last_seen": last_seen.isoformat(),
                "seconds_since_last": int(delta_sec),
                "ping_count_window": len(timestamps),
                "median_interval_sec": int(median_interval) if median_interval else None,
                "open_alert": pid in open_alerts,
                "open_alert_active": alert_active,
                "open_alert_sent_at": alert_ts.isoformat() if alert_ts else None,
                "ip_recent": entries[0][1].get("ipAddress"),
            })

        for pid, ts in open_alerts.items():
            if pid in by_partner:
                continue
            rows.append({
                "partnerId": pid, "status": "DOWN",
                "last_seen": None, "seconds_since_last": None,
                "ping_count_window": 0, "median_interval_sec": None,
                "open_alert": True, "open_alert_active": True,
                "open_alert_sent_at": ts.isoformat() if ts else None,
                "ip_recent": None,
            })

        # Optional workspace enrichment
        hex24 = re.compile(r"^(?P<prefix>.+)_(?P<wsid>[0-9a-f]{24})$", re.IGNORECASE)
        afileon_slug = re.compile(r"^afileon[-_](?P<slug>.+)$", re.IGNORECASE)

        def _norm(name):
            return re.sub(r"[\s\.\-_&+]", "", name or "").upper()

        if workspaces:
            by_id = {(w.get("id") or ""): (w.get("name") or "") for w in workspaces}
            by_norm = {_norm(w.get("name")): (w.get("id"), w.get("name"))
                       for w in workspaces if w.get("name")}
            for r in rows:
                pid = r.get("partnerId") or ""
                ws_id, ws_name = "", ""
                m = hex24.match(pid)
                if m:
                    ws_id = m.group("wsid")
                    ws_name = by_id.get(ws_id, "")
                else:
                    hit = by_norm.get(_norm(pid))
                    if hit:
                        ws_id, ws_name = hit[0], hit[1]
                    else:
                        ms = afileon_slug.match(pid)
                        if ms:
                            hit = by_norm.get(_norm(ms.group("slug")))
                            if hit:
                                ws_id, ws_name = hit[0], hit[1]
                r["workspace_id"] = ws_id
                r["workspace_name"] = ws_name
        else:
            for r in rows:
                r.setdefault("workspace_id", "")
                r.setdefault("workspace_name", "")

        status_order = {"DOWN": 0, "LATE": 1, "UNKNOWN": 2, "UP": 3}
        rows.sort(key=lambda r: (status_order[r["status"]],
                                 -(r["seconds_since_last"] or 0)))
        counts = {"DOWN": 0, "LATE": 0, "UNKNOWN": 0, "UP": 0}
        for r in rows:
            counts[r["status"]] += 1
        return {
            "generated_at": now.isoformat(),
            "window_hours": hours,
            "counts": counts,
            "total_partners": len(rows),
            "rows": rows,
        }


def list_workspace_envs(workspaces_dir=None):
    """List selectable workspace credential files (workspaces/<name>.env).

    Returns a list of {name, label, base_url, workspace_id, has_user_api} for the
    Console workspace picker. Only real .env files with a BETTERCO_WORKSPACE_ID are
    returned; templates/placeholders (e.g. *.txt, empty files) are skipped. Pair the
    chosen ``name`` with ``BetterCoClient.from_workspace_env(path)``.
    """
    import glob as _glob
    from dotenv import dotenv_values as _dv
    if workspaces_dir is None:
        workspaces_dir = os.path.join(os.path.dirname(__file__), "workspaces")
    out = []
    for path in sorted(_glob.glob(os.path.join(workspaces_dir, "*.env"))):
        name = os.path.basename(path)[:-4]
        v = _dv(path)
        ws = v.get("BETTERCO_WORKSPACE_ID")
        if not ws:
            continue
        out.append({
            "name": name,
            "label": name.replace("-", " · "),
            "base_url": v.get("BETTERCO_BASE_URL", "https://editor.betterco.ai/bcapi"),
            "workspace_id": ws,
            "has_user_api": bool(v.get("BETTERCO_USER_EMAIL") and v.get("BETTERCO_USER_PASSWORD")),
        })
    return out
