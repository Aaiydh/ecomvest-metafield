"""Shared Shopify Admin API client: OAuth, GraphQL, pagination."""
import json
import secrets
import time
import urllib.parse
from pathlib import Path

import requests

API_VERSION = "2024-10"
REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = (
    "read_channels,read_files,write_files,write_inventory,read_inventory,"
    "read_metaobject_definitions,write_metaobject_definitions,read_metaobjects,"
    "write_metaobjects,read_product_feeds,write_product_feeds,read_product_listings,"
    "write_product_listings,read_products,write_products,read_reports,"
    "unauthenticated_read_product_inventory,unauthenticated_read_product_listings,"
    "unauthenticated_read_product_tags"
)

STORES_DIR = Path(__file__).parent / "config" / "stores"


def _is_throttled(errors: list) -> bool:
    return any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors)


def _store_path(name: str) -> Path:
    return STORES_DIR / f"{name}.json"


def list_store_names() -> list:
    """Brand/store names with a config file present, e.g. ['Luxfi', 'Nova']."""
    if not STORES_DIR.exists():
        return []
    return sorted(p.stem for p in STORES_DIR.glob("*.json"))


def get_store_config(name: str) -> dict:
    path = _store_path(name)
    if not path.exists():
        raise FileNotFoundError(
            f"No config for store '{name}' at {path} - copy "
            f"config/stores/_template.json.example to config/stores/{name}.json and fill it in."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def save_store_config(name: str, config: dict) -> None:
    _store_path(name).write_text(json.dumps(config, indent=2), encoding="utf-8")


def authorize_url(name: str) -> str:
    store = get_store_config(name)
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": store["client_id"],
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    return f"https://{store['shop']}/admin/oauth/authorize?{urllib.parse.urlencode(params)}"


def exchange_code(name: str) -> str:
    store = get_store_config(name)
    if not store.get("auth_code"):
        raise ValueError(f"Store '{name}' has no auth_code set - run authorize --show-url first.")

    resp = requests.post(
        f"https://{store['shop']}/admin/oauth/access_token",
        json={
            "client_id": store["client_id"],
            "client_secret": store["client_secret"],
            "code": store["auth_code"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]

    store["token"] = token
    store["auth_code"] = ""
    save_store_config(name, store)
    return token


class ShopifyClient:
    def __init__(self, store_name: str):
        store = get_store_config(store_name)
        if not store.get("token"):
            raise ValueError(f"Store '{store_name}' has no token yet - run the authorize flow first.")
        self.store_name = store_name
        self.shop = store["shop"]
        self.token = store["token"]
        self.endpoint = f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json"

    def graphql(self, query: str, variables: dict | None = None, max_retries: int = 5) -> dict:
        for attempt in range(max_retries + 1):
            resp = requests.post(
                self.endpoint,
                headers={
                    "X-Shopify-Access-Token": self.token,
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables or {}},
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
            if "errors" in payload:
                if attempt < max_retries and _is_throttled(payload["errors"]):
                    time.sleep(min(2 ** attempt, 16))
                    continue
                raise RuntimeError(f"GraphQL errors: {json.dumps(payload['errors'], indent=2)}")
            return payload["data"]

    def paginate(self, query: str, variables: dict, data_path: list):
        """
        Walk a `connection { edges { node } pageInfo { hasNextPage endCursor } }` field,
        yielding one node dict at a time. `data_path` locates the connection inside the
        response, e.g. ["products"] for `{ products(...) { edges pageInfo } }`.
        """
        cursor = None
        vars_ = dict(variables)
        while True:
            vars_["cursor"] = cursor
            data = self.graphql(query, vars_)
            connection = data
            for key in data_path:
                connection = connection[key]

            for edge in connection["edges"]:
                yield edge["node"]

            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]
