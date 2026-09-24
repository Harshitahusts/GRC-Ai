"""Cloud connectors: AWS, Google Cloud, Microsoft Azure.

Read-only checks with two purposes:
- Where personal data is stored, as evidence for cross-border transfers
  (Section 16). Findings flag storage outside Indian regions, and the app
  compares that with the client's intake answers.
- Basic security posture, as evidence for reasonable security safeguards
  (Section 8(5)): public access, MFA, audit logging.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from grc_agent.connectors.base import (
    SECURITY,
    Check,
    ConnectorError,
    expect_ok,
    location_check,
    request,
    sign_rs256_jwt,
)

AWS_INDIA = {"ap-south-1", "ap-south-2"}
GCP_INDIA = {"asia-south1", "asia-south2"}
AZURE_INDIA = {"centralindia", "southindia", "westindia", "jioindiawest", "jioindiacentral"}

# ---- AWS


ROLE_ARN = re.compile(r"arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,512}")


def firm_session() -> Any:
    """The firm's own AWS credentials, from the standard AWS lookup on this machine
    (environment variables, ~/.aws, or GRC_AWS_PROFILE). They're only used to assume
    clients' read-only roles."""
    import boto3

    return boto3.session.Session(profile_name=os.getenv("GRC_AWS_PROFILE") or None)


def firm_account_id() -> str:
    ident, err = _aws_call(firm_session().client("sts").get_caller_identity)
    if err:
        raise ConnectorError(f"The firm's AWS credentials don't work ({err}).")
    return ident["Account"]


def _session_from(credentials: dict, region: str) -> Any:
    import boto3

    return boto3.session.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


def _aws_session(config: dict, secrets: dict) -> Any:
    """Temporary credentials (15 minutes) for the client's read-only role."""
    role_arn = (config.get("role_arn") or "").strip()
    if not ROLE_ARN.fullmatch(role_arn):
        raise ConnectorError(
            "Enter the role ARN from the client, like arn:aws:iam::123456789012:role/…"
        )
    result, err = _aws_call(
        firm_session().client("sts").assume_role,
        RoleArn=role_arn,
        RoleSessionName="grc-agent",
        ExternalId=config["external_id"],
        DurationSeconds=900,
    )
    if err:
        hint = {
            "AccessDenied": "Check the role trusts this firm's AWS account with this external ID.",
        }.get(err, "")
        raise ConnectorError(f"Couldn't assume the client's role ({err}). {hint}".strip())
    return _session_from(result["Credentials"], config.get("region") or "ap-south-1")


def cloudformation_template(firm_account: str, external_id: str) -> str:
    """What the client runs in CloudFormation: a read-only role only this firm can use."""
    return f"""AWSTemplateFormatVersion: "2010-09-09"
Description: >-
  Read-only access for the GRC agent (AWS managed policy SecurityAudit).
  Only AWS account {firm_account} can use it, and only with the external ID below.
  Delete this stack to remove the access.
Resources:
  GrcAgentReadOnlyRole:
    Type: AWS::IAM::Role
    Properties:
      Description: Read-only access for the GRC agent's DPDPA assessment
      MaxSessionDuration: 3600
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              AWS: arn:aws:iam::{firm_account}:root
            Action: sts:AssumeRole
            Condition:
              StringEquals:
                sts:ExternalId: "{external_id}"
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/SecurityAudit
Outputs:
  RoleArn:
    Description: Send this to your consultant
    Value: !GetAtt GrcAgentReadOnlyRole.Arn
"""


