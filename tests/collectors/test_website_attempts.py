import httpx

from src.collectors.website import classify_website_failure, website_retry_seconds


def test_website_failure_taxonomy_and_backoff_are_specific():
    assert classify_website_failure(None, status_code=429) == "429"
    assert classify_website_failure(None, status_code=503) == "5xx"
    assert classify_website_failure(httpx.ConnectError("[Errno -2] Name or service not known")) == "dns"
    assert classify_website_failure(httpx.ReadTimeout("timed out")) == "timeout"
    assert classify_website_failure(httpx.ConnectError("SSL certificate verify failed")) == "tls"
    assert website_retry_seconds("tls", 1) == 21600
    assert website_retry_seconds("429", 2) > website_retry_seconds("connection", 1)
