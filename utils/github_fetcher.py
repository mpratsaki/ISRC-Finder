"""
utils/github_fetcher.py

Fetches the private ground-truth IPI LIST Excel file from a private GitHub
repository via the Contents API, builds a nickname/legal-name lookup
dictionary from it, and provides IPI metadata-health scanning.
"""

import base64
import io
import re
import urllib.parse
from decimal import Decimal, InvalidOperation

import openpyxl
import requests
import streamlit as st

# Private GitHub IPI LIST source. The Excel file must live in a private
# repository. Store the read-only token and file location in Streamlit secrets.
GITHUB_API_VERSION = "2022-11-28"
GITHUB_CONTENTS_TIMEOUT_SECONDS = 20
IPI_LIST_CACHE_TTL_SECONDS = 15 * 60


def get_private_ipi_config():
    """
    Reads the private GitHub source for the IPI LIST ground-truth Excel.

    Required Streamlit secrets:
      IPI_GITHUB_OWNER  -> GitHub user/org that owns the private repository
      IPI_GITHUB_REPO   -> private repository name
      IPI_GITHUB_PATH   -> path to the xlsx file inside the repository
      IPI_GITHUB_TOKEN  -> fine-grained PAT with Contents: Read-only on that repo

    Optional Streamlit secret:
      IPI_GITHUB_REF    -> branch, tag, or commit SHA. Defaults to "main".
    """
    required_keys = [
        "IPI_GITHUB_OWNER",
        "IPI_GITHUB_REPO",
        "IPI_GITHUB_PATH",
        "IPI_GITHUB_TOKEN",
    ]
    missing = [key for key in required_keys if not str(st.secrets.get(key, "")).strip()]
    if missing:
        raise RuntimeError("Λείπουν Streamlit secrets για το IPI LIST: " + ", ".join(missing))

    return {
        "owner": str(st.secrets["IPI_GITHUB_OWNER"]).strip(),
        "repo": str(st.secrets["IPI_GITHUB_REPO"]).strip(),
        "path": str(st.secrets["IPI_GITHUB_PATH"]).strip(),
        "ref": str(st.secrets.get("IPI_GITHUB_REF", "main")).strip() or "main",
        "token": str(st.secrets["IPI_GITHUB_TOKEN"]).strip(),
    }


def _github_contents_api_url(owner, repo, path):
    encoded_path = "/".join(
        urllib.parse.quote(part, safe="")
        for part in str(path).strip("/").split("/")
        if part
    )
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}"


@st.cache_data(ttl=IPI_LIST_CACHE_TTL_SECONDS, show_spinner=False)
def fetch_private_ipi_list_bytes(owner, repo, path, ref, token):
    """
    Fetches the private IPI LIST Excel from GitHub using the repository contents
    API. The token must be stored in Streamlit secrets, not in the repository.
    """
    url = _github_contents_api_url(owner, repo, path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "stay-independent-catalog-generator",
    }

    response = requests.get(
        url,
        headers=headers,
        params={"ref": ref},
        timeout=GITHUB_CONTENTS_TIMEOUT_SECONDS,
    )

    if response.status_code in (401, 403):
        raise RuntimeError("Το GitHub token δεν έχει πρόσβαση στο ιδιωτικό IPI LIST repo.")
    if response.status_code == 404:
        raise RuntimeError("Δεν βρέθηκε το IPI LIST αρχείο στο ιδιωτικό GitHub repo.")

    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" in content_type:
        # Defensive fallback if GitHub returns the default JSON representation
        # instead of raw bytes. The 'content' field is Base64 encoded.
        data = response.json()
        encoded_content = str(data.get("content") or "").replace("\n", "")
        if not encoded_content:
            raise RuntimeError("Το GitHub API δεν επέστρεψε περιεχόμενο για το IPI LIST.")
        file_bytes = base64.b64decode(encoded_content)
    else:
        file_bytes = response.content

    if not file_bytes:
        raise RuntimeError("Το IPI LIST αρχείο είναι κενό.")

    # .xlsx files are ZIP containers and normally start with PK. This catches
    # accidental HTML/JSON error payloads before openpyxl tries to parse them.
    if not file_bytes.startswith(b"PK"):
        raise RuntimeError("Το αρχείο που φορτώθηκε από GitHub δεν φαίνεται να είναι έγκυρο .xlsx.")

    return file_bytes


