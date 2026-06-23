#!/usr/bin/env python3
"""
BetterCo integration guide — local web app driving the Mandanten-Neuannahme flow.

A single HTML page (index.html) backed by this stdlib http.server, which holds the
authenticated BetterCo client and proxies every call (the browser never talks to
BetterCo directly). See README.md for the full flow and HTTP_REFERENCE.md for the
raw HTTP behind each step.

    python app.py                                  # default env, :8770
    python app.py --env-file workspaces/prod-eckhard-afileon.env
    python app.py --port 8771 --no-browser
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from betterco_client import BetterCoClient
from reference_flow import connect, _as_list, _parse_env_file

HERE = Path(__file__).parent
HTML_FILE = HERE / "index.html"
WORKSPACES_DIR = HERE / "workspaces"

# Credential fields captured by the in-app Zugangsdaten editor, in file order:
# (key, label, secret, default). REST (key+secret) drives the customer/case/
# process/document calls; the User-API (email+password) is required for registry
# search (step 1) and the enriched "Akte anlegen" (step 3). secret=True fields are
# masked on read and only overwritten when the user actually changes them.
ENV_FIELDS = [
    ("BETTERCO_BASE_URL",      "Base URL",        False, "https://editor.betterco.ai/bcapi"),
    ("BETTERCO_API_KEY",       "REST API Key",    False, ""),
    ("BETTERCO_API_SECRET",    "REST API Secret", True,  ""),
    ("BETTERCO_WORKSPACE_ID",  "Workspace ID",    False, ""),
    ("BETTERCO_ORG_ID",        "Advisor Org ID",  False, ""),
    ("BETTERCO_USER_EMAIL",    "User E-Mail",      False, ""),
    ("BETTERCO_USER_PASSWORD", "User Passwort",    True,  ""),
]
SECRET_MASK = "••••••••"   # shown for a stored secret

# WZ 2008 (Klassifikation der Wirtschaftszweige) code -> Bezeichnung, keyed by the
# numeric code without the leading section letter (e.g. "70.10.1"). Sourced from the
# Destatis WZ 2008 list. Used to describe a customer's segmentCodes.wz on hover.
try:
    WZ2008 = json.loads((HERE / "wz2008.json").read_text(encoding="utf-8"))
except Exception:  # noqa: BLE001
    WZ2008 = {}


def _wz_lookup(code: str) -> str | None:
    """Resolve one WZ code to its Bezeichnung, falling back to broader levels
    (subclass -> class -> group -> division) when the exact code isn't listed."""
    parts = code.split(".")
    for i in range(len(parts), 0, -1):
        hit = WZ2008.get(".".join(parts[:i]))
        if hit:
            return hit
    return None


def _wz_describe(wz: str) -> str:
    """A multi-line "code — Bezeichnung" description for a (possibly comma-joined)
    WZ string, for the WZ-Code hover. Codes with no match are shown bare."""
    out = []
    for raw in str(wz or "").split(","):
        code = raw.strip()
        if not code:
            continue
        desc = _wz_lookup(code)
        out.append(f"{code} — {desc}" if desc else code)
    return "\n".join(out)
ENV_HEADER = (
    "# BetterCo workspace credentials — written by the in-app Zugangsdaten editor.\n"
    "# REST (key+secret) drives customer/case/process/document calls; the User-API\n"
    "# (email+password) is required for registry search and the enriched 'Akte anlegen'.\n"
    "# Contains secrets and is git-ignored — never commit a real .env.\n\n"
)

# Flow sets added to the matter on "Akte anlegen" (step 3), by domain.
ENTITY_FLOWS = ["F1800_OnboardingEntity_A", "F1800_OnboardingEntity_E",
                "F18000_ReKYC", "F1600_RiskAMLScreening"]
PERSON_FLOWS = ["F1900_OnboardingIndividual_A", "F1900_OnboardingIndividual_E",
                "F19000_ReKYC", "F1600_RiskAMLScreening"]

_client = None          # set in main()
_org_id = None          # advisor org id (from the workspace env) — set in main()
_env_file = None        # path of the active workspace .env — set in main()/on save
_status = {}            # last verify_env() report (for the Zugangsdaten status line)
_client_lock = threading.Lock()


