"""Request helpers for the household API tests."""

import json


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def get(client, path, token):
    return client.get(path, **auth(token))


def post(client, path, payload, token):
    return client.post(
        path, data=json.dumps(payload), content_type="application/json", **auth(token)
    )


def patch(client, path, payload, token):
    return client.patch(
        path, data=json.dumps(payload), content_type="application/json", **auth(token)
    )
