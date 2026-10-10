"""假 gh 可执行（tests/rehearsal 夹具）。

``write_fake_gh(path, faults)`` 生成一个 POSIX shell 桩，经
``KANPILOT_GH_BIN``（constants.gh_bin）指向它；行为按故障注入开关变化：

- 无故障：``--version`` / ``auth status`` / ``pr list|create|view`` 均成功；
- ``gh_timeout``：任何调用挂起 60s（超出 doctor 探针超时）；
- ``gh_not_logged_in``：``auth status`` 退出 1（未登录形态）；
- ``gh_missing``：fixture 层把 KANPILOT_GH_BIN 指向不存在路径（不生成桩）。

桩只回显固定 JSON，绝不访问网络。
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = """#!/bin/sh
# 演练台假 gh（tests/rehearsal/fake_gh.py 生成）。绝不访问网络。
case ",$KANPILOT_REHEARSAL_FAULT," in
    *,gh_timeout,*) sleep 60; exit 0 ;;
esac
case "$1" in
    --version)
        echo "gh version 2.65.0-rehearsal"
        exit 0
        ;;
    auth)
        case "$2" in
            status)
                case ",$KANPILOT_REHEARSAL_FAULT," in
                    *,gh_not_logged_in,*)
                        echo "fake gh: not logged in" >&2
                        exit 1
                        ;;
                esac
                echo "fake gh: logged in as rehearsal-bot"
                exit 0
                ;;
        esac
        exit 0
        ;;
    pr)
        case "$2" in
            list)
                echo '[]'
                exit 0
                ;;
            create)
                echo '{"number": 1, "url": "https://github.com/rehearsal-owner/rehearsal-repo/pull/1", "state": "open", "headRefName": "rehearsal-branch", "baseRefName": "main"}'
                exit 0
                ;;
            view)
                echo '{"number": 1, "state": "open", "merged": false, "url": "https://github.com/rehearsal-owner/rehearsal-repo/pull/1"}'
                exit 0
                ;;
        esac
        exit 0
        ;;
esac
exit 0
"""


def write_fake_gh(path, faults=()) -> Path:
    """在 ``path`` 写假 gh 可执行并返回该路径（调用方负责设 KANPILOT_GH_BIN）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SCRIPT, encoding="utf-8")
    path.chmod(0o755)
    return path