def _safe_env_path(name: str) -> Path:
    """Resolve a user-supplied env name to workspaces/<base>.env (no traversal)."""
    base = Path(name or "").name           # strip any directory part
    if not base.endswith(".env"):
        base += ".env"
    return WORKSPACES_DIR / base


def _build_client(values: dict) -> BetterCoClient:
    """Construct a client from explicit env values (same wiring as connect())."""
    return BetterCoClient(
        base_url=values.get("BETTERCO_BASE_URL"),
        api_key=values.get("BETTERCO_API_KEY"),
        api_secret=values.get("BETTERCO_API_SECRET"),
        workspace_id=values.get("BETTERCO_WORKSPACE_ID"),
        org_id=values.get("BETTERCO_ORG_ID"),
        user_email=values.get("BETTERCO_USER_EMAIL"),
        user_password=values.get("BETTERCO_USER_PASSWORD"),
    )


def _write_env_file(path: Path, values: dict) -> None:
    body = ENV_HEADER + "".join(f"{k}={values.get(k, '')}\n" for k, *_ in ENV_FIELDS)
    path.write_text(body, encoding="utf-8")


def _first(*vals):
    for v in vals:
        if v not in (None, ""):
            return v
    return None


def _risk_fields_rest(cu: dict) -> dict:
    """Extract KYC/risk fields from a REST getCustomerById object.

    Final bindings (all under amlProfile.riskProfile): kycNote, kycStatus,
    taxIndustry, taxIndustryRisk, riskCountry, aggregatedAmlRisk. wz from
    segmentCodes.wz. Top-level riskProfile (+ riskProfile.amlProfile) are
    fallbacks in case a profiled REST object nests them differently.
    """
    arp = (cu.get("amlProfile") or {}).get("riskProfile") or {}   # amlProfile.riskProfile
    rp = cu.get("riskProfile") or {}                              # top-level fallback
    rpa = rp.get("amlProfile") or {}                              # riskProfile.amlProfile
    seg = cu.get("segmentCodes") or {}
    wz = seg.get("wz")
    wz = ", ".join(wz) if isinstance(wz, list) else (wz or "")

    def f(key):
        return _first(arp.get(key), rp.get(key), rpa.get(key))
    return {
        "pruefnotiz": f("kycNote"),
        "kycStatus": f("kycStatus"),
        "wzCode": wz,
        "wzDescription": _wz_describe(wz),
        "taxIndustry": f("taxIndustry"),
        "industryRisk": f("taxIndustryRisk"),
        "countryRisk": f("riskCountry"),
        "amlRisk": f("aggregatedAmlRisk"),
    }


