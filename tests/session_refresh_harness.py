#!/usr/bin/env python3
"""T2 集成验收：pi 会话隔离文件持续更新 → CLI 刷新核验（v1.6）。

不依赖测试套件；只按冻结契约（docs/protocol.md §5.15.5 + §5.18.2）经 CLI
黑盒断言。数据源为本机 pi 轮次目录 ``<runs_root>/<task_id>/<run_id>/`` 下的
``job.json``（身份 + backend=pi）与 ``events.jsonl``（canonical 事件行，
首事件恒为 session）。每次 CLI 调用都是独立进程——「保留上一份有效内容」
靠 session_reads 位点 + settings 跨进程快照兑现，脚本即验证该路径。

用法: python3 tests/session_refresh_harness.py <repo_root>
"""
import json, os, shutil, sqlite3, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
CORE = REPO / "core" / "kanpilot.py"
TASK_ID, RUN_ID = "htest-task-1", "hrun-0001"
results = []

def check(name, cond, evidence=""):
    results.append((name, bool(cond), evidence))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{evidence}]" if evidence and not cond else ""))

def cli(db, cmd, payload):
    env = dict(os.environ, KANPILOT_DB=str(db))
    r = subprocess.run([sys.executable, str(CORE), cmd, "--json", json.dumps(payload)],
                       capture_output=True, text=True, env=env)
    try:
        out = json.loads(r.stdout)
    except Exception:
        out = {"_raw": r.stdout, "_err": r.stderr}
    if r.returncode != 0:
        out.setdefault("_rc", r.returncode)
        out.setdefault("_err", r.stderr[-500:])
    return out.get("result", out), r.returncode

def need(r, key):
    if not isinstance(r, dict) or key not in r:
        print("UNEXPECTED RESPONSE:", json.dumps(r, ensure_ascii=False)[:800])
        raise SystemExit(2)
    return r

tmp = Path(tempfile.mkdtemp(prefix="kp-refresh-"))
db = tmp / "k.db"; root = tmp / "runs"; run_dir = root / TASK_ID / RUN_ID
run_dir.mkdir(parents=True)
subprocess.run([sys.executable, str(CORE), "migrate", "--json", json.dumps({})],
               env=dict(os.environ, KANPILOT_DB=str(db)), capture_output=True)
conn = sqlite3.connect(db)
cols = {r[1]: r for r in conn.execute("PRAGMA table_info(tasks)")}
seed = {"task_id": TASK_ID, "run_id": RUN_ID, "status": "running"}
if "title" in cols: seed["title"] = "refresh harness"
for name, info in cols.items():
    _, _, _t, notnull, dflt, _pk = info
    if name not in seed and notnull and dflt is None:
        seed[name] = "hp" if "id" in name or "sha" in name else "2026-09-21T00:00:00+00:00"
keys = ", ".join(seed); ph = ", ".join("?" * len(seed))
conn.execute(f"INSERT INTO tasks ({keys}) VALUES ({ph})", list(seed.values()))
conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('session_runs_root', ?)",
             (json.dumps(str(root)),))
conn.commit(); conn.close()

(run_dir / "job.json").write_text(json.dumps(
    {"task_id": TASK_ID, "run_id": RUN_ID, "backend": "pi",
     "status": "running", "schema_version": 1}))

SEQ = [0]
def ev(etype, ts_minute, **fields):
    """canonical events.jsonl 行（dsh_events 词汇，§5.18.2）。seq 单调。"""
    SEQ[0] += 1
    return {"v": 1, "seq": SEQ[0],
            "ts": f"2026-09-21T{ts_minute:02d}:{SEQ[0] % 60:02d}:00+00:00",
            "type": etype, **fields}

def write_events(events):
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8")

ef = run_dir / "events.jsonl"
# A. 无 events.jsonl → pending（等待事件日志），非“暂无消息”
r, _ = cli(db, "session-messages", {"task_id": TASK_ID})
r = need(r, "session")
check("A 缺文件=pending", r["session"]["state"] == "pending" and r["messages"] == [],
      json.dumps(r, ensure_ascii=False)[:200])

# B. 首载完整历史（首事件恒为 session → system 消息）
write_events([
    ev("session", 0, session_id="s1", session_title="harness"),
    ev("user", 0, content="start"),
    ev("assistant_message", 0, content="thinking"),
    ev("tool_call", 0, name="exec", arguments={"command": "ls"}),
    ev("command_output", 0, name="exec", input=json.dumps({"command": "ls"}),
       output="file list", exit_code=0),
])
r, _ = cli(db, "session-messages", {"task_id": TASK_ID})
types = [m["type"] for m in r["messages"]]
check("B 首载=user+assistant+tool_call+工具输出",
      {"user", "assistant", "tool_call"} <= set(types)
      and set(types) & {"tool_result", "command_output"}, str(types))
