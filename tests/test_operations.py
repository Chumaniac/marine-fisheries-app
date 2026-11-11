import base64
import gzip
import importlib.util
import json
import pytest
import httpx
from tools.cost_report import estimate


def test_aggregation_rejects_untrusted_input_and_gzips(monkeypatch):
    spec = importlib.util.spec_from_file_location("handler", "deploy/serverless/handler.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setenv("MARINE_ORIGIN", "https://api.example")
    request = {
        "requestContext": {"http": {"method": "GET"}},
        "rawPath": "/v1/dashboard",
        "headers": {"Authorization": "Bearer synthetic"},
        "queryStringParameters": {"siteId": "A"},
    }

    def respond(req):
        assert req.url.host == "api.example" and req.headers["Authorization"] == "Bearer synthetic"
        return httpx.Response(200, json={"tenant": "t", "prediction": None})

    real = httpx.Client
    monkeypatch.setattr(
        m.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(respond), **kw)
    )
    result = m.handler(request)
    assert json.loads(gzip.decompress(base64.b64decode(result["body"])))["tenant"] == "t"
    request["queryStringParameters"]["url"] = "https://evil.example"
    assert m.handler(request)["statusCode"] == 422
    request["requestContext"]["http"]["method"] = "POST"
    assert m.handler(request)["statusCode"] == 404
    assert estimate({"requests": 10}, {"requests": 0.1})["savingsFraction"] is None
    with pytest.raises(ValueError):
        estimate({"x": float("nan")}, {"x": 1})
