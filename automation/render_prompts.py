#!/usr/bin/env python3
"""Render the four executor automation.toml files from prompts/*.md + _preamble.md.

Usage: python3 render_prompts.py [--out DIR]
Writes <out>/<executor-id>.toml next to the prompts (default: automation/rendered/).
The {PREAMBLE} placeholder inside each prompt is replaced by _preamble.md content.
Does NOT touch ~/.codex — the cutover step copies/reviews these files.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
OUT = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else HERE / "rendered"

# id -> (name, rrule, model, reasoning_effort, cwds)
EXECUTORS = {
    "kaneo-plan":     ("Kaneo 计划生成",      "RRULE:FREQ=HOURLY;BYMINUTE=0,15,30,45;BYSECOND=0",   "gpt-6-astra", "high",   ["/Users/sakurasep/Documents/Code/Plan"]),
    "kaneo-dispatch": ("Kaneo 计划派发 Devin", "RRULE:FREQ=HOURLY;BYMINUTE=4,19,34,49;BYSECOND=0",  "gpt-5.6-luna", "max",    ["/Users/sakurasep/Documents/Code/Plan"]),
    "kaneo-monitor":  ("Kaneo 执行监控与收尾", "RRULE:FREQ=HOURLY;BYMINUTE=8,23,38,53;BYSECOND=0",  "gpt-5.6-luna", "max",    ["/Users/sakurasep/Documents/Code/Plan"]),
    "kaneo-review":   ("Kaneo 评审",          "RRULE:FREQ=HOURLY;BYMINUTE=12,27,42,57;BYSECOND=0", "gpt-5.6-sol",  "xhigh",  ["/Users/sakurasep/Documents/Code/Plan"]),
}

TARGET = 'target = { type = "project", project_id = "77370c88-9e3e-4a38-bd47-aa62d52f63bb" }'


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def main() -> None:
    preamble = (PROMPTS / "_preamble.md").read_text(encoding="utf-8").strip()
    OUT.mkdir(parents=True, exist_ok=True)
    for executor_id, (name, rrule, model, effort, cwds) in EXECUTORS.items():
        body = (PROMPTS / f"{executor_id}.md").read_text(encoding="utf-8")
        assert "{PREAMBLE}" in body, f"{executor_id}.md missing placeholder"
        prompt = body.replace("{PREAMBLE}", preamble)
        toml = "\n".join([
            "version = 1",
            f'id = "{executor_id}"',
            'kind = "cron"',
            f'name = "{name}"',
            f'prompt = "{toml_escape(prompt)}"',
            'status = "ACTIVE"',
            f'rrule = "{rrule}"',
            f'model = "{model}"',
            f'reasoning_effort = "{effort}"',
            'execution_environment = "local"',
            TARGET,
            "cwds = [" + ", ".join(f'"{c}"' for c in cwds) + "]",
            "",
        ])
        (OUT / f"{executor_id}.toml").write_text(toml, encoding="utf-8")
        print(f"rendered {executor_id}.toml ({len(prompt)} chars)")


if __name__ == "__main__":
    main()
