"""Trade the app's client credentials for a short-lived Admin API access token.

Shopify's client credentials grant returns a token that expires in 24 hours,
so this runs at the start of every pull rather than being stored anywhere.
Docs: https://shopify.dev/docs/apps/build/dev-dashboard/get-api-access-tokens
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

STORE = os.getenv("SHOPIFY_STORE")
CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET")

TOKEN_URL = f"https://{STORE}.myshopify.com/admin/oauth/access_token"


def get_access_token() -> str:
    """Return a fresh Admin API access token."""
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    response.raise_for_status()  # turn a 4xx/5xx into a loud Python error

    payload = response.json()
    print(f"scopes granted : {payload['scope']}")
    print(f"expires in     : {payload['expires_in']} seconds")
    return payload["access_token"]


if __name__ == "__main__":
    token = get_access_token()
    print(f"token length   : {len(token)} characters")
