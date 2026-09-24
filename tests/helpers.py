"""Shared helpers for the web tests."""

import re

PASSWORD = "correct-horse-battery"


def csrf(client, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def login(client, password=PASSWORD):
    return client.post(
        "/login",
        data={"username": "harshit", "password": password, "csrf": csrf(client)},
        follow_redirects=False,
    )


def post(client, path, data=None, **kwargs):
    return client.post(path, data={**(data or {}), "csrf": csrf(client, "/")}, **kwargs)


def create(client, mode="agent"):
    response = post(
        client, "/engagements", {"client": "Acme Pvt Ltd", "sector": "SaaS", "mode": mode}
    )
    return int(response.url.path.rsplit("/", 1)[1])


ALL_YES = {
    "q_INFO-DATA": "Names, emails",
    "q_CTX-CHILDREN": "no",
    "q_CTX-VENDORS": "yes",
    "q_CTX-FOREIGN": "no",
    **{
        f"q_{q}": "yes"
        for q in [
            "Q-NOTICE",
            "Q-CONSENT",
            "Q-WITHDRAW",
            "Q-SECURITY",
            "Q-BREACH",
            "Q-ERASURE",
            "Q-CONTACT",
            "Q-GRIEVANCE",
            "Q-ACCESS",
            "Q-CORRECTION",
            "Q-VENDOR-CONTRACT",
        ]
    },
}