# --------------------------------------------------------------------------
# IPI LIST lookup helpers
# --------------------------------------------------------------------------
def _clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _lookup_key(value):
    """Case-insensitive lookup key matching nickname.strip().lower()."""
    text = _clean_text(value)
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def _parse_ipi(value):
    """Return IPI as an int where possible, so Excel stores it as a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else int(value)

    text = str(value).strip()
    if not text:
        return None

    compact = text.replace(" ", "").replace(",", "")
    try:
        number = Decimal(compact)
        if number == number.to_integral_value():
            return int(number)
    except (InvalidOperation, ValueError):
        pass

    digits_only = re.sub(r"\D+", "", text)
    if digits_only:
        return int(digits_only)

    return None


@st.cache_data(show_spinner=False)
def build_ipi_lookup_from_bytes(file_bytes):
    """
    Builds:
      {
        nickname.strip().lower(): {"legal": LEGAL NAME, "ipi": IPI, "pro": PRO},
        legal_name.strip().lower(): {"legal": LEGAL NAME, "ipi": IPI, "pro": PRO},
      }
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    try:
        if "IPI LIST" not in wb.sheetnames:
            raise ValueError("Το αρχείο δεν περιέχει sheet με όνομα 'IPI LIST'.")

        ws = wb["IPI LIST"]
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header_row:
            raise ValueError("Το sheet 'IPI LIST' δεν έχει headers.")

        header_map = {
            str(header).strip().upper(): idx
            for idx, header in enumerate(header_row)
            if header is not None and str(header).strip()
        }
        required_headers = ["NICKNAME", "LEGAL NAME", "IPI", "PRO"]
        missing_headers = [h for h in required_headers if h not in header_map]
        if missing_headers:
            raise ValueError("Λείπουν headers από το IPI LIST: " + ", ".join(missing_headers))

        def get_cell(row, header_name):
            idx = header_map[header_name]
            return row[idx] if idx < len(row) else None

        lookup = {}
        source_row_count = 0

        for row in ws.iter_rows(min_row=2, values_only=True):
            nickname = _clean_text(get_cell(row, "NICKNAME"))
            legal_name = _clean_text(get_cell(row, "LEGAL NAME"))
            ipi = _parse_ipi(get_cell(row, "IPI"))
            pro = _clean_text(get_cell(row, "PRO"))

            if not nickname and not legal_name:
                continue

            entry = {
                "legal": legal_name or nickname,
                "ipi": ipi,
                "pro": pro,
            }

            nickname_key = _lookup_key(nickname)
            legal_key = _lookup_key(legal_name)

            if nickname_key and nickname_key not in lookup:
                lookup[nickname_key] = entry
            if legal_key and legal_key not in lookup:
                lookup[legal_key] = entry

            source_row_count += 1

        return lookup, source_row_count

    finally:
        wb.close()


def _contributor_row(name, ipi_lookup):
    raw_name = _clean_text(name)
    match = ipi_lookup.get(_lookup_key(raw_name)) if ipi_lookup else None
    if match:
        return {
            "raw": raw_name,
            "writer": match.get("legal") or raw_name,
            "ipi": match.get("ipi"),
            "pro": match.get("pro") or "",
            "matched": True,
        }

    return {
        "raw": raw_name,
        "writer": raw_name,
        "ipi": None,
        "pro": "",
        "matched": False,
    }


def _build_contributor_rows(names, ipi_lookup):
    rows = []
    seen = set()

    for name in names:
        if not _clean_text(name):
            continue

        row = _contributor_row(name, ipi_lookup)
        key = _lookup_key(row["writer"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    return rows


def _scan_ipi_health(ipi_lookup):
    """
    Deduplicates the IPI lookup by legal name and reports entries that are
    missing an IPI number or a PRO affiliation. Returns (total, problems).
    """
    seen = set()
    total = 0
    problems = []
    for entry in ipi_lookup.values():
        legal = (entry.get("legal") or "").strip()
        key = legal.lower()
        if not legal or key in seen:
            continue
        seen.add(key)
        total += 1

        missing = []
        if entry.get("ipi") is None:
            missing.append("IPI")
        if not (entry.get("pro") or "").strip():
            missing.append("PRO")

        if missing:
            problems.append({
                "Writer": legal,
                "IPI": entry.get("ipi") if entry.get("ipi") is not None else "—",
                "PRO": entry.get("pro") or "—",
                "Πρόβλημα": ", ".join(f"Missing {m}" for m in missing),
            })
    return total, problems
