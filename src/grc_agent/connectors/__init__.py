"""Connectors: read-only links to a client's systems that collect evidence
(version control, cloud) or post notifications (Slack, Teams, Google Chat)."""

from grc_agent.connectors.base import Check, ConnectorError
from grc_agent.connectors.catalog import BY_ID, CATEGORIES, CONNECTORS, Connector, by_category

__all__ = [
    "BY_ID",
    "CATEGORIES",
    "CONNECTORS",
    "Check",
    "Connector",
    "ConnectorError",
    "by_category",
]
