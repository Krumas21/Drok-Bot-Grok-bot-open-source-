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

# ---------- harness ----------
c.post(f"/c/{forge}/send", data={"text": "e2e plan"})
check(wait_for(forge, r'class="plan".*1/2 done'), "plan renders as a live checklist")

c.post(f"/c/{scout}/send", data={"text": "e2e delegate"})
check(wait_for(scout, r"Worker 1.*worker-one.*Worker 2.*worker-two", 90), "delegate runs two isolated workers in parallel")

c.post(f"/c/{forge}/send", data={"text": "e2e makefile"})
check(wait_for(forge, r"Created /workspace/e2e/report.pdf", 90), "make_file renders markdown to PDF")
check(c.get("/files/e2e/report.pdf", params={"raw": 1}).content[:4] == b"%PDF", "the PDF is a real PDF")
c.post(f"/c/{forge}/send", data={"text": "e2e sheet"})
check(wait_for(forge, r"Created /workspace/e2e/data.xlsx"), "make_file builds a spreadsheet")

c.post(f"/c/{scout}/send", data={"text": "e2e extract: https://example.com"})
check(wait_for(scout, r"Example Domain", 60), "web_extract scrapes a page into markdown")

c.post(f"/c/{chief}/send", data={"text": "e2e memory - note that my favourite colour is orange"})
check(wait_for(chief, r"Remembered"), "memory add")
c.post(f"/c/{scout}/send", data={"text": "e2e recall"})
check(wait_for(scout, r"is orange"), "recall finds it in another agent's conversation")

c.post(f"/c/{chief}/send", data={"text": "e2e clarify"})
check(wait_for(chief, r'data-answer.*Blue'), "clarify asks with option buttons")
c.post(f"/c/{chief}/send", data={"text": "Blue"})
check(wait_for(chief, r"Mock reply to: Blue"), "the answer reaches the agent")

c.post(f"/c/{forge}/send", data={"text": "e2e bg"})
check(wait_for(forge, r"bg-done", 60), "background process runs and its log is read back")

c.post(f"/c/{forge}/send", data={"text": "e2e tool"})
html = wait_for(forge, r'data-approval-id="(\d+)"[^>]*>\s*<div class="callout-head"><span><strong>Forge</strong> wants to run <code>tool_create')
check(html is not None, "building a tool asks the owner first")
aid = re.findall(r'data-approval-id="(\d+)"', html)[-1]
c.post(f"/approvals/{aid}", data={"decision": "once", "args": ""}, headers={"x-requested-with": "fetch"})
check(wait_for(forge, r"registered for the whole team"), "self-built tool registered after a passing test run")
check("e2e_echo" in c.get("/settings").text, "self-built tool listed in settings")

c.post(f"/c/{forge}/send", data={"text": "e2e goal"})
end = time.time() + 120
status = ""
while time.time() < end:
    page = c.get("/goals").text
    m = re.search(r'<span class="tag (\w+)">(\w+)</span>\s*<h3><a href="/goals/(\d+)">#\d+ E2E goal', page)
    status = m.group(2) if m else ""
    if status == "done":
        break
    time.sleep(2)
check(status == "done", f"goal ran across turns and passed verification (status: {status or 'missing'})")
check((c.get("/files/e2e-goal.txt", params={"raw": 1}).text == "goal done"), "goal's deliverable exists")

for path in ("/computer", "/approvals", "/routines", "/skills", "/settings", "/activity", "/files", "/agents",
             "/goals", "/agents/forge"):
    check(c.get(path).status_code == 200, f"page {path}")
print("\nAll smoke checks passed.")