cursor = r["next_cursor"]
ids1 = {m["id"]: m["revision"] for m in r["messages"]}

# C. 追加一条事件 → 增量只回新消息、无重复
events = [json.loads(l) for l in ef.read_text().splitlines() if l.strip()]
write_events(events + [ev("assistant_message", 1, content="done")])
r, _ = cli(db, "session-messages", {"task_id": TASK_ID, "cursor": cursor})
check("C 增量只含新 seq", all(m["seq"] > int(str(cursor).split(":")[-1]) for m in r["messages"])
      and len(r["messages"]) >= 1, json.dumps([m["seq"] for m in r["messages"]]))
check("C 旧消息不重复", all(m["id"] not in ids1 for m in r["messages"]))

# D. 同 seq 重发修订（append-only 语义：同 id 高 revision 覆盖）→ 增量透出更新
events = [json.loads(l) for l in ef.read_text().splitlines() if l.strip()]
revised = dict(events[-1], content="done (updated conclusion)")
write_events(events + [revised])
cursor = r["next_cursor"]
r, _ = cli(db, "session-messages", {"task_id": TASK_ID, "cursor": cursor})
upd = [m for m in r["messages"] if "updated conclusion" in (m.get("content") or "")]
check("D 尾步修订可透出", len(upd) == 1 and upd[0]["revision"] != ids1.get(upd[0]["id"]),
      json.dumps(r["messages"], ensure_ascii=False)[:200])

# E. 半写入文件 → 保留上一份有效内容 + detail 原因，不当作空会话
#    （跨进程快照路径：CLI 每次调用都是新进程，进程内缓存必然为空）
good = ef.read_text()
ef.write_text('{"v":1,"seq":')  # 非法 JSON：有内容但零有效行 → read_failed
r, _ = cli(db, "session-messages", {"task_id": TASK_ID})
check("E 半写入保留内容", len(r["messages"]) > 0,
      json.dumps(r, ensure_ascii=False)[:200])
check("E 半写入带原因", bool((r.get("connection") or {}).get("detail"))
      or r["session"]["state"] == "pending", json.dumps(r.get("connection")))
ef.write_text(good)

# F. 身份不符 → unavailable/失败原因，不读内容
mis = root / TASK_ID / "hrun-evil"
mis.mkdir()
(mis / "job.json").write_text(json.dumps(
    {"task_id": "OTHER", "run_id": "hrun-evil", "backend": "pi"}))
r, _ = cli(db, "session-messages", {"task_id": TASK_ID, "run_id": "hrun-evil"})
check("F 身份不符拒绝", r["session"]["state"] == "unavailable" and not r["messages"]
      and (r.get("connection") or {}).get("detail"), json.dumps(r, ensure_ascii=False)[:200])

# G. 重开（无 cursor）→ 完整历史，条数 >= B 且含更新后的尾步
r, _ = cli(db, "session-messages", {"task_id": TASK_ID})
check("G 重开完整历史", len(r["messages"]) >= len(ids1) + 1
      and any("updated conclusion" in (m.get("content") or "") for m in r["messages"]),
      f"{len(r['messages'])} vs {len(ids1)}")
ids2 = [m["id"] for m in r["messages"]]
check("G 重开无重复 id", len(ids2) == len(set(ids2)))

# H. 轮次隔离：另一 run 目录不串线
other = root / TASK_ID / "hrun-0002"; other.mkdir()
(other / "job.json").write_text(json.dumps(
    {"task_id": TASK_ID, "run_id": "hrun-0002", "backend": "pi"}))
(other / "events.jsonl").write_text(json.dumps(
    {"v": 1, "seq": 1, "ts": "2026-09-21T09:00:00+00:00",
     "type": "user", "content": "only-run2"}) + "\n", encoding="utf-8")
r, _ = cli(db, "session-messages", {"task_id": TASK_ID, "run_id": "hrun-0002"})
check("H 轮次不串线", all("only-run2" in m.get("content", "") or m["type"] != "user"
                          for m in r["messages"])
      and any("only-run2" in m.get("content", "") for m in r["messages"]),
      json.dumps([m.get("content", "")[:30] for m in r["messages"]]))

shutil.rmtree(tmp, ignore_errors=True)
fails = [n for n, ok, _ in results if not ok]
print(f"\n== {len(results) - len(fails)}/{len(results)} PASS ==")
sys.exit(1 if fails else 0)
