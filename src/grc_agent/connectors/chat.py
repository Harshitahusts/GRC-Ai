"""Chat connectors: Slack, Microsoft Teams, Google Chat.

They post notifications about an engagement (intake submitted, assessment run,
draft pack ready, delivered) to a channel through an incoming webhook. The
webhook URL is a secret: anyone with it can post to the channel.

Only each service's own webhook hosts are accepted, so a pasted URL can't make
the app send data anywhere else.
"""

from __future__ import annotations

from grc_agent.connectors.base import ConnectorError, check_host, request

SLACK_HOSTS = ("hooks.slack.com",)
GOOGLE_CHAT_HOSTS = ("chat.googleapis.com",)
# Teams "Workflows" webhooks. Office 365 connector URLs were retired in 2026, and
# workflow URLs moved from logic.azure.com to environment.api.powerplatform.com.
TEAMS_HOSTS = (".environment.api.powerplatform.com", ".logic.azure.com")


def _post(url: str, payload: dict, what: str) -> None:
    resp = request("POST", url, json_body=payload)
    if resp.status in (401, 403, 404):
        raise ConnectorError(f"{what} rejected the message. The webhook may have been deleted.")
    if not resp.ok:
        raise ConnectorError(f"{what} returned HTTP {resp.status}.")


def slack_send(config: dict, secrets: dict, text: str) -> None:
    check_host(secrets["webhook_url"], SLACK_HOSTS, "Slack incoming webhook")
    _post(secrets["webhook_url"], {"text": text}, "Slack")


def google_chat_send(config: dict, secrets: dict, text: str) -> None:
    check_host(secrets["webhook_url"], GOOGLE_CHAT_HOSTS, "Google Chat webhook")
    _post(secrets["webhook_url"], {"text": text}, "Google Chat")


def teams_send(config: dict, secrets: dict, text: str) -> None:
    check_host(secrets["webhook_url"], TEAMS_HOSTS, "Microsoft Teams Workflows webhook")
    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                },
            }
        ],
    }
    _post(secrets["webhook_url"], card, "Microsoft Teams")
