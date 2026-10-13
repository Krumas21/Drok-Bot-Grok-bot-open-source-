"""End-to-end smoke test against a running Drok wired to tests/e2e/mock_llm.py.

    uv run python tests/e2e/smoke.py http://localhost:8080 <admin password>

Exercises the platform (login, shell, browser over CDP, screenshots, approvals, handoffs, routines, webhooks) and the
harness (plans, delegation, documents, scraping, memory + recall, clarify, background processes, self-built tools,
long-horizon goals with verification). Each check only looks at messages that arrived after its own trigger, so a
leftover from an earlier step can't make it pass. Exits non-zero on the first failure.
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


def latest(cid) -> int:
    ids = re.findall(r'data-id="(\d+)"', c.get(f"/c/{cid}/messages", params={"after": 0}).text)
    return max(map(int, ids), default=0)


def send(cid, text) -> int:
    """Post as the owner; returns the new message id (the start of this step's window)."""
    return c.post(f"/c/{cid}/send", data={"text": text}).json()["id"]


def wait_for(cid, pattern, after, timeout=60):
    end = time.time() + timeout
    html = ""
    while time.time() < end:
        html = c.get(f"/c/{cid}/messages", params={"after": after}).text
        if re.search(pattern, html, re.S):
            return html
        time.sleep(1)
    print(html[-3000:])
    return None


r = c.post("/login", data={"password": "wrong"})
check("error" in r.headers.get("location", ""), "wrong password rejected")
r = c.post("/login", data={"password": PASSWORD})
check(r.status_code == 303 and r.headers["location"] == "/", "login")
check(c.get("/", headers={"accept": "text/html"}).status_code == 307, "home redirects to lead chat")

forge, chief, scout = conv_of("forge"), conv_of("chief"), conv_of("scout")

# ---------- platform ----------
m = send(forge, "e2e shell: echo drok-$((40+2)) && uname -s")
check(wait_for(forge, r"drok-42.*Linux", m), "shell runs on the computer and the reply comes back")

m = send(scout, "e2e browse: data:text/html,<h1>Hello Drok</h1><button>Press me</button>")
check(wait_for(scout, r"Interactive elements:.*button.*Press me", m), "browser goto + DOM snapshot over CDP")

m = send(scout, "e2e screen")
check(wait_for(scout, r"Screen \d after screenshot", m), "computer screenshot")

m = send(forge, "e2e push")
html = wait_for(forge, r'data-approval-id="(\d+)"', m)
check(html is not None, "git push parks for approval")
aid = re.findall(r'data-approval-id="(\d+)"', html)[-1]
c.post(f"/approvals/{aid}", data={"decision": "once", "args": ""}, headers={"x-requested-with": "fetch"})
check(wait_for(forge, r"Tool said:.*exit code", m, 30), "approved call resumes and runs")

m = send(chief, "e2e handoff")
check(wait_for(chief, r"Teammate reported back", m, 60), "handoff to forge and reply back to chief")

r = c.post("/routines", data={"agent_id": "forge", "name": "smoke", "instructions": "e2e shell: echo routine-ok",
                              "kind": "schedule", "cron": "0 3 * * *", "tz": "UTC"})
check(r.status_code == 303, "routine created")
rid = re.findall(r'action="/routines/(\d+)/test"', c.get("/routines").text)[-1]
m = latest(forge)
c.post(f"/routines/{rid}/test")
check(wait_for(forge, r"Tool said:\s*```\s*&lt;untrusted source=&#34;shell&#34;&gt;\s*exit code: 0\s*routine-ok", m),
      "routine test run executes")

r = c.post("/routines", data={"agent_id": "forge", "name": "hook", "instructions": "e2e shell: echo hook-ok",
                              "kind": "webhook", "match": "deploy"})
page = c.get("/routines").text
url = re.findall(r'value="[^"]*?(/hooks/\d+\?secret=[^"]+)"', page)[-1]
check(httpx.post(BASE + url, content="nothing here").json()["fired"] is False, "webhook filter ignores non-matching")
check(httpx.post(BASE + url.split("?")[0], content="deploy").status_code == 404, "webhook without secret rejected")
m = latest(forge)
check(httpx.post(BASE + url, content="deploy done").json()["fired"] is True, "webhook fires on match")
check(wait_for(forge, r"exit code: 0\s*hook-ok", m), "webhook-triggered run executes")

# ---------- harness ----------
m = send(forge, "e2e plan")
check(wait_for(forge, r'class="plan".*1/2 done', m), "plan renders as a live checklist")

m = send(scout, "e2e delegate")
check(wait_for(scout, r'class="wtag">w1.*Worker 1.*worker-one.*Worker 2.*worker-two', m, 90),
      "delegate runs two isolated workers and returns their reports")

m = send(forge, "e2e makefile")
check(wait_for(forge, r"Created /workspace/e2e/report.pdf", m, 90), "make_file renders markdown to PDF")
check(c.get("/files/e2e/report.pdf", params={"raw": 1}).content[:4] == b"%PDF", "the PDF is a real PDF")
m = send(forge, "e2e sheet")
check(wait_for(forge, r"Created /workspace/e2e/data.xlsx", m), "make_file builds a spreadsheet")

m = send(scout, "e2e extract: https://example.com")
check(wait_for(scout, r"Title: Example Domain", m, 60), "web_extract scrapes a page into markdown")

m = send(chief, "e2e memory - note that my favourite colour is orange")
check(wait_for(chief, r"Remembered", m), "memory add")
m = send(scout, "e2e recall")
check(wait_for(scout, r"is orange", m), "recall finds it in another agent's conversation")

m = send(chief, "e2e clarify")
check(wait_for(chief, r'data-answer.*Blue', m), "clarify asks with option buttons")
m = send(chief, "Blue")
check(wait_for(chief, r"Mock reply to: Blue", m), "the answer reaches the agent")

m = send(forge, "e2e bg")
check(wait_for(forge, r"finished \(exit 0\).*bg-done", m, 60), "background process runs and its log is read back")

m = send(forge, "e2e tool")
html = wait_for(forge, r'<strong>Forge</strong> wants to run <code>tool_create', m)
check(html is not None, "building a tool asks the owner first")
aid = re.findall(r'data-approval-id="(\d+)"', html)[-1]
c.post(f"/approvals/{aid}", data={"decision": "once", "args": ""}, headers={"x-requested-with": "fetch"})
check(wait_for(forge, r"registered for the whole team", m), "self-built tool registered after a passing test run")
check("e2e_echo" in c.get("/settings").text, "self-built tool listed in settings")

send(forge, "e2e goal")
end, status = time.time() + 120, ""
while time.time() < end:
    found = re.search(r'<span class="tag \w+">(\w+)</span>\s*<h3><a href="/goals/(\d+)">#\d+ E2E goal',
                      c.get("/goals").text)
    status = found.group(1) if found else ""
    if status == "done":
        break
    time.sleep(2)
check(status == "done", f"goal ran across turns and passed verification (status: {status or 'missing'})")
goal_page = c.get(f"/goals/{found.group(2)}").text
check("PASSED" in goal_page, "verifier verdict recorded on the goal timeline")
check(c.get("/files/e2e-goal.txt", params={"raw": 1}).text == "goal done", "goal's deliverable exists")

# ---------- files in, owner commands, hiring ----------
m = latest(forge)
c.post(f"/c/{forge}/send", data={"text": "e2e shell: cat /workspace/inbox/*_upload_e2e.txt"},
       files={"files": ("upload e2e.txt", b"uploaded-42")})
check(wait_for(forge, r"/workspace/inbox/\d+_upload_e2e\.txt.*uploaded-42", m), "uploaded file reaches the computer")

m = latest(forge)
check(c.post(f"/c/{forge}/send", data={"text": "/model mock/m2"}).json() == {"ok": True}, "/model is a command")
check(wait_for(forge, r"Forge now on mock/m2", m, 10), "/model tests the model and switches")
m = latest(forge)
c.post(f"/c/{forge}/send", data={"text": "/new"})
check(wait_for(forge, r"see the messages above", m, 10), "/new starts a fresh conversation")

m = send(chief, "e2e hire")
html = wait_for(chief, r'data-approval-id="(\d+)"', m)
check(html is not None and "agent_create" in html, "hiring an agent asks the owner first")
aid = re.findall(r'data-approval-id="(\d+)"', html)[-1]
c.post(f"/approvals/{aid}", data={"decision": "once", "args": ""}, headers={"x-requested-with": "fetch"})
check(wait_for(chief, r"Created Ledger", m, 30) and c.get("/agents/ledger").status_code == 200,
      "lead agent hires a new teammate")

for path in ("/computer", "/approvals", "/routines", "/skills", "/settings", "/activity", "/files", "/agents",
             "/goals", "/agents/forge"):
    check(c.get(path).status_code == 200, f"page {path}")
print("\nAll smoke checks passed.")