def _aws_call(fn, *args, **kwargs) -> tuple[Any, str | None]:
    """Run a boto3 call; return (result, None) or (None, AWS error code)."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        return fn(*args, **kwargs), None
    except ClientError as exc:
        return None, exc.response.get("Error", {}).get("Code", "Error")
    except BotoCoreError as exc:
        raise ConnectorError(f"AWS: {exc.__class__.__name__}") from None


def aws_test(config: dict, secrets: dict, session: Any = None) -> str:
    session = session or _aws_session(config, secrets)
    ident, err = _aws_call(session.client("sts").get_caller_identity)
    if err:
        raise ConnectorError(f"AWS rejected the credentials ({err}).")
    return f"Connected to AWS account {ident['Account']}"


def aws_collect(config: dict, secrets: dict, session: Any = None) -> list[Check]:
    session = session or _aws_session(config, secrets)
    ident, err = _aws_call(session.client("sts").get_caller_identity)
    if err:
        raise ConnectorError(f"AWS rejected the credentials ({err}).")
    account = ident["Account"]
    iam, checks = session.client("iam"), []

    summary, err = _aws_call(iam.get_account_summary)
    if summary:
        on = summary["SummaryMap"].get("AccountMFAEnabled") == 1
        checks.append(
            Check(
                "root_mfa",
                "MFA on the root account",
                "pass" if on else "fail",
                "Enabled." if on else "The root account has no MFA.",
                SECURITY,
            )
        )
    else:
        checks.append(_denied("root_mfa", "MFA on the root account", err))

    policy, err = _aws_call(iam.get_account_password_policy)
    if policy:
        checks.append(
            Check(
                "password_policy",
                "Password policy",
                "pass",
                "An account password policy is set.",
                SECURITY,
            )
        )
    elif err == "NoSuchEntity":
        checks.append(
            Check(
                "password_policy",
                "Password policy",
                "fail",
                "No account password policy.",
                SECURITY,
            )
        )
    else:
        checks.append(_denied("password_policy", "Password policy", err))

    trails, err = _aws_call(session.client("cloudtrail").describe_trails)
    if trails is not None:
        trail_list = trails.get("trailList", [])
        multi = any(t.get("IsMultiRegionTrail") for t in trail_list)
        status = "pass" if multi else ("warn" if trail_list else "fail")
        detail = (
            "A multi-region CloudTrail trail records activity."
            if multi
            else "Trails exist but none covers all regions."
            if trail_list
            else "No CloudTrail trail: activity isn't being logged."
        )
        checks.append(
            Check("audit_logging", "Audit logging (CloudTrail)", status, detail, SECURITY)
        )
    else:
        checks.append(_denied("audit_logging", "Audit logging (CloudTrail)", err))

    block, err = _aws_call(session.client("s3control").get_public_access_block, AccountId=account)
    if block:
        cfg = block["PublicAccessBlockConfiguration"]
        full = all(
            cfg.get(k)
            for k in (
                "BlockPublicAcls",
                "IgnorePublicAcls",
                "BlockPublicPolicy",
                "RestrictPublicBuckets",
            )
        )
        checks.append(
            Check(
                "public_access",
                "S3 public access block",
                "pass" if full else "warn",
                "Blocked for the whole account." if full else "Only partly blocked.",
                SECURITY,
            )
        )
    elif err == "NoSuchPublicAccessBlockConfiguration":
        checks.append(
            Check(
                "public_access",
                "S3 public access block",
                "warn",
                "Not set at account level: buckets could be made public.",
                SECURITY,
            )
        )
    else:
        checks.append(_denied("public_access", "S3 public access block", err))

    s3 = session.client("s3")
    buckets, err = _aws_call(s3.list_buckets)
    if buckets is None:
        checks.append(_denied("data_location", "Where data is stored", err))
    else:
        locations: dict[str, list[str]] = {}
        for b in buckets.get("Buckets", [])[:100]:
            loc, _ = _aws_call(s3.get_bucket_location, Bucket=b["Name"])
            region = (loc or {}).get("LocationConstraint") or "us-east-1"
            locations.setdefault(region, []).append(b["Name"])
        checks.append(location_check("AWS S3", locations, lambda r: r in AWS_INDIA))
    return checks


def _denied(key: str, title: str, code: str | None) -> Check:
    return Check(key, title, "info", f"Couldn't check ({code}). Give the credentials read access.")


# ---- Google Cloud (service account key, JWT bearer flow)

GCP_TOKEN_URI = "https://oauth2.googleapis.com/token"
GCP_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"


def _gcp_key(secrets: dict) -> dict:
    try:
        key = json.loads(secrets["service_account_json"])
        key["client_email"], key["private_key"]  # noqa: B018 (presence check)
    except (json.JSONDecodeError, KeyError, TypeError):
        raise ConnectorError("Paste the whole service account key JSON file.") from None
    return key


def gcp_jwt(key: dict, now: int | None = None) -> str:
    now = now or int(time.time())
    claims = {
        "iss": key["client_email"],
        "scope": GCP_SCOPE,
        "aud": GCP_TOKEN_URI,
        "iat": now,
        "exp": now + 3600,
    }
    return sign_rs256_jwt(claims, key["private_key"])


def _gcp_token(key: dict) -> str:
    resp = request(
        "POST",
        GCP_TOKEN_URI,
        form={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": gcp_jwt(key),
        },
    )
    return expect_ok(resp, "Google Cloud sign-in")["access_token"]


def _gcp_project(config: dict, key: dict) -> str:
    project = (config.get("project_id") or key.get("project_id") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ConnectorError("Enter a valid Google Cloud project ID.")
    return project


def gcp_test(config: dict, secrets: dict) -> str:
    key = _gcp_key(secrets)
    _gcp_token(key)
    return f"Connected to Google Cloud project {_gcp_project(config, key)} as {key['client_email']}"


def gcp_collect(config: dict, secrets: dict) -> list[Check]:
    key = _gcp_key(secrets)
    project = _gcp_project(config, key)
    token = _gcp_token(key)
    resp = request(
        "GET",
        f"https://storage.googleapis.com/storage/v1/b?project={project}&maxResults=200",
        headers={"Authorization": f"Bearer {token}"},
    )
    items = expect_ok(resp, "Listing Cloud Storage buckets").get("items", [])
    locations: dict[str, list[str]] = {}
    exposed, not_uniform = [], []
    for b in items:
        locations.setdefault(b.get("location", "unknown").lower(), []).append(b["name"])
        iam = b.get("iamConfiguration", {})
        if iam.get("publicAccessPrevention") != "enforced":
            exposed.append(b["name"])
        if not iam.get("uniformBucketLevelAccess", {}).get("enabled"):
            not_uniform.append(b["name"])
    checks = [location_check("Google Cloud Storage", locations, lambda loc: loc in GCP_INDIA)]
    if items:
        checks.append(
            Check(
                "public_access",
                "Public access prevention",
                "warn" if exposed else "pass",
                ("Not enforced on: " + ", ".join(exposed[:10]))
                if exposed
                else "Enforced on every bucket.",
                SECURITY,
            )
        )
        checks.append(
            Check(
                "uniform_access",
                "Uniform bucket-level access",
                "warn" if not_uniform else "pass",
                ("Off on: " + ", ".join(not_uniform[:10]))
                if not_uniform
                else "On for every bucket, so object-level ACLs can't grant access.",
                SECURITY,
            )
        )
    return checks


# ---- Microsoft Azure (app registration with the Reader role)

ARM = "https://management.azure.com"


def _azure_token(secrets: dict) -> str:
    tenant = secrets.get("tenant_id", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.-]{3,100}", tenant):
        raise ConnectorError("Enter the directory (tenant) ID.")
    resp = request(
        "POST",
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        form={
            "grant_type": "client_credentials",
            "client_id": secrets["client_id"],
            "client_secret": secrets["client_secret"],
            "scope": f"{ARM}/.default",
        },
    )
    if resp.status in (400, 401):
        raise ConnectorError("Azure rejected the tenant ID, client ID or secret.")
    return expect_ok(resp, "Azure sign-in")["access_token"]


def _arm_list(url: str, token: str, pages: int = 5) -> list[dict]:
    items = []
    while url and pages:
        if not url.startswith(ARM + "/"):
            raise ConnectorError("Unexpected Azure address.")
        body = expect_ok(request("GET", url, headers={"Authorization": f"Bearer {token}"}), "Azure")
        items += body.get("value", [])
        url, pages = body.get("nextLink"), pages - 1
    return items


def azure_test(config: dict, secrets: dict) -> str:
    subs = _arm_list(f"{ARM}/subscriptions?api-version=2022-12-01", _azure_token(secrets))
    if not subs:
        raise ConnectorError(
            "Signed in, but the app can't see any subscriptions. Give it the Reader role."
        )
    return f"Connected to Azure: {len(subs)} subscription(s) visible"


def azure_collect(config: dict, secrets: dict) -> list[Check]:
    token = _azure_token(secrets)
    subs = _arm_list(f"{ARM}/subscriptions?api-version=2022-12-01", token)
    locations: dict[str, list[str]] = {}
    public_blob, weak = [], []
    for sub in subs[:10]:
        sid = sub["subscriptionId"]
        for res in _arm_list(f"{ARM}/subscriptions/{sid}/resources?api-version=2021-04-01", token):
            loc = (res.get("location") or "").lower()
            if loc and loc != "global":
                locations.setdefault(loc, []).append(res.get("name", "?"))
        accounts = _arm_list(
            f"{ARM}/subscriptions/{sid}/providers/Microsoft.Storage/storageAccounts?api-version=2023-05-01",
            token,
        )
        for acct in accounts:
            props = acct.get("properties", {})
            if props.get("allowBlobPublicAccess", False):
                public_blob.append(acct["name"])
            if not props.get("supportsHttpsTrafficOnly", True) or props.get(
                "minimumTlsVersion"
            ) in ("TLS1_0", "TLS1_1"):
                weak.append(acct["name"])
    checks = [location_check("Azure", locations, lambda loc: loc in AZURE_INDIA)]
    checks.append(
        Check(
            "public_access",
            "Storage public blob access",
            "warn" if public_blob else "pass",
            ("Allowed on: " + ", ".join(public_blob[:10]))
            if public_blob
            else "No storage account allows anonymous blob access.",
            SECURITY,
        )
    )
    checks.append(
        Check(
            "encryption_in_transit",
            "Encryption in transit",
            "warn" if weak else "pass",
            ("HTTP or old TLS allowed on: " + ", ".join(weak[:10]))
            if weak
            else "Storage accounts require HTTPS with TLS 1.2 or newer.",
            SECURITY,
        )
    )
    return checks
