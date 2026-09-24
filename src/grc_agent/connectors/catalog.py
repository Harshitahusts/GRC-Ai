"""Every connector the app knows about: what it needs, what it checks, and why.

"available" connectors work now. "planned" ones show as "Coming soon", so the
roadmap is visible in the app; they follow the categories GRC and DPDPA platforms connect
to (cloud, identity, HR, code, ticketing, devices, and data stores for
personal-data discovery).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from grc_agent.connectors import chat, cloud, vcs


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""
    help: str = ""
    multiline: bool = False


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    category: str
    summary: str
    why: str  # why it matters for DPDPA work
    status: str = "available"  # available | planned (shown as "Coming soon")
    kind: str = "evidence"  # evidence (collects checks) | notify (posts messages)
    flow: str = "form"  # form (paste credentials) | github_app | aws_role (client authorises)
    fields: tuple[Field, ...] = ()
    setup: tuple[str, ...] = ()
    permissions: str = ""
    test: Callable | None = field(default=None, compare=False)
    collect: Callable | None = field(default=None, compare=False)
    send: Callable | None = field(default=None, compare=False)


CATEGORIES = [
    "Version control",
    "Cloud",
    "Communication",
    "Identity and access",
    "HR systems",
    "Ticketing and projects",
    "Devices",
    "Data stores and business apps",
]

TOKEN = Field("token", "Access token", secret=True)
WEBHOOK = Field("webhook_url", "Webhook URL", secret=True, placeholder="https://…")

CONNECTORS: tuple[Connector, ...] = (
    # ---- Version control
    Connector(
        "github",
        "GitHub",
        "Version control",
        "Repository visibility, default branch protection and secret scanning.",
        "Public repositories and leaked secrets are a common route to personal data breaches.",
        flow="github_app",
        setup=(
            "Click Authorize on GitHub, or send the client the install link.",
            "The client picks their organisation and which repositories to share.",
            "They approve read-only access on GitHub's own screen. No token is copied anywhere.",
            "They can remove the app at any time in their GitHub settings under Applications.",
        ),
        permissions="Read-only: Metadata and Administration (read), on the repositories the client chooses.",
    ),
    Connector(
        "gitlab",
        "GitLab",
        "Version control",
        "Project visibility and default branch protection, on gitlab.com or self-managed.",
        "Public projects and unprotected branches put code and data at risk.",
        fields=(
            Field(
                "base_url",
                "GitLab address",
                required=False,
                placeholder="https://gitlab.com",
                help="Leave blank for gitlab.com. Self-managed GitLab must be reachable over HTTPS.",
            ),
            TOKEN,
        ),
        setup=(
            "In GitLab go to Preferences → Access tokens (or a group access token).",
            "Scope: read_api only. Set an expiry date.",
            "Paste the token here.",
        ),
        permissions="Read-only: the read_api scope.",
        test=vcs.gitlab_test,
        collect=vcs.gitlab_collect,
    ),
    Connector(
        "bitbucket",
        "Bitbucket",
        "Version control",
        "Repository visibility and branch restrictions on the main branch.",
        "Public repositories and unprotected branches put code and data at risk.",
        fields=(
            Field("email", "Atlassian account email", secret=True),
            Field("token", "API token", secret=True),
        ),
        setup=(
            "Go to id.atlassian.com → Security → API tokens → Create API token with scopes.",
            "Choose Bitbucket and the scopes read:repository:bitbucket and read:user:bitbucket.",
            "Paste your Atlassian email and the token here. App passwords no longer work.",
        ),
        permissions="Read-only repository and user scopes.",
        test=vcs.bitbucket_test,
        collect=vcs.bitbucket_collect,
    ),
    # ---- Cloud
    Connector(
        "aws",
        "Amazon Web Services",
        "Cloud",
        "Where S3 data is stored, S3 public access, root MFA, password policy and CloudTrail.",
        "Shows whether personal data sits outside India (Section 16) and checks basic safeguards.",
        flow="aws_role",
        fields=(
            Field(
                "role_arn",
                "Role ARN from the client",
                placeholder="arn:aws:iam::123456789012:role/…",
            ),
            Field("region", "Home region", required=False, placeholder="ap-south-1"),
        ),
        setup=(
            "Download the CloudFormation template below and send it to the client.",
            "The client opens CloudFormation → Create stack → Upload a template file, and creates the stack.",
            "It creates a read-only role (AWS SecurityAudit policy) that only this firm can use, locked to this engagement's external ID.",
            "The client copies RoleArn from the stack's Outputs tab and sends it back. Paste it here.",
            "To remove access, the client deletes the stack.",
        ),
        permissions="Read-only: the AWS managed policy SecurityAudit, through a role the client controls.",
        test=cloud.aws_test,
        collect=cloud.aws_collect,
    ),
    Connector(
        "gcp",
        "Google Cloud",
        "Cloud",
        "Where Cloud Storage data is stored, public access prevention and uniform access.",
        "Shows whether personal data sits outside India (Section 16) and checks bucket exposure.",
        fields=(
            Field(
                "project_id",
                "Project ID",
                required=False,
                help="Leave blank to use the project in the key file.",
            ),
            Field(
                "service_account_json", "Service account key (JSON)", secret=True, multiline=True
            ),
        ),
        setup=(
            "In IAM & Admin → Service accounts, create a service account for this app.",
            "Grant it Storage Object Viewer and Browser (or Viewer) on the project.",
            "Keys → Add key → JSON. Paste the whole file here.",
        ),
        permissions="Read-only: Browser and Storage Object Viewer.",
        test=cloud.gcp_test,
        collect=cloud.gcp_collect,
    ),
    Connector(
        "azure",
        "Microsoft Azure",
        "Cloud",
        "Where resources are located, storage public blob access and TLS settings.",
        "Shows whether personal data sits outside India (Section 16) and checks storage exposure.",
        fields=(
            Field("tenant_id", "Directory (tenant) ID", secret=True),
            Field("client_id", "Application (client) ID", secret=True),
            Field("client_secret", "Client secret", secret=True),
        ),
        setup=(
            "In Microsoft Entra ID → App registrations, register an app for this tool.",
            "Certificates & secrets → New client secret. Copy its value.",
            "On each subscription: Access control (IAM) → Add role assignment → Reader → the app.",
            "Paste the tenant ID, client ID and secret here.",
        ),
        permissions="Read-only: the Reader role on each subscription.",
        test=cloud.azure_test,
        collect=cloud.azure_collect,
    ),
    # ---- Communication
    Connector(
        "slack",
        "Slack",
        "Communication",
        "Posts engagement updates to a Slack channel.",
        "Keeps the team (or the client) informed without checking the app.",
        kind="notify",
        fields=(WEBHOOK,),
        setup=(
            "Go to api.slack.com/apps → Create New App → From scratch.",
            "Incoming Webhooks → turn on → Add New Webhook to Workspace → pick the channel.",
            "Copy the webhook URL (https://hooks.slack.com/services/…) and paste it here.",
        ),
        permissions="Posts to one channel only.",
        send=chat.slack_send,
    ),
    Connector(
        "teams",
        "Microsoft Teams",
        "Communication",
        "Posts engagement updates to a Teams channel through Workflows.",
        "Keeps the team (or the client) informed without checking the app.",
        kind="notify",
        fields=(WEBHOOK,),
        setup=(
            "In the Teams channel: … (More options) → Workflows.",
            "Choose 'Send webhook alerts to a channel' and finish the steps.",
            "Copy the URL it gives you and paste it here. Old Office 365 connector URLs no longer work.",
        ),
        permissions="Posts to one channel only.",
        send=chat.teams_send,
    ),
    Connector(
        "google_chat",
        "Google Chat",
        "Communication",
        "Posts engagement updates to a Google Chat space.",
        "Keeps the team (or the client) informed without checking the app.",
        kind="notify",
        fields=(WEBHOOK,),
        setup=(
            "Open the Google Chat space → space name → Apps & integrations → Webhooks.",
            "Add a webhook, name it, and copy the URL (https://chat.googleapis.com/…).",
            "Paste it here. Needs a Google Workspace account.",
        ),
        permissions="Posts to one space only.",
        send=chat.google_chat_send,
    ),
    # ---- Planned
    *(
        Connector(cid, name, cat, summary, why, status="planned")
        for cid, name, cat, summary, why in (
            (
                "google_workspace",
                "Google Workspace",
                "Identity and access",
                "Users, 2-step verification and admin roles.",
                "Proves access control and finds ex-employees who still have access.",
            ),
            (
                "entra_id",
                "Microsoft Entra ID",
                "Identity and access",
                "Users, MFA and conditional access in Microsoft 365.",
                "Proves access control and MFA coverage.",
            ),
            (
                "okta",
                "Okta",
                "Identity and access",
                "Users, MFA factors and app assignments.",
                "Proves access control and MFA coverage.",
            ),
            (
                "keka",
                "Keka",
                "HR systems",
                "Joiners and leavers.",
                "Checks leavers lose access, and maps employee personal data.",
            ),
            (
                "darwinbox",
                "Darwinbox",
                "HR systems",
                "Joiners and leavers.",
                "Checks leavers lose access, and maps employee personal data.",
            ),
            (
                "zoho_people",
                "Zoho People",
                "HR systems",
                "Joiners and leavers.",
                "Checks leavers lose access, and maps employee personal data.",
            ),
            (
                "jira",
                "Jira",
                "Ticketing and projects",
                "Security and data-request tickets.",
                "Evidence for rights requests, grievances and incident handling.",
            ),
            (
                "zoho_desk",
                "Zoho Desk",
                "Ticketing and projects",
                "Customer support tickets.",
                "Evidence that grievances are answered within the set period.",
            ),
            (
                "intune",
                "Microsoft Intune",
                "Devices",
                "Disk encryption and screen lock on laptops.",
                "Evidence for safeguards on devices that hold personal data.",
            ),
            (
                "jamf",
                "Jamf",
                "Devices",
                "Disk encryption and updates on Macs.",
                "Evidence for safeguards on devices that hold personal data.",
            ),
            (
                "postgres",
                "PostgreSQL / MySQL",
                "Data stores and business apps",
                "Scan tables for personal data (names, phone numbers, Aadhaar, PAN).",
                "Builds the data inventory behind the RoPA.",
            ),
            (
                "mongodb",
                "MongoDB",
                "Data stores and business apps",
                "Scan collections for personal data.",
                "Builds the data inventory behind the RoPA.",
            ),
            (
                "salesforce",
                "Salesforce",
                "Data stores and business apps",
                "Customer records and consent fields.",
                "Maps customer personal data and consent.",
            ),
            (
                "hubspot",
                "HubSpot",
                "Data stores and business apps",
                "Contacts and marketing consent.",
                "Maps marketing data and checks consent is recorded.",
            ),
            (
                "zoho_crm",
                "Zoho CRM",
                "Data stores and business apps",
                "Customer records and consent fields.",
                "Maps customer personal data and consent.",
            ),
        )
    ),
)

# Built and tested, but switched off for now: GitHub and AWS use authorised
# read-only access first; these follow once they have the same kind of flow.
COMING_SOON = {"gitlab", "bitbucket", "gcp", "azure", "slack", "teams", "google_chat"}
CONNECTORS = tuple(replace(c, status="planned") if c.id in COMING_SOON else c for c in CONNECTORS)

BY_ID = {c.id: c for c in CONNECTORS}


def by_category() -> list[tuple[str, list[Connector]]]:
    return [(cat, [c for c in CONNECTORS if c.category == cat]) for cat in CATEGORIES]
