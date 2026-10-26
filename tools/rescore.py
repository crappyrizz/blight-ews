"""Re-score one node on the hosted server as admin: the demo's "trigger the alert" step.

    .\\.venv\\Scripts\\python.exe -m tools.rescore                  # demo-01 on Render
    .\\.venv\\Scripts\\python.exe -m tools.rescore node-01 --url http://127.0.0.1:8000

Asks for the admin password (typed hidden; never printed or stored), logs in,
calls POST /admin/nodes/{node_id}/rescore, and says in plain words what came
back: the risk level, and whether the high-risk SMS went out. Waits up to two
minutes per request, because the free Render server can take a minute to wake.
"""
import argparse
import getpass

import httpx

DEFAULT_URL = "https://blight-ews-api.onrender.com"
DEFAULT_NODE = "demo-01"
ADMIN_PHONE = "0700000001"
TIMEOUT_S = 120

SMS_MEANING = {
    "submitted": "sent: Africa's Talking accepted it (look in the simulator)",
    "dry_run": "NOT sent: SMS_DRY_RUN is still true on the server (logged only)",
    "error": "NOT sent: Africa's Talking could not be reached or refused the key (check AT_API_KEY)",
    None: "not sent this time: the risk did not CHANGE to high, or the 24 h cooldown applies "
          "(re-run tools.seed_demo_event to reset a demo device)",
}


class RescoreError(Exception):
    """Something the person running the demo can act on: the message says what."""


def login(client: httpx.Client, base_url: str, phone: str, password: str) -> str:
    response = client.post(f"{base_url}/auth/login", json={"phone_number": phone, "password": password})
    if response.status_code == 401:
        raise RescoreError("login refused: wrong phone number or password")
    response.raise_for_status()
    return response.json()["access_token"]


def rescore(client: httpx.Client, base_url: str, token: str, node_id: str) -> dict:
    response = client.post(f"{base_url}/admin/nodes/{node_id}/rescore",
                           headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 401:
        raise RescoreError("the server did not accept the login token")
    if response.status_code == 403:
        raise RescoreError("that account is not an admin; log in with the admin phone number")
    if response.status_code == 404:
        raise RescoreError(f"no node called {node_id} on this server")
    response.raise_for_status()
    return response.json()


def describe(result: dict) -> list[str]:
    sms = result.get("high_risk_sms")
    return [
        f"{result['node_id']}: risk {result['risk_level'].upper()} "
        f"(score {result['risk_score']:.2f}, from {result['risk_basis']})",
        f"High-risk SMS: {SMS_MEANING.get(sms, sms)}",
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Re-score a node as admin (risk and SMS check now).")
    parser.add_argument("node_id", nargs="?", default=DEFAULT_NODE)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--phone", default=ADMIN_PHONE, help="admin phone number")
    args = parser.parse_args(argv)

    password = getpass.getpass(f"Admin password for {args.phone}: ")
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            print(f"Logging in to {args.url} (up to {TIMEOUT_S} s if the server is waking up)...")
            token = login(client, args.url, args.phone, password)
            print(f"Re-scoring {args.node_id}...")
            for line in describe(rescore(client, args.url, token, args.node_id)):
                print(line)
    except RescoreError as error:
        print(f"Not done: {error}")
        return 1
    except httpx.HTTPError as error:
        print(f"Not done: could not talk to the server ({type(error).__name__}: {error})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
