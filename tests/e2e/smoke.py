"""End-to-end smoke test against a running Drok wired to tests/e2e/mock_llm.py.

    uv run python tests/e2e/smoke.py http://localhost:8080 <admin password>

Exercises: login, shell on the computer, browser over CDP, screenshot, approval park/resume, agent handoff,
routine test run, webhook trigger. Exits non-zero on the first failure.
"""

import re
import sys
import time

import httpx

BASE, PASSWORD = sys.argv[1].rstrip("/"), sys.argv[2]
c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        sys.exit(1)


def conv_of(agent_id):
    html = c.get(f"/agents/{agent_id}").text
    return int(re.search(r'href="/c/(\d+)">Open chat', html).group(1))


def wait_for(cid, pattern, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        html = c.get(f"/c/{cid}/messages", params={"after": 0}).text
        if re.search(pattern, html, re.S):
            return html
        time.sleep(1)
    print(c.get(f"/c/{cid}/messages", params={"after": 0}).text[-3000:])
    return None


r = c.post("/login", data={"password": "wrong"})
check("error" in r.headers.get("location", ""), "wrong password rejected")
r = c.post("/login", data={"password": PASSWORD})
check(r.status_code == 303 and r.headers["location"] == "/", "login")
check(c.get("/", headers={"accept": "text/html"}).status_code == 307, "home redirects to lead chat")

forge, chief, scout = conv_of("forge"), conv_of("chief"), conv_of("scout")

c.post(f"/c/{forge}/send", data={"text": "e2e shell: echo drok-$((40+2)) && uname -s"})
check(wait_for(forge, r"drok-42.*Linux"), "shell runs on the computer and the reply comes back")

c.post(f"/c/{scout}/send", data={"text": "e2e browse: data:text/html,<h1>Hello Drok</h1><button>Press me</button>"})
check(wait_for(scout, r"Interactive elements:.*button.*Press me"), "browser goto + DOM snapshot over CDP")

c.post(f"/c/{scout}/send", data={"text": "e2e screen"})
check(wait_for(scout, r"Screen \d after screenshot"), "computer screenshot")

c.post(f"/c/{forge}/send", data={"text": "e2e push"})
html = wait_for(forge, r'data-approval-id="(\d+)"')
check(html is not None, "git push parks for approval")
aid = re.findall(r'data-approval-id="(\d+)"', html)[-1]
c.post(f"/approvals/{aid}", data={"decision": "once", "args": ""}, headers={"x-requested-with": "fetch"})
check(wait_for(forge, r"exit code: \d+.*git push|Tool said:.*exit code", 30), "approved call resumes and runs")

c.post(f"/c/{chief}/send", data={"text": "e2e handoff"})
check(wait_for(chief, r"Teammate reported back", 60), "handoff to forge and reply back to chief")

r = c.post("/routines", data={"agent_id": "forge", "name": "smoke", "instructions": "e2e shell: echo routine-ok",
                              "kind": "schedule", "cron": "0 3 * * *", "tz": "UTC"})
check(r.status_code == 303, "routine created")
rid = re.findall(r'action="/routines/(\d+)/test"', c.get("/routines").text)[-1]
c.post(f"/routines/{rid}/test")
check(wait_for(forge, r"Tool said:.*routine-ok"), "routine test run executes")

r = c.post("/routines", data={"agent_id": "forge", "name": "hook", "instructions": "e2e shell: echo hook-ok",
                              "kind": "webhook", "match": "deploy"})
page = c.get("/routines").text
url = re.findall(r'value="[^"]*?(/hooks/\d+\?secret=[^"]+)"', page)[-1]
check(httpx.post(BASE + url, content="nothing here").json()["fired"] is False, "webhook filter ignores non-matching")
check(httpx.post(BASE + url.split("?")[0], content="deploy").status_code == 404, "webhook without secret rejected")
check(httpx.post(BASE + url, content="deploy done").json()["fired"] is True, "webhook fires on match")
check(wait_for(forge, r"Tool said:.*hook-ok"), "webhook-triggered run executes")

for path in ("/computer", "/approvals", "/routines", "/skills", "/settings", "/activity", "/files", "/agents"):
    check(c.get(path).status_code == 200, f"page {path}")
print("\nAll smoke checks passed.")
