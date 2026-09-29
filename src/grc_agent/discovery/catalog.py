"""What each kind of personal data is called, how risky it is, and which obligations it touches.

The categories are practical groupings for an inventory, not legal categories: the DPDP Act
2023 treats all digital personal data alike, with extra duties for children's data
(Section 9). Risk tiers reflect the likely harm of a breach, which drives how strong the
reasonable security safeguards of Section 8(5) need to be.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Kind:
    label: str
    category: str
    risk: str  # high | medium | low


KINDS: dict[str, Kind] = {
    "PERSON": Kind("Person's name", "Identity", "medium"),
    "EMAIL_ADDRESS": Kind("Email address", "Contact", "medium"),
    "PHONE_NUMBER": Kind("Phone number", "Contact", "medium"),
    "ADDRESS": Kind("Postal address or location", "Contact", "medium"),
    "PIN_CODE": Kind("PIN code", "Contact", "low"),
    "CITY": Kind("City, district or state", "Contact", "low"),
    "DATE_OF_BIRTH": Kind("Date of birth", "Demographic", "medium"),
    "AGE": Kind("Age", "Demographic", "low"),
    "GENDER": Kind("Gender", "Demographic", "low"),
    "NRP": Kind("Nationality, religion or political group", "Demographic", "high"),
    "IN_AADHAAR": Kind("Aadhaar number", "Government ID", "high"),
    "IN_PAN": Kind("PAN", "Government ID", "high"),
    "IN_PASSPORT": Kind("Passport number", "Government ID", "high"),
    "IN_VOTER": Kind("Voter ID (EPIC)", "Government ID", "high"),
    "IN_DRIVING_LICENCE": Kind("Driving licence number", "Government ID", "high"),
    "IN_VEHICLE_REGISTRATION": Kind("Vehicle registration", "Government ID", "medium"),
    "IN_GSTIN": Kind("GSTIN (can identify a proprietor)", "Business ID", "low"),
    "CREDIT_CARD": Kind("Card number", "Financial", "high"),
    "BANK_ACCOUNT": Kind("Bank account number", "Financial", "high"),
    "UPI_ID": Kind("UPI ID", "Financial", "high"),
    "FINANCIAL": Kind("Income, salary or credit data", "Financial", "high"),
    "HEALTH": Kind("Health or medical data", "Health", "high"),
    "BIOMETRIC": Kind("Biometric data", "Biometric", "high"),
    "IP_ADDRESS": Kind("IP address", "Online identifier", "medium"),
    "DEVICE_ID": Kind("Device or advertising ID", "Online identifier", "medium"),
    "GEOLOCATION": Kind("Precise location", "Online identifier", "medium"),
}

RISK_ORDER = {"high": 0, "medium": 1, "low": 2}

# Column-name hints. Words are matched against the column name split on non-letters,
# so "customer_email" and "EmailAddress" both hit "email".
NAME_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("IN_AADHAAR", ("aadhaar", "aadhar", "uidai")),
    ("IN_PAN", ("pan", "pan_no", "pan_number")),
    ("IN_PASSPORT", ("passport",)),
    ("IN_VOTER", ("voter", "epic")),
    ("IN_DRIVING_LICENCE", ("driving", "licence", "license", "dl_no")),
    ("IN_GSTIN", ("gstin", "gst", "gst_no")),
    ("EMAIL_ADDRESS", ("email", "e_mail", "mail")),
    ("PHONE_NUMBER", ("phone", "mobile", "contact_no", "msisdn", "whatsapp", "tel", "cell")),
    ("DATE_OF_BIRTH", ("dob", "date_of_birth", "birth_date", "birthdate", "birthday")),
    ("AGE", ("age",)),
    ("GENDER", ("gender", "sex")),
    ("NRP", ("religion", "caste", "nationality", "political")),
    ("PIN_CODE", ("pincode", "pin_code", "postal_code", "zip", "zipcode")),
    ("ADDRESS", ("address", "addr", "street", "locality", "landmark", "house")),
    ("CITY", ("city", "district", "town", "village")),
    ("CREDIT_CARD", ("card_number", "card_no", "cc_number")),
    ("BANK_ACCOUNT", ("account_number", "account_no", "acct_no", "bank_account", "ifsc")),
    ("UPI_ID", ("upi", "vpa")),
    ("FINANCIAL", ("salary", "income", "ctc", "credit_score", "cibil", "loan_amount")),
    (
        "HEALTH",
        (
            "diagnosis",
            "medical",
            "health",
            "prescription",
            "blood_group",
            "disease",
            "allergy",
            "treatment",
            "symptom",
            "medication",
        ),
    ),
    ("BIOMETRIC", ("fingerprint", "biometric", "face_id", "iris", "retina", "voiceprint")),
    ("IP_ADDRESS", ("ip", "ip_address", "ipaddr")),
    ("DEVICE_ID", ("device_id", "imei", "advertising_id", "gaid", "idfa", "android_id")),
    ("GEOLOCATION", ("latitude", "longitude", "lat", "lng", "lon", "geo", "gps")),
    (
        "PERSON",
        (
            "name",
            "full_name",
            "first_name",
            "last_name",
            "fname",
            "lname",
            "customer_name",
            "patient_name",
            "father_name",
            "mother_name",
            "guardian_name",
            "nominee",
        ),
    ),
)

# Words that make a "name" column clearly not about a person.
_NOT_PERSON = {"product", "company", "file", "user", "username", "host", "branch", "city", "item"}


def _tokens(column: str) -> tuple[list[str], str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1_\2", column).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", spaced) if w]
    return words, "_".join(words)


def hint_for(column: str) -> str | None:
    """The entity a column name suggests, if any."""
    words, joined = _tokens(column)
    for entity, keys in NAME_HINTS:
        for key in keys:
            if "_" in key:
                if key in joined:
                    return entity
            elif key in words:
                if entity == "PERSON" and _NOT_PERSON & set(words):
                    return None
                return entity
    return None


# Obligations (ids from the DPDPA register) that every inventory record touches, and
# the extra ones a record's category or legal basis brings in.
ALWAYS = ("OBL-001", "OBL-004", "OBL-005", "OBL-006")
CONSENT = ("OBL-002", "OBL-003")
CHILDREN = ("OBL-011",)

LEGAL_BASES = {
    "": "Not decided yet",
    "consent": "Consent (Section 6)",
    "legitimate_use": "A legitimate use (Section 7)",
}
PRINCIPALS = (
    "Customers",
    "Employees",
    "Job applicants",
    "Patients",
    "Students",
    "Children",
    "Vendor staff",
    "Website visitors",
    "Other",
)


def obligations_for(category: str, legal_basis: str, children: bool) -> tuple[str, ...]:
    ids = list(ALWAYS)
    if legal_basis in ("", "consent"):
        ids += CONSENT
    if children or category == "Children":
        ids += CHILDREN
    return tuple(ids)
