"""
Standalone CORS/reachability diagnostic for the customer360 backend.

Run with: python check_cors.py
No dependencies beyond the Python standard library.

This sends the same kind of request a file:// page's fetch() would send
(an explicit Origin: null header) and prints back whether the server
replied with the right Access-Control-Allow-Origin header. If this
script shows everything is fine but the browser still fails, the
problem is browser-side (see the file:// note printed at the end), not
this backend's CORS config.
"""

import json
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8000"


def check(method, path, extra_headers=None):
    url = BASE_URL + path
    headers = {"Origin": "null"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, method=method, headers=headers)
    print(f"\n--- {method} {path} (Origin: null) ---")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"Status: {resp.status}")
            print(f"Access-Control-Allow-Origin: {resp.headers.get('Access-Control-Allow-Origin')}")
            print(f"Access-Control-Allow-Methods: {resp.headers.get('Access-Control-Allow-Methods')}")
    except urllib.error.HTTPError as e:
        # A non-2xx status still has headers we can inspect — this is
        # normal (e.g. 401 without a token), not a CORS failure.
        print(f"Status: {e.code} (HTTP error, not a connection failure)")
        print(f"Access-Control-Allow-Origin: {e.headers.get('Access-Control-Allow-Origin')}")
    except urllib.error.URLError as e:
        print(f"COULD NOT CONNECT AT ALL: {e.reason}")
        print("-> uvicorn is probably not running, or the port/host is wrong.")


if __name__ == "__main__":
    check("GET", "/docs")
    check("GET", "/api/search/recent")
    check(
        "OPTIONS",
        "/api/search/recent",
        {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"},
    )
    print(
        "\nIf every Access-Control-Allow-Origin above shows '*', the "
        "backend's CORS config is fine. A browser still failing from a "
        "file:// page is almost always the browser itself refusing "
        "local-file network access (or an extension/antivirus doing it) "
        "— serve the HTML via 'python -m http.server' instead of "
        "double-clicking it to rule that out."
    )
