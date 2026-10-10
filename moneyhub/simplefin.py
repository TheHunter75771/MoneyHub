"""SimpleFIN Bridge sync connector.

Live sync is an accelerator, never a dependency: rows fetched here run
through the exact same dedup pipeline as file imports (fitid layer +
cross-source guard), and the monthly statement import remains the audit
that catches anything the aggregator drops or duplicates.

Secrets: the setup token and access URL live in the gitignored .env file
(SIMPLEFIN_SETUP_TOKEN / SIMPLEFIN_ACCESS_URL). They are never logged,
printed, or stored in the database.
"""
from __future__ import annotations

import base64
import re
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .importing import ParsedRow
from .models import Account
from .money import to_cents

SFIN_FITID_PREFIX = "sfin:"
# The bridge sits behind bot protection that 403s library-default agents.
USER_AGENT = "moneyhub/0.1 (+personal finance sync)"


class SimpleFinError(RuntimeError):
    pass


ENV_PREFIXES = ("SIMPLEFIN_", "NOTION_", "NTFY_")


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Minimal KEY=VALUE .env reader. Process environment wins on conflict."""
    import os

    values: dict[str, str] = {}
    env_path = Path(path)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    values.update(
        {k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIXES)}
    )
    return values


def claim_setup_token(setup_token: str) -> str:
    """One-time exchange: setup token -> access URL (with embedded basic auth)."""
    try:
        claim_url = base64.b64decode(setup_token.strip()).decode()
    except Exception as exc:
        raise SimpleFinError("SIMPLEFIN_SETUP_TOKEN is not valid base64") from exc
    if not claim_url.startswith("https://"):
        raise SimpleFinError("decoded claim URL is not https — refusing")
    request = urllib.request.Request(
        claim_url, method="POST", data=b"", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode().strip()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise SimpleFinError(
                "claim rejected (403): the setup token was already used or is invalid. "
                "Generate a fresh token at bridge.simplefin.org."
            ) from exc
        raise SimpleFinError(f"claim failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SimpleFinError(f"claim unreachable: {exc.reason}") from exc


_ACCESS_URL_LINE = re.compile(r"^\s*SIMPLEFIN_ACCESS_URL\s*=\s*\S", re.MULTILINE)


def persist_access_url(access_url: str, env_path: str | Path = ".env") -> None:
    """Append the claimed access URL to .env unless a real value is already set.

    Only an actual ``SIMPLEFIN_ACCESS_URL=<value>`` line counts: a comment
    that merely mentions the key, or an empty ``SIMPLEFIN_ACCESS_URL=``,
    must not suppress the write — a setup token is single-use, so a skipped
    save here silently burns it.
    """
    path = Path(env_path)
    existing = path.read_text() if path.exists() else ""
    if _ACCESS_URL_LINE.search(existing):
        return
    with path.open("a") as handle:
        handle.write(f"\nSIMPLEFIN_ACCESS_URL={access_url}\n")


def fetch_accounts(access_url: str, days: int) -> bytes:
    """GET /accounts with basic auth from the access URL. Returns raw body."""
    parts = urlsplit(access_url)
    if not parts.username:
        raise SimpleFinError("access URL has no embedded credentials")
    credentials = base64.b64encode(
        f"{parts.username}:{parts.password or ''}".encode()
    ).decode()
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    start = int((datetime.now(tz=UTC) - timedelta(days=days)).timestamp())
    url = urlunsplit(
        (parts.scheme, host, parts.path.rstrip("/") + "/accounts", f"start-date={start}", "")
    )
    request = urllib.request.Request(
        url, headers={"Authorization": f"Basic {credentials}", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SimpleFinError(f"/accounts failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:  # DNS, refused, TLS, timeout
        raise SimpleFinError(f"/accounts unreachable: {exc.reason}") from exc


def rows_from_account(raw: dict) -> list[ParsedRow]:
    """Posted transactions of one SimpleFIN account -> importer rows."""
    rows = []
    for txn in raw.get("transactions", []):
        if txn.get("pending"):
            continue  # pending ids are unstable at some institutions
        rows.append(
            ParsedRow(
                posted_date=datetime.fromtimestamp(txn["posted"], tz=UTC).date(),
                amount_cents=to_cents(txn["amount"]),
                description_raw=txn.get("description", "") or "",
                merchant=txn.get("payee", "") or "",
                fitid=f"{SFIN_FITID_PREFIX}{txn['id']}",
            )
        )
    return rows


# (institution substring, account-name substring) -> Money Hub account name.
AUTO_MAP = [
    ("service", "spending", "SCU Spending"),
    ("service", "everyday", "SCU Spending"),
    ("service", "bills", "SCU Bills"),
    ("service", "paycheck", "SCU Paycheck Holdings"),
    ("service", "primary", "SCU Paycheck Holdings"),
    ("service", "emergency", "SCU E-Fund"),
    ("service", "holiday", "SCU E-Fund"),
    ("service", "pc/misc", "SCU PC Savings"),
    ("service", "gift", "SCU PC Savings"),
    ("service", "apartment", "SCU Apartment Savings"),
    ("navy", "checking", "NFCU Checking"),
    ("navy", "savings", "NFCU Savings"),
]


def auto_map_account(session: Session, raw: dict) -> Account | None:
    """Map a SimpleFIN account to ours by (org, name) heuristics, once."""
    org = (raw.get("org") or {})
    org_name = f"{org.get('name', '')} {org.get('domain', '')}".lower()
    name = (raw.get("name") or "").lower()
    for org_sub, name_sub, target in AUTO_MAP:
        if org_sub in org_name and name_sub in name:
            account = session.scalars(
                select(Account).where(
                    Account.name == target, Account.simplefin_id.is_(None)
                )
            ).first()
            if account is not None:
                account.simplefin_id = raw["id"]
                return account
    return None
