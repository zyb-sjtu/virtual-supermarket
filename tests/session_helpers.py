"""HTTP session helpers for permission-aware tests."""
import json
from http.cookiejar import CookieJar
from urllib.request import Request, build_opener, HTTPCookieProcessor


def login(base, role):
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    request = Request(f"{base}/api/session", json.dumps({"role": role}).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(request) as response:
        data = json.load(response)
    return opener, data
