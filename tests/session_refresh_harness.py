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
    env = dict(ISOLATED_ENV, KANPILOT_DB=str(db))
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

def guarded_ack(db, payload):
    """A separate real CLI process with expensive reading paths forbidden."""
    program = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from kanpilot import cli, cmd_session, dsh_events, headless_adapter, session_store
def forbidden(*args, **kwargs):
    raise AssertionError("acknowledged source attempted full read/preparation")
for module, names in (
    (dsh_events, ("iter_events", "_read_events", "to_messages")),
    (headless_adapter, ("slice_messages",)),
    (cmd_session, ("_load_snapshot", "_save_snapshot")),
    (session_store, ("decode_revisions",)),
):
    for name in names:
        setattr(module, name, forbidden)
print(json.dumps(cli.run("session-messages", json.loads(sys.argv[3]), Path(sys.argv[2]))))
'''
    result = subprocess.run(
        [sys.executable, "-c", program, str(REPO / "core"), str(db), json.dumps(payload)],
        capture_output=True, text=True, env=ISOLATED_ENV,
    )
    return json.loads(result.stdout) if result.returncode == 0 else {}, result.returncode

def need(r, key):
    if not isinstance(r, dict) or key not in r:
        print("UNEXPECTED RESPONSE:", json.dumps(r, ensure_ascii=False)[:800])
        raise SystemExit(2)
    return r

tmp = Path(tempfile.mkdtemp(prefix="kp-refresh-"))
ISOLATED_ENV = dict(os.environ, **{
    "KANPILOT_APP_SUPPORT": str(tmp / "app-support"),
    "KANPILOT_DB": str(tmp / "k.db"),
    "KANPILOT_LEGACY_ROOT": str(tmp / "legacy"),
    "KANPILOT_PLAN_ROOT": str(tmp / "plans"),
    "KANPILOT_REPOS_ROOT": str(tmp / "repos"),
    "KANPILOT_REPORTS_ROOT": str(tmp / "reports"),
    "KANEO_CREDENTIALS_PATH": str(tmp / "credentials.json"),
})
for name in ("app-support", "legacy", "plans", "repos", "reports", "backups"):
    (tmp / name).mkdir()
(tmp / "credentials.json").write_text("{}", encoding="utf-8")
db = tmp / "k.db"; root = tmp / "runs"; run_dir = root / TASK_ID / RUN_ID
run_dir.mkdir(parents=True)
migration, rc = cli(db, "migrate", {
    "legacy_root": str(tmp / "legacy"), "plan_root": str(tmp / "plans"),
    "backup_root": str(tmp / "backups"),
})
if rc:
    raise RuntimeError("isolated migration failed: " + repr(migration))
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
version = r["source_version"]
check("B 稳定来源版本", isinstance(version, str) and len(version) == 64)
checked = r["session"]["last_checked_at"]
r, guarded_rc = guarded_ack(db, {"task_id": TASK_ID, "cursor": cursor,
                                 "source_version": version})
check("B 快返不读全文/快照/revision map", guarded_rc == 0)
need(r, "session")
check("B 独立进程末端确认", r["messages"] == [] and not r["has_more"]
      and r["next_cursor"] == cursor and r["source_version"] == version
      and r["session"]["last_checked_at"] > checked)
r, _ = cli(db, "session-messages", {"task_id": TASK_ID, "cursor": cursor,
                                    "source_version": version, "force_reload": True})
check("B 强制刷新保留游标与版本", r["source_version"] == version
      and not r["has_more"])

# C. 追加一条事件 → 增量只回新消息、无重复
events = [json.loads(l) for l in ef.read_text().splitlines() if l.strip()]
write_events(events + [ev("assistant_message", 1, content="done")])
r, _ = cli(db, "session-messages", {"task_id": TASK_ID, "cursor": cursor,
                                    "source_version": version})
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
check("E 失败不确认版本", r["source_version"] is None)
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

# I. >2MB: paginate across independent processes before acknowledging the tail.
large_run = "hrun-large"
large = root / TASK_ID / large_run
large.mkdir()
(large / "job.json").write_text(json.dumps(
    {"task_id": TASK_ID, "run_id": large_run, "backend": "pi"}))
large_events = [dict(v=1, seq=i, ts="2026-09-21T09:00:00+00:00",
                     type="user", content=(f"message-{i}:" + "x" * 65536))
                for i in range(1, 41)]
large_file = large / "events.jsonl"
large_file.write_text("".join(json.dumps(e) + "\n" for e in large_events))
loaded = []
request = {"task_id": TASK_ID, "run_id": large_run, "limit": 7}
while True:
    page, rc = cli(db, "session-messages", request)
    need(page, "messages")
    loaded.extend(page["messages"])
    if not page["has_more"]:
        break
    request["cursor"] = page["next_cursor"]
check("I >2MB 独立进程连续分页完整", large_file.stat().st_size > 2_000_000
      and [m["seq"] for m in loaded] == list(range(1, 41)))
conn = sqlite3.connect(db)
meta = json.loads(conn.execute("SELECT value FROM settings WHERE key=?",
                             ("session_snapshot_meta:" + TASK_ID + "/" + large_run,)).fetchone()[0])
conn.close()
check("I 大会话仅小meta无全文快照", meta["full_snapshot_available"] is False)
ack, rc = guarded_ack(db, {"task_id": TASK_ID, "run_id": large_run,
                          "cursor": page["next_cursor"], "source_version": page["source_version"]})
check("I 大会话末端快返不读全文", rc == 0 and ack.get("messages") == []
      and ack.get("next_cursor") == page["next_cursor"]
      and ack.get("source_version") == page["source_version"])
reopen, _ = cli(db, "session-messages", {"task_id": TASK_ID, "run_id": large_run, "limit": 500})
check("I 大会话重开仍读完整历史", len(reopen["messages"]) == 40)

shutil.rmtree(tmp, ignore_errors=True)
fails = [n for n, ok, _ in results if not ok]
print(f"\n== {len(results) - len(fails)}/{len(results)} PASS ==")
sys.exit(1 if fails else 0)
