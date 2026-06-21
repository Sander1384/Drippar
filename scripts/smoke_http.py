import argparse
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request


def request(opener, url, data=None, headers=None):
    payload = json.dumps(data).encode("utf-8") if data is not None else None
    merged_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        merged_headers["Content-Type"] = "application/json"
    return opener.open(urllib.request.Request(url, data=payload, headers=merged_headers), timeout=10)


def main():
    parser = argparse.ArgumentParser(description="Smoke-test a running Driparr HTTP instance.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    with request(opener, f"{base}/health") as response:
        health = json.load(response)
    if not health.get("ok"):
        raise RuntimeError(f"Health check failed: {health}")

    login_payload = urllib.parse.urlencode({"username": args.username, "password": args.password}).encode("utf-8")
    with opener.open(urllib.request.Request(f"{base}/login", data=login_payload), timeout=10) as response:
        page = response.read().decode("utf-8")
    if not jar:
        raise RuntimeError("Login did not set a session cookie.")
    match = re.search(r'const DRIPARR_CSRF_TOKEN = "([a-f0-9]+)";', page)
    if not match:
        raise RuntimeError("Authenticated page did not expose a CSRF token.")
    token = match.group(1)

    try:
        request(opener, f"{base}/api/run-now", {}).read()
        raise RuntimeError("POST without a CSRF token was accepted.")
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise

    with request(opener, f"{base}/api/run-now", {}, {"X-Driparr-CSRF": token}) as response:
        accepted = json.load(response)

    print(
        json.dumps(
            {
                "ok": True,
                "version": health.get("version"),
                "health": health.get("status"),
                "login": "ok",
                "csrfRejectedWithoutToken": True,
                "authenticatedPost": accepted.get("message", "ok"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
