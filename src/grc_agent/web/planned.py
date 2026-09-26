"""Features whose buttons exist in the UI but whose backend isn't built yet.

A template marks a planned button with `data-soon="<key>"`. Clicking it shows
a "coming soon" toast instead of doing anything. When the backend lands,
replace the button with a real form or link and delete its entry here.
tests/test_ui.py checks every `data-soon` key in the templates is listed.
"""

PLANNED: dict[str, dict[str, str]] = {
    # ---- login
    "sso-google": {
        "label": "Continue with Google",
        "area": "Login",
        "backend": "OAuth 2.0 / OpenID Connect sign-in, matched to an existing user.",
    },
    "sso-microsoft": {
        "label": "Continue with Microsoft",
        "area": "Login",
        "backend": "Microsoft Entra ID sign-in, matched to an existing user.",
    },
    "forgot-password": {
        "label": "Forgot password",
        "area": "Login",
        "backend": "Email a single-use reset link that expires in 30 minutes.",
    },
    "remember-me": {
        "label": "Keep me signed in",
        "area": "Login",
        "backend": "Longer session cookie (30 days) on trusted devices.",
    },
    "request-access": {
        "label": "Request access",
        "area": "Login",
        "backend": "Send an access request to workspace admins for approval.",
    },
    "two-factor": {
        "label": "Two-step verification",
        "area": "Login",
        "backend": "TOTP code after the password, with recovery codes.",
    },
    # ---- dashboard and shell
    "global-search": {
        "label": "Search all records",
        "area": "Dashboard",
        "backend": "Full-text search over engagements, findings, evidence and documents.",
    },
    "date-range": {
        "label": "Date range filter",
        "area": "Dashboard",
        "backend": "Recompute the dashboard numbers for the chosen period.",
    },
    "customise-dashboard": {
        "label": "Customise dashboard",
        "area": "Dashboard",
        "backend": "Save which cards each user sees and in what order.",
    },
    "export-portfolio": {
        "label": "Export portfolio report",
        "area": "Dashboard",
        "backend": "PDF / Excel of all engagements, readiness and top risks.",
    },
    "invite-teammate": {
        "label": "Invite a teammate",
        "area": "Dashboard",
        "backend": "Create a user and email an invite link with a role.",
    },
    "schedule-review": {
        "label": "Schedule a client review",
        "area": "Dashboard",
        "backend": "Calendar invite plus a reminder notification before the review.",
    },
    "profile": {
        "label": "Profile & password",
        "area": "Account",
        "backend": "Change display name, email and password.",
    },
    "help": {
        "label": "Help & shortcuts",
        "area": "Account",
        "backend": "In-app guide and keyboard shortcut list.",
    },
    # ---- data manager
    "backup-now": {
        "label": "Back up now",
        "area": "Data manager",
        "backend": "Online SQLite backup to a dated file, then verify it.",
    },
    "schedule-backups": {
        "label": "Schedule backups",
        "area": "Data manager",
        "backend": "Daily backup with 14-day rotation.",
    },
    "export-data": {
        "label": "Export all data",
        "area": "Data manager",
        "backend": "Zip of every table as CSV for a data-portability request.",
    },
    "purge-expired": {
        "label": "Purge expired records",
        "area": "Data manager",
        "backend": "Delete rows past their retention period, with an audit entry.",
    },
    "set-retention": {
        "label": "Set retention",
        "area": "Data manager",
        "backend": "Store a retention period per table and apply it on purge.",
    },
    "vacuum": {
        "label": "Compact the database",
        "area": "Data manager",
        "backend": "Run VACUUM to return free pages to disk.",
    },
}
