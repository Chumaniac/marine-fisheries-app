"""AWS HTTP API payload-v2 read aggregation adapter; deployment is explicit.

The trusted origin performs token verification and tenant scoping. No user URL,
forwarded identity header, cookies, or write method is accepted here.
"""

import base64
import gzip
import json
import os
from urllib.parse import urlencode, urlsplit
import httpx


def handler(event, context=None):
    request = event.get("requestContext", {}).get("http", {})
    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    if request.get("method") != "GET" or event.get("rawPath") != "/v1/dashboard":
        return {"statusCode": 404, "body": '{"code":"NOT_FOUND"}'}
    token = headers.get("authorization", "")
    if not token.startswith("Bearer ") or len(token) > 8192:
        return {"statusCode": 401, "body": '{"code":"AUTH_REQUIRED"}'}
    origin = os.environ["MARINE_ORIGIN"]
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Trusted HTTPS origin required")
    query = event.get("queryStringParameters") or {}
    if not query.keys() <= {"siteId", "speciesId"} or any(
        not isinstance(v, str) or not 1 <= len(v) <= 128 for v in query.values()
    ):
        return {"statusCode": 422, "body": '{"code":"INVALID_SCOPE"}'}
    try:
        with httpx.Client(timeout=4, trust_env=False, follow_redirects=False) as client:
            with client.stream(
                "GET",
                origin.rstrip("/") + "/v1/dashboard?" + urlencode(query),
                headers={"Authorization": token},
            ) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise ValueError("Origin response too large")
                if response.status_code != 200:
                    return {
                        "statusCode": response.status_code
                        if response.status_code in {401, 403, 422}
                        else 503,
                        "body": '{"code":"ORIGIN_UNAVAILABLE"}',
                    }
                json.loads(data)
        packed = gzip.compress(bytes(data), compresslevel=6)
        return {
            "statusCode": 200,
            "isBase64Encoded": True,
            "headers": {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "Cache-Control": "no-store",
                "Vary": "Authorization",
            },
            "body": base64.b64encode(packed).decode(),
        }
    except (httpx.HTTPError, ValueError):
        return {
            "statusCode": 503,
            "headers": {"Retry-After": "5"},
            "body": '{"code":"ORIGIN_UNAVAILABLE"}',
        }
