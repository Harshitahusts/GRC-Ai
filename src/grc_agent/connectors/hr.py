"""Importing employees for training: from a CSV export, or straight from Zoho People.

Every HR system (Zoho People, Keka, Darwinbox, greytHR, BambooHR...) can export employees
as a spreadsheet, so CSV is the route that works everywhere: the columns are matched by
name (Email, Name or First/Last name, Department, Manager, Employee ID). Zoho People can
also be read directly with an OAuth refresh token for a self-client.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from grc_agent.connectors.base import ConnectorError, check_host, expect_ok, request

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_ROWS = 5000

# Zoho data centres: accounts and People hosts.
ZOHO_DCS = {
    "in": "India (zoho.in)",
    "com": "US (zoho.com)",
    "eu": "Europe (zoho.eu)",
    "com.au": "Australia (zoho.com.au)",
    "jp": "Japan (zoho.jp)",
}


@dataclass(frozen=True)
class Person:
    name: str
    email: str
    department: str = ""
    manager: str = ""
    external_id: str = ""


def _norm(header: str) -> str:
    return re.sub(r"[^a-z]", "", header.lower())


_COLUMNS = {
    "email": ("email", "emailid", "emailaddress", "workemail", "officialemail", "officeemail"),
    "name": ("name", "fullname", "employeename", "displayname"),
    "first": ("firstname", "givenname"),
    "last": ("lastname", "surname", "familyname"),
    "department": ("department", "dept", "team", "division"),
    "manager": ("manager", "reportingto", "reportingmanager", "linemanager"),
    "external_id": ("employeeid", "empid", "employeenumber", "employeecode", "id"),
    "status": ("status", "employeestatus", "employmentstatus"),
}


def from_csv(data: bytes) -> tuple[list[Person], list[str]]:
    """People from an HR export, and the rows that were skipped (with why)."""
    text = data.decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ConnectorError("The file is empty.")
    found: dict[str, str] = {}
    for header in reader.fieldnames:
        key = _norm(header or "")
        for col, names in _COLUMNS.items():
            if key in names and col not in found:
                found[col] = header
    if "email" not in found:
        raise ConnectorError("No email column found. Add a column called Email.")
    if "name" not in found and "first" not in found:
        raise ConnectorError("No name column found. Add Name, or First name and Last name.")
    people, skipped, seen = [], [], set()
    for n, row in enumerate(reader, start=2):
        if n > MAX_ROWS + 1:
            skipped.append(f"Stopped after {MAX_ROWS} rows.")
            break

        def get(col: str, row: dict = row) -> str:
            return (row.get(found[col]) or "").strip() if col in found else ""

        email = get("email").lower()
        name = get("name") or " ".join(p for p in (get("first"), get("last")) if p)
        status = get("status").lower()
        if status and status not in ("active", "current", "confirmed", "probation", "1"):
            skipped.append(f"Row {n}: {status} (only active employees are imported)")
            continue
        if not EMAIL.match(email):
            skipped.append(f"Row {n}: no valid email")
            continue
        if email in seen:
            skipped.append(f"Row {n}: {email} appears twice")
            continue
        seen.add(email)
        people.append(
            Person(
                name=(name or email.split("@")[0])[:120],
                email=email[:200],
                department=get("department")[:120],
                manager=get("manager")[:120],
                external_id=get("external_id")[:60],
            )
        )
    return people, skipped


def zoho_people(dc: str, client_id: str, client_secret: str, refresh_token: str) -> list[Person]:
    """Active employees from Zoho People (OAuth self-client with ZOHOPEOPLE.forms.READ)."""
    if dc not in ZOHO_DCS:
        raise ConnectorError("Choose your Zoho data centre.")
    token_url = f"https://accounts.zoho.{dc}/oauth/v2/token"
    check_host(token_url, (f"accounts.zoho.{dc}",), "Zoho")
    resp = request(
        "POST",
        token_url,
        form={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        },
    )
    body = expect_ok(resp, "Zoho sign-in")
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise ConnectorError("Zoho didn't accept those credentials. Check the refresh token.")
    people: list[Person] = []
    start = 1
    while len(people) < MAX_ROWS:
        url = (
            f"https://people.zoho.{dc}/people/api/forms/employee/getRecords"
            f"?sIndex={start}&limit=200"
        )
        check_host(url, (f"people.zoho.{dc}",), "Zoho People")
        resp = request("GET", url, headers={"Authorization": f"Zoho-oauthtoken {token}"})
        body = expect_ok(resp, "Reading Zoho People")
        result = (body.get("response") or {}).get("result") if isinstance(body, dict) else None
        if not result:
            break
        for entry in result:
            for records in entry.values():
                for r in records:
                    people.extend(_zoho_person(r))
        if len(result) < 200:
            break
        start += 200
    return people


def _zoho_person(r: dict) -> list[Person]:
    status = str(r.get("Employeestatus", "Active")).lower()
    email = str(r.get("EmailID", "")).strip().lower()
    if status != "active" or not EMAIL.match(email):
        return []
    name = " ".join(p for p in (r.get("FirstName", ""), r.get("LastName", "")) if p).strip()
    manager = str(r.get("Reporting_To", "") or "")
    return [
        Person(
            name=(name or email.split("@")[0])[:120],
            email=email[:200],
            department=str(r.get("Department", "") or "")[:120],
            manager=manager[:120],
            external_id=str(r.get("EmployeeID", "") or "")[:60],
        )
    ]
