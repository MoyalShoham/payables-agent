"""Smoke test of the running Docker Compose stack, through nginx, like a browser.

    docker compose up -d --build && python3 scripts/smoke.py [http://127.0.0.1:3100]

Waits for the demo seed, signs in as the approver, approves a ready draft, approves the bank-change
draft with a phone callback, fires 20 approvals of one draft at once (exactly one may succeed),
verifies the audit chain and checks that signing out revokes the session. Standard library only.
"""

from __future__ import annotations  # runs on any Python 3.9+

import http.cookiejar
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3100").rstrip("/")
HEADERS = {"X-Payables-Client": "smoke", "Content-Type": "application/json"}


def client() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(opener: urllib.request.OpenerDirector, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=HEADERS)  # noqa: S310
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def wait_for(what: str, check: Any, seconds: int = 300) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(2)
    sys.exit(f"timed out waiting for {what}")


def main() -> None:
    anon = client()
    wait_for("the API", lambda: call(anon, "GET", "/api/health")[0] == 200)
    approver = client()
    status, me = call(approver, "POST", "/api/session", {"username": "approver", "password": "change-me-approver"})
    assert status == 200, (status, me)
    drafts = wait_for("the demo seed", lambda: (d := call(approver, "GET", "/api/drafts")[1]) and len(d) >= 12 and d)
    print(f"signed in as {me['display_name']}; {len(drafts)} drafts")

    ready = [d for d in drafts if d["state"] == "READY"]
    assert len(ready) >= 2, [d["state"] for d in drafts]
    status, body = call(approver, "POST", f"/api/drafts/{ready[0]['id']}/approve", {"comment": "smoke"})
    assert status == 200, (status, body)
    print(f"approved draft {ready[0]['id']}: simulated payment {body['simulated_payment_id']}")

    bank = next(d for d in drafts if d["requires_bank_verification"] and d["state"] == "NEEDS_ATTENTION")
    detail = call(approver, "GET", f"/api/drafts/{bank['id']}")[1]
    status, body = call(approver, "POST", f"/api/drafts/{bank['id']}/approve", {})
    assert status == 409, (status, body)
    at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    callback = {"phone": detail["vendor_phones"][0], "person": "Smoke test, accounts", "at": at}
    status, body = call(approver, "POST", f"/api/drafts/{bank['id']}/approve", {"callback": callback})
    assert status == 200, (status, body)
    print(f"approved bank-change draft {bank['id']} after a callback to {callback['phone']}")

    target = ready[1]["id"]
    statuses: list[int] = []
    barrier = threading.Barrier(20)

    def approve() -> None:
        barrier.wait()
        statuses.append(call(approver, "POST", f"/api/drafts/{target}/approve", {})[0])

    threads = [threading.Thread(target=approve) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    counts = Counter(statuses)
    print(f"20 concurrent approvals of draft {target}: {dict(counts)}")
    assert counts == Counter({200: 1, 409: 19}), counts

    status, verified = call(approver, "GET", "/api/audit/verify")
    assert status == 200 and verified["intact"] and verified["checkpoint"], verified
    print(f"audit chain intact: {verified['entries']} entries, checkpoint {verified['checkpoint']['seq']}")

    assert call(approver, "DELETE", "/api/session")[0] == 200
    assert call(approver, "GET", "/api/me")[0] == 401
    print("signed out; the session no longer works")
    print("smoke test passed")


if __name__ == "__main__":
    main()
