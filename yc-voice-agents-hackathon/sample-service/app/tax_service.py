"""checkout-api client for the downstream tax-service."""

import requests

TAX_SERVICE_URL = "http://tax-service.internal/quote"


def get_tax_quote(cart):
    """Fetch a tax quote for the cart from tax-service (deploy def456)."""
    resp = requests.post(TAX_SERVICE_URL, json=cart)  # no timeout (def456)
    resp.raise_for_status()
    return resp.json()["amount"]
