#!/usr/bin/env python3
"""批次處理所有還開著的「新增／刪除 ETF」Issue。

GitHub 同一個 concurrency 群組只保留一個排隊中的執行，連續開多張 Issue 時，
前面排隊的會被取消。所以不只處理觸發的那一張，而是每次都把所有開著的 Issue 一起處理，
被取消的也會在下一次（任何一次新增或排程更新）補做。

用法：
  python3 scripts/process_issues.py apply   # 依序修改清單，結果存到 ../issues_result.json
  python3 scripts/process_issues.py reply   # 推送成功後，回覆並關閉這些 Issue
需要環境變數 GITHUB_REPOSITORY、GITHUB_REPOSITORY_OWNER、TOKEN。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

RESULT = Path("../issues_result.json")
TITLE_RE = re.compile(r"^\s*(新增|刪除|移除)")


def api(method: str, path: str, data=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}",
        data=json.dumps(data).encode() if data is not None else None, method=method,
        headers={"Authorization": f"Bearer {os.environ['TOKEN']}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else None


def pending_issues():
    owner = os.environ["GITHUB_REPOSITORY_OWNER"]
    items = api("GET", f"/issues?state=open&creator={owner}&per_page=100&sort=created&direction=asc") or []
    return [i for i in items if "pull_request" not in i and TITLE_RE.match(i.get("title") or "")]


def apply() -> int:
    done = []
    for i in pending_issues():
        p = subprocess.run([sys.executable, "scripts/manage.py", i["title"], i.get("body") or ""],
                           capture_output=True, text=True)
        msg = p.stdout.strip() or "❌ 執行失敗，請到 Actions 查看紀錄。"
        print(f"#{i['number']} {i['title']} → rc={p.returncode}\n{msg}\n{p.stderr.strip()}")
        done.append({"number": i["number"], "title": i["title"], "rc": p.returncode, "msg": msg})
    RESULT.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    changed = [d for d in done if d["rc"] == 0]
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as fh:
            fh.write(f"ISSUES_CHANGED={' '.join('#' + str(d['number']) for d in changed)}\n")
    print(f"共 {len(done)} 張 Issue，{len(changed)} 張有變動")
    return 0


def reply() -> int:
    if not RESULT.exists():
        return 0
    for d in json.loads(RESULT.read_text(encoding="utf-8")):
        body = d["msg"] + ("\n\n網站約 1–2 分鐘後更新，手機重新整理即可看到。" if d["rc"] == 0 else "")
        try:
            api("POST", f"/issues/{d['number']}/comments", {"body": body})
            api("PATCH", f"/issues/{d['number']}", {"state": "closed"})
        except Exception as e:  # noqa: BLE001
            print(f"#{d['number']} 回覆失敗：{e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(apply() if cmd == "apply" else reply() if cmd == "reply" else 2)