def _doc_meta(d: dict) -> dict:
    """List doc {id,displayName,url} -> add documentType + uploadDate from its detail.

    getCustomer/ProcessDocuments lists don't carry the type (null); the per-doc
    detail (the list `url`) has `type` and `uploadDate`. Call inside _client_lock.
    """
    out = {"id": d.get("id"), "displayName": d.get("displayName") or d.get("name"),
           "documentType": None, "uploadDate": None}
    url = d.get("url")
    if url:
        try:
            det = _client.session.get(url).json()
            out["documentType"] = det.get("type")
            out["uploadDate"] = det.get("uploadDate")
        except Exception:  # noqa: BLE001
            pass
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quieter console
        pass

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = HTML_FILE.read_text(encoding="utf-8").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._send_html()
        if parsed.path == "/api/env":
            return self._handle_env_get(parse_qs(parsed.query))
        if parsed.path == "/api/search":
            return self._handle_search(parse_qs(parsed.query))
        if parsed.path == "/api/document":
            return self._handle_document_download(parse_qs(parsed.query))
        if parsed.path == "/api/documents-zip":
            return self._handle_documents_zip(parse_qs(parsed.query))
        self._send_json({"error": "not found"}, 404)

    def _handle_documents_zip(self, qs: dict):
        """Download ALL docs of a customer (cid) or a process (cid+case_id+pid) as one ZIP.

        No bulk endpoint exists — fetch each doc (detail→downloadURI→bytes) and zip them.
        """
        import io
        import zipfile
        cid = (qs.get("cid") or [""])[0].strip()
        case_id = (qs.get("case_id") or [""])[0].strip()
        pid = (qs.get("pid") or [""])[0].strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        try:
            with _client_lock:
                if case_id and pid:
                    docs = _as_list(_client.list_process_documents(cid, case_id, pid))
                    zipname = f"prozess_dokumente_{pid}.zip"
                else:
                    docs = _as_list(_client.list_customer_documents(cid))
                    zipname = f"mandanten_dokumente_{cid}.zip"
                _client._ensure_auth()
                buf = io.BytesIO()
                added = 0
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    seen = {}
                    for d in docs:
                        url = d.get("url")
                        if not url:
                            continue
                        try:
                            det = _client.session.get(url).json()
                            dl = det.get("downloadURI")
                            fname = det.get("fileName") or d.get("displayName") or f"{d.get('id')}.pdf"
                            if not dl:
                                continue
                            r = _client.session.get(dl)
                            if not r.ok or "json" in r.headers.get("Content-Type", ""):
                                continue
                            # avoid duplicate names inside the zip
                            n = seen.get(fname, 0)
                            seen[fname] = n + 1
                            if n:
                                stem, _, ext = fname.rpartition(".")
                                fname = f"{stem}_{n}.{ext}" if stem else f"{fname}_{n}"
                            zf.writestr(fname, r.content)
                            added += 1
                        except Exception:  # noqa: BLE001
                            continue
                data = buf.getvalue()
            if not added:
                return self._send_json({"error": "keine Dokumente"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{zipname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def _handle_document_download(self, qs: dict):
        """Proxy a document download. Customer docs (cid+doc_id) or process docs
        (cid+case_id+pid+doc_id). The list `url` is the detail JSON whose
        `downloadURI` is the actual binary — fetch detail, then stream the binary.
        """
        cid = (qs.get("cid") or [""])[0].strip()
        doc_id = (qs.get("doc_id") or [""])[0].strip()
        case_id = (qs.get("case_id") or [""])[0].strip()
        pid = (qs.get("pid") or [""])[0].strip()
        if not (cid and doc_id):
            return self._send_json({"error": "cid, doc_id required"}, 400)
        try:
            with _client_lock:
                if case_id and pid:
                    docs = _as_list(_client.list_process_documents(cid, case_id, pid))
                else:
                    docs = _as_list(_client.list_customer_documents(cid))
                doc = next((d for d in docs if d.get("id") == doc_id), None)
                if not doc or not doc.get("url"):
                    return self._send_json({"error": "document not found"}, 404)
                _client._ensure_auth()
                detail = _client.session.get(doc["url"])
                detail.raise_for_status()
                dj = detail.json()
                dl = dj.get("downloadURI")
                fname = dj.get("fileName") or doc.get("displayName") or f"{doc_id}.pdf"
                if not dl:
                    return self._send_json({"error": "no downloadURI"}, 404)
                r = _client.session.get(dl, stream=True)
            if not r.ok:
                return self._send_json({"error": f"download failed ({r.status_code})"}, 502)
            ctype = r.headers.get("Content-Type", "application/pdf")
            if "json" in ctype:   # malware-scan / error body, not a file
                return self._send_json({"error": r.text[:200]}, 502)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.end_headers()
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    self.wfile.write(chunk)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send_json({"error": "invalid JSON body"}, 400)
        if parsed.path == "/api/env":
            return self._handle_env_save(body)
        if parsed.path == "/api/create-matter":
            return self._handle_create_matter(body)
        if parsed.path == "/api/process-link":
            return self._handle_process_link(body)
        if parsed.path == "/api/processes":
            return self._handle_processes(body)
        if parsed.path == "/api/process-detail":
            return self._handle_process_detail(body)
        if parsed.path == "/api/customer":
            return self._handle_customer(body)
        if parsed.path == "/api/documents":
            return self._handle_documents(body)
        if parsed.path == "/api/process-documents":
            return self._handle_process_documents(body)
        if parsed.path == "/api/customers-list":
            return self._handle_customers_list(body)
        if parsed.path == "/api/risk-profile":
            return self._handle_risk_profile(body)
        if parsed.path == "/api/customer-processes":
            return self._handle_customer_processes(body)
        self._send_json({"error": "not found"}, 404)

    # ── Zugangsdaten (workspace .env credential editor) ──────────────
    def _handle_env_get(self, qs: dict):
        """List workspace .env files and return one file's values (secrets masked).

        Secret fields (API_SECRET, USER_PASSWORD) never leave the server in clear —
        a stored secret reads back as SECRET_MASK; an unset one as "". For a brand-new
        (non-existent) file the non-secret defaults (e.g. Base URL) are prefilled.
        """
        active = Path(_env_file).name if _env_file else None
        requested = (qs.get("file") or [""])[0].strip()
        target = Path(requested).name if requested else active
        files = sorted(p.name for p in WORKSPACES_DIR.glob("*.env"))
        parsed = {}
        if target:
            path = WORKSPACES_DIR / target
            if path.exists():
                parsed = _parse_env_file(str(path))
        is_new = bool(target) and not parsed
        values, is_set = {}, {}
        for key, _label, secret, default in ENV_FIELDS:
            stored = parsed.get(key, "")
            is_set[key] = bool(stored)
            if secret and stored:
                values[key] = SECRET_MASK
            elif stored:
                values[key] = stored
            elif is_new and default:        # new file: prefill sensible defaults
                values[key] = default
            else:
                values[key] = ""
        return self._send_json({
            "files": files, "active": active, "file": target,
            "fields": [{"key": k, "label": l, "secret": s} for k, l, s, _ in ENV_FIELDS],
            "values": values, "isSet": is_set, "mask": SECRET_MASK, "status": _status,
        })

    def _handle_env_save(self, body: dict):
        """Write a workspace .env and, by default, activate it as the live client.

        An unchanged masked secret (== SECRET_MASK) keeps the value already on disk.
        Activation runs verify_env() and only swaps the global client if it reports ok;
        otherwise the file is still written but the running client is left untouched.
        """
        global _client, _org_id, _env_file, _status
        fname = (body.get("file") or "").strip()
        if not fname:
            return self._send_json({"error": "Dateiname erforderlich"}, 400)
        values_in = body.get("values") or {}
        activate = body.get("activate", True)
        path = _safe_env_path(fname)
        existing = _parse_env_file(str(path)) if path.exists() else {}
        out = {}
        for key, _label, secret, _default in ENV_FIELDS:
            v = (values_in.get(key) or "").strip()
            if secret and v == SECRET_MASK:        # unchanged masked secret -> keep stored
                v = existing.get(key, "")
            out[key] = v
        try:
            _write_env_file(path, out)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"Schreiben fehlgeschlagen: {exc}"}, 500)
        resp = {"saved": path.name, "activated": False}
        if activate:
            rep, client = {}, None
            try:
                client = _build_client(out)
                rep = client.verify_env()
            except Exception as exc:  # noqa: BLE001
                resp["error"] = f"Verbindung fehlgeschlagen: {exc}"
            resp["status"] = rep
            if rep.get("ok"):
                with _client_lock:
                    _client = client
                    _org_id = out.get("BETTERCO_ORG_ID")
                    _env_file = str(path)
                    _status = rep
                resp["activated"] = True
            elif "error" not in resp:
                resp["error"] = "Gespeichert, aber Verbindung nicht ok — siehe Status."
        return self._send_json(resp)

    def _handle_customer_processes(self, body: dict):
        """Process map {cid: [{flow,label,status}]} for the overview process filter.

        Two-phase + concurrent (collect refs, then fetch details in one big pool) —
        much faster than per-customer sequential fetching.
        """
        limit = body.get("limit")
        try:
            custs = _as_list(_client.list_customers())
            if limit:
                custs = custs[: int(limit)]
            cids = [c.get("id") for c in custs if c.get("id")]

            # Phase 1: collect refs for ONLY the ReKYC + Screening processes (identified
            # from the list displayName, no detail call) — the only two the filter offers.
            def refs_for(cid):
                refs = []
                try:
                    for case in _as_list(_client.list_cases(cid)):
                        for p in _as_list(_client.list_processes(cid, case.get("id"))):
                            di = p.get("displayInformation") or {}
                            label = ((di.get("processName") or {}).get("deValue")) or p.get("displayName")
                            low = (p.get("displayName") or "").lower().replace(" ", "")
                            if "rekyc" not in low and "screening" not in low:
                                continue
                            refs.append({"cid": cid, "url": p.get("url"), "label": label})
                except Exception:  # noqa: BLE001
                    pass
                return refs
            with ThreadPoolExecutor(max_workers=16) as ex:
                all_refs = [r for lst in ex.map(refs_for, cids) for r in lst]

            # Phase 2: fetch each process detail (status + flow code) in one big pool
            def detail(ref):
                try:
                    det = _client.session.get(ref["url"]).json() if ref.get("url") else {}
                    ref["flow"] = det.get("name")
                    ref["status"] = det.get("status")
                except Exception:  # noqa: BLE001
                    ref["flow"] = ref["status"] = None
                return ref
            with ThreadPoolExecutor(max_workers=16) as ex:
                all_refs = list(ex.map(detail, all_refs))

            pmap = {}
            for r in all_refs:
                pmap.setdefault(r["cid"], []).append(
                    {"flow": r.get("flow"), "label": r.get("label"), "status": r.get("status")})
            return self._send_json({"processes": pmap})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_risk_profile(self, body: dict):
        """Risiko-Profil for one customer via REST getCustomerById.

        Bindings (amlProfile.riskProfile.*): kycNote, kycStatus, taxIndustry,
        taxIndustryRisk, riskCountry, aggregatedAmlRisk; wz from segmentCodes.wz.
        """
        cid = (body.get("cid") or "").strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        try:
            with _client_lock:
                cu = _client.get_customer(cid)
            f = _risk_fields_rest(cu)
            return self._send_json({
                "pruefnotiz": f["pruefnotiz"],
                "kycStatus": f["kycStatus"],
                "wzCode": f["wzCode"],
                "taxIndustry": f["taxIndustry"],
                "derivedIndustryRisk": f["industryRisk"],
                "derivedCountryRisk": f["countryRisk"],
                "amlRisk": f["amlRisk"],
            })
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_customers_list(self, body: dict):
        """Workspace customer overview with KYC/risk fields — REST getCustomerById.

        All bindings come from the REST customer object (see _risk_fields_rest):
        kycStatus=additionalData[import].kycStatus, wzCode=segmentCodes.wz,
        taxIndustry/industryRisk/countryRisk=riskProfile.{taxIndustry,taxIndustryRisk,
        riskCountry}. amlRisk binding TBD.
        """
        limit = body.get("limit")
        try:
            custs = _as_list(_client.list_customers())
            if limit:
                custs = custs[: int(limit)]
            items = [(c.get("id"), c.get("displayName")) for c in custs if c.get("id")]

            def build(item):
                cid, dn = item
                try:
                    cu = _client.get_customer(cid)       # REST
                except Exception:  # noqa: BLE001
                    return {"id": cid, "name": dn, "error": True, "processes": []}
                f = _risk_fields_rest(cu)
                return {
                    "id": cid,
                    "name": (cu.get("legalInfo") or {}).get("legalName") or dn,
                    "type": cu.get("type"),   # INDIVIDUAL = Person, else Unternehmen
                    "kycStatus": f["kycStatus"], "wzCode": f["wzCode"],
                    "wzDescription": f["wzDescription"],
                    "taxIndustry": f["taxIndustry"], "industryRisk": f["industryRisk"],
                    "countryRisk": f["countryRisk"], "amlRisk": f["amlRisk"],
                    "pruefnotiz": f["pruefnotiz"],
                }

            # REST get_customer per customer — concurrent (process data is loaded
            # separately/lazily via /api/customer-processes to keep this fast)
            with ThreadPoolExecutor(max_workers=16) as ex:
                rows = list(ex.map(build, items))
            rows.sort(key=lambda r: (r.get("name") or "").lower())
            return self._send_json({"total": len(rows), "customers": rows})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_process_documents(self, body: dict):
        """getProcessDocuments — process-level docs (Sonstige Dokumente)."""
        cid = (body.get("cid") or "").strip()
        case_id = (body.get("case_id") or "").strip()
        pid = (body.get("pid") or "").strip()
        if not (cid and case_id and pid):
            return self._send_json({"error": "cid, case_id, pid required"}, 400)
        try:
            with _client_lock:
                docs = _as_list(_client.list_process_documents(cid, case_id, pid))
                out = [_doc_meta(d) for d in docs]
            return self._send_json({"pid": pid, "documents": out})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_customer(self, body: dict):
        """getCustomerById — full customer master data for the Kundendaten view."""
        cid = (body.get("cid") or "").strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        try:
            with _client_lock:
                cust = _client.get_customer(cid)
            return self._send_json({"customer": cust})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_documents(self, body: dict):
        """getCustomerDocuments — list (id/displayName); download via /api/document proxy."""
        cid = (body.get("cid") or "").strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        try:
            with _client_lock:
                docs = _as_list(_client.list_customer_documents(cid))
                out = [_doc_meta(d) for d in docs]
            return self._send_json({"cid": cid, "documents": out})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_process_detail(self, body: dict):
        """Read-only detail of one process (for non-shareable/internal flows)."""
        cid = (body.get("cid") or "").strip()
        case_id = (body.get("case_id") or "").strip()
        pid = (body.get("pid") or "").strip()
        if not (cid and case_id and pid):
            return self._send_json({"error": "cid, case_id, pid required"}, 400)
        try:
            with _client_lock:
                d = _client.get_process(cid, case_id, pid)
            di = d.get("displayInformation") or {}
            label = (((di.get("processName") or {}).get("deValue"))
                     or ((di.get("displayName") or {}).get("deValue")) or d.get("name"))
            tasks = [{"taskSpec": t.get("taskSpec"), "status": t.get("status")}
                     for t in (d.get("tasks") or [])]
            return self._send_json({
                "label": label, "flow": d.get("name"), "status": d.get("status"),
                "total": d.get("totalTasks"), "completed": d.get("numberOfCompletedTasks"),
                "shareable": d.get("shareable"), "tasks": tasks,
            })
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_processes(self, body: dict):
        """Enriched process list for the picker (getProcesses + per-process getProcess).

        Returns, per process: flow code, German label, status, task progress, and the
        `shareable` flag (true = has a client runner that can be iframed).
        """
        cid = (body.get("cid") or "").strip()
        case_id = (body.get("case_id") or "").strip()
        if not (cid and case_id):
            return self._send_json({"error": "cid, case_id required"}, 400)
        try:
            with _client_lock:
                rows = _as_list(_client.list_processes(cid, case_id))
                out = []
                for p in rows:
                    pid = p.get("id")
                    try:
                        d = _client.get_process(cid, case_id, pid)
                    except Exception:  # noqa: BLE001
                        d = {}
                    di = d.get("displayInformation") or p.get("displayInformation") or {}
                    label = (((di.get("processName") or {}).get("deValue"))
                             or ((di.get("displayName") or {}).get("deValue"))
                             or p.get("displayName") or d.get("name"))
                    out.append({
                        "id": pid,
                        "flow": d.get("name"),
                        "label": label,
                        "status": d.get("status"),
                        "total": d.get("totalTasks"),
                        "completed": d.get("numberOfCompletedTasks"),
                        "open": d.get("numberOfOpenTasks"),
                        "shareable": d.get("shareable"),
                    })
            return self._send_json({"cid": cid, "case_id": case_id, "processes": out})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_process_link(self, body: dict):
        """Mint a client-runner share link for one process (for the iframe picker)."""
        cid = (body.get("cid") or "").strip()
        case_id = (body.get("case_id") or "").strip()
        pid = (body.get("pid") or "").strip()
        if not (cid and case_id and pid):
            return self._send_json({"error": "cid, case_id, pid required"}, 400)
        try:
            with _client_lock:
                # initiator side = the client/advisor runner (not the target/customer-fill view)
                url = _client.create_share_link(cid, case_id, pid, is_initiator=True)
            return self._send_json({"share_url": url})
        except Exception as exc:  # noqa: BLE001 — e.g. screening has no client runner
            return self._send_json({"share_url": None, "error": str(exc)})

    def _handle_create_matter(self, body: dict):
        """Step 3: enriched client (NorthData) -> matter (auto-case) -> 3 domain flows.

        Enrichment requires creating with the NorthData externalRegistryId via the
        registry path (POST /api/customers) + the correct advisor org — that pulls
        ND + company.info (contacts/Verflechtungen/HR). A bare REST create would NOT
        enrich. Case/process creation then go via REST (customer is REST-visible).
        """
        name = (body.get("name") or "").strip()
        domain = (body.get("domain") or "ENTITY").strip().upper()
        ext_id = (body.get("externalRegistryId") or "").strip()
        if not name:
            return self._send_json({"error": "name required"}, 400)
        if not ext_id:
            return self._send_json({"error": "externalRegistryId required (für Anreicherung)"}, 400)
        is_person = domain == "PERSON"
        flows = PERSON_FLOWS if is_person else ENTITY_FLOWS
        category = "INDIVIDUAL" if is_person else "ENTITY"
        try:
            with _client_lock:
                # 1) enriched client via NorthData registry path
                payload = {
                    "clientActorExternalId": ext_id,
                    "advisorActorId": _org_id,
                    "customerCategoryType": category,
                    "clientActorName": name,
                    "domain": domain,
                    "purchaseDocuments": False,
                }
                r = requests.post(_client.base_url + "/api/customers", json=payload,
                                  headers=_client._user_headers(), verify=_client.session.verify)
                r.raise_for_status()
                cid = r.json()["businessRelationId"]
                # poll until NorthData/company.info enrichment finished
                for _ in range(30):
                    rr = requests.get(_client.base_url + "/api/customers/business-relation",
                                      params={"businessRelationId": cid},
                                      headers=_client._user_headers(), verify=_client.session.verify)
                    if rr.ok and rr.json().get("isFullyInitialized"):
                        break
                    time.sleep(2)
                # 2) matter = the auto-created case
                cases = _as_list(_client.list_cases(cid))
                case_id = cases[0]["id"] if cases else _client.create_case(cid, name)
                # 3) processes (flows) via REST
                processes = []
                for flow in flows:
                    proc = _client.create_process(cid, case_id, flow)
                    processes.append({"flow": flow, "id": proc.get("id")})
                contact_count = len(_as_list(_client.list_contacts(cid)))
                try:
                    document_count = len(_as_list(_client.list_customer_documents(cid)))
                except Exception:  # noqa: BLE001 — REST docs endpoint may 404 when empty
                    document_count = 0
                share_url = share_error = None
                try:
                    share_url = _client.create_share_link(cid, case_id, processes[0]["id"])
                except Exception as exc:  # noqa: BLE001
                    share_error = str(exc)
            return self._send_json({
                "businessRelationId": cid, "case_id": case_id, "domain": domain,
                "contact_count": contact_count, "document_count": document_count,
                "processes": processes,
                "share_url": share_url, "share_error": share_error,
            })
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_search(self, qs: dict):
        query = (qs.get("q") or [""])[0].strip()
        domain = (qs.get("domain") or ["ENTITY"])[0].strip() or "ENTITY"
        if not query:
            return self._send_json({"error": "query required", "hits": []}, 400)
        try:
            with _client_lock:
                hits = _client.search_registry(query, domain=domain)
            # normalize a display subset, but keep the full raw hit for the Stammdaten panel
            results = []
            for h in hits:
                # ENTITY hits carry legalName; PERSON hits carry firstName/lastName
                name = (h.get("legalName") or h.get("name")
                        or " ".join(p for p in (h.get("firstName"), h.get("lastName")) if p).strip())
                # register line for entities (registerId + court); birthDate for persons
                if h.get("registerId"):
                    sub = " · ".join(p for p in (h.get("legalType"), h.get("registerId"),
                                                 h.get("registerCity")) if p)
                elif h.get("birthDate"):
                    sub = f"geb. {h.get('birthDate')}"
                else:
                    sub = ""
                results.append({
                    "externalRegistryId": h.get("externalRegistryId"),
                    "name": name,
                    "address": h.get("address") or h.get("registerInfo") or "",
                    "subline": sub,
                    "raw": h,
                })
            return self._send_json({"query": query, "domain": domain,
                                    "count": len(results), "hits": results})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc), "hits": []}, 500)


def main() -> None:
    global _client, _org_id, _env_file, _status
    ap = argparse.ArgumentParser(description="BetterCo PoC search widget server")
    ap.add_argument("--env-file", default="workspaces/editor-betterco-claude.env",
                    help="Workspace .env to authenticate with")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    _env_file = args.env_file
    # Don't hard-fail on a missing/invalid env: boot anyway so the Zugangsdaten
    # editor can capture credentials for a fresh workspace. API calls return errors
    # until a valid env is saved (which swaps in the live client).
    try:
        _org_id = _parse_env_file(args.env_file).get("BETTERCO_ORG_ID")
        _client = connect(args.env_file)
        _status = _client.verify_env()
    except (SystemExit, Exception) as exc:  # noqa: BLE001
        print(f"!! Env '{args.env_file}' not ready ({exc}). "
              f"Open the app and set credentials under 'Zugangsdaten'.")
    url = f"http://localhost:{args.port}"
    print(f"BetterCo PoC search widget -> {url}  (env: {args.env_file})")
    if not args.no_browser:
        webbrowser.open(url)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
