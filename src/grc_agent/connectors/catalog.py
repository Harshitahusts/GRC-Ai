"""Every connector the app knows about: what it needs, what it checks, and why.

"available" connectors work now. "planned" ones show as "Coming soon", so the
roadmap is visible in the app; they follow the categories GRC and DPDP platforms connect
to (cloud, identity, HR, code, ticketing, devices, and data stores for
personal-data discovery).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from grc_agent.connectors import chat, cloud, identity, vcs


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
    why: str  # why it matters for DPDP work
    status: str = "available"  # available | planned (shown as "Coming soon")
    kind: str = "evidence"  # evidence (collects checks) | notify (posts messages)
    flow: str = "form"  # form (paste credentials) | github_app | aws_role (client authorises)
    fields: tuple[Field, ...] = ()
    setup: tuple[str, ...] = ()
    permissions: str = ""
    test: Callable | None = field(default=None, compare=False)
    collect: Callable | None = field(default=None, compare=False)
    send: Callable | None = field(default=None, compare=False)
    # The quick way, for connectors whose main flow needs a one-time firm setup (a GitHub
    # App, the firm's own AWS account): paste a read-only key. Shown first when present.
    key_fields: tuple[Field, ...] = ()
    key_setup: tuple[str, ...] = ()
    key_permissions: str = ""
    key_test: Callable | None = field(default=None, compare=False)
    key_collect: Callable | None = field(default=None, compare=False)

    @property
    def has_keys(self) -> bool:
        return bool(self.key_fields)


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
        key_fields=(
            Field(
                "owner",
                "GitHub user or organisation to check",
                required=False,
                placeholder="acme-labs",
                help="Leave blank to check the token owner's own repositories.",
            ),
            Field("token", "Personal access token", secret=True, placeholder="github_pat_…"),
        ),
        key_setup=(
            "On GitHub: your picture → Settings → Developer settings → Personal access tokens "
            "→ Fine-grained tokens → Generate new token.",
            "Resource owner: the user or organisation to check. Expiration: 90 days or less.",
            "Repository access: All repositories (or pick the ones in scope).",
            "Repository permissions: Metadata → Read-only and Administration → Read-only. "
            "Nothing else. (Administration read lets the app see branch protection.)",
            "Generate, copy the token (starts with github_pat_) and paste it here with the "
            "user or organisation name.",
        ),
        key_permissions="Read-only: Metadata and Administration (read). A classic token "
        "with the repo scope also works but can write, so prefer a fine-grained one.",
        key_test=vcs.github_token_test,
        key_collect=vcs.github_token_collect,
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
        key_fields=(
            Field("access_key_id", "Access key ID", secret=True, placeholder="AKIA…"),
            Field("secret_access_key", "Secret access key", secret=True),
            Field(
                "region",
                "Home region",
                required=False,
                placeholder="ap-south-1",
                help="Where most of the client's resources are. Mumbai is ap-south-1.",
            ),
        ),
        key_setup=(
            "In the client's AWS console open IAM → Users → Create user, e.g. grc-flow-readonly. "
            "No console access.",
            "Permissions: Attach policies directly → tick SecurityAudit (an AWS managed, "
            "read-only policy). Nothing else.",
            "Open the user → Security credentials → Create access key → Third-party service.",
            "Copy the Access key ID and the Secret access key (shown only once) and paste "
            "them here. Never use the root account's keys.",
            "To remove access, delete the access key or the user.",
        ),
        key_permissions="Read-only: the AWS managed policy SecurityAudit on an IAM user the "
        "client creates and can delete at any time.",
        key_test=cloud.aws_key_test,
        key_collect=cloud.aws_key_collect,
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
    # ---- Identity and access
    Connector(
        "entra_id",
        "Microsoft Entra ID",
        "Identity and access",
        "Microsoft 365 sign-ins: MFA coverage, Conditional Access or security defaults, Global Administrators and guest accounts.",
        "Access control is a named safeguard (Rule 6): who can sign in, and whether MFA protects them.",
        fields=(
            Field("tenant_id", "Directory (tenant) ID", secret=True),
            Field("client_id", "Application (client) ID", secret=True),
            Field("client_secret", "Client secret", secret=True),
        ),
        setup=(
            "In the Microsoft Entra admin center (entra.microsoft.com): App registrations → "
            "New registration, e.g. grc-flow-readonly. Single tenant. No redirect URI.",
            "API permissions → Add a permission → Microsoft Graph → Application permissions: "
            "User.Read.All, AuditLog.Read.All, Policy.Read.All and RoleManagement.Read.Directory. "
            "Then click Grant admin consent.",
            "Certificates & secrets → New client secret (6 to 12 months). Copy its Value.",
            "Paste the Directory (tenant) ID and Application (client) ID from Overview, and the "
            "secret, here. To remove access, delete the app registration.",
        ),
        permissions="Read-only Microsoft Graph application permissions: User.Read.All, "
        "AuditLog.Read.All, Policy.Read.All, RoleManagement.Read.Directory. The MFA report "
        "needs a Microsoft Entra ID P1 or P2 licence; without one that check is skipped.",
        test=identity.entra_test,
        collect=identity.entra_collect,
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


BY_ID = {c.id: c for c in CONNECTORS}


def by_category() -> list[tuple[str, list[Connector]]]:
    """Working connectors by category. Planned ones are not shown as cards."""
    groups = [
        (cat, [c for c in CONNECTORS if c.category == cat and c.status == "available"])
        for cat in CATEGORIES
    ]
    return [(cat, items) for cat, items in groups if items]


def planned_names() -> list[str]:
    """Names of connectors on the roadmap, for one line on the Connectors page."""
    return [c.name for c in CONNECTORS if c.status == "planned"]
