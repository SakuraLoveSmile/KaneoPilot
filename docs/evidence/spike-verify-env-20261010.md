# Spike: login shell 下 verify 的环境差异与 `verify_env` 形态 — 2026-10-10

## 结论（一句话）

**在本机，「login shell」必须用 `$SHELL`（`/bin/zsh`）而不是 `/bin/bash`**：用户登录 shell 是 zsh，`~/.zprofile` 里才有 homebrew/pyenv/MacPorts 的 PATH；`~/.bash_profile` 只做 conda + x-cmd，**不含** homebrew 与 pyenv shims。因此 `verify` 若用 `/bin/bash -l -c` 起子进程，`gh` 与 `pytest` 都会 `command not found`（rc=127），而 `/bin/zsh -l -c` 两者都正常。**`verify_env` 的职责就是把这条 PATH（及项目级变量）显式固化下来**，使验证结果不依赖调用方的 shell 与启动方式。

## 采集方法（真实测量）

分三组对比，全部在本机执行：

| 组 | 启动方式 | 继承 PATH |
|---|---|---|
| A | 当前进程（终端里跑） | 完整终端 PATH（含 `/opt/homebrew/bin`、`~/.pyenv/shims`） |
| B | `/bin/bash --noprofile --norc -c` | 与 A **完全一致**（逐字节相同） |
| C | `/bin/bash -l -c` | 与 A **完全一致**（`env \| cut -d= -f1 \| sort` 的 diff 为空） |

A/B/C 相同的原因是：这些子进程是从「已有完整 PATH 的终端进程」派生的，profile 只是重复设置同样的值。**真正有差异的是 GUI 启动场景**：

| 组 | 启动方式 | 继承 PATH | `gh` | `pytest` | `git` |
|---|---|---|---|---|---|
| D | 最小 PATH `/usr/bin:/bin:/usr/sbin:/sbin` + `/bin/bash -l -c` | 最小 | **rc=127** | **rc=127** | rc=0 |
| E | 最小 PATH + **`/bin/zsh -l -c`** | 完整 | rc=0（gh 2.97.0） | rc=0（pytest 9.1.1） | rc=0 |

D 组即 KaneoPilot App 的真实处境：App 由 Finder/launchd 拉起（`launchctl getenv PATH` 为空），子进程只拿到系统最小 PATH。`gh` 在 `/opt/homebrew/bin/gh`、`pytest` 在 `/Users/sakurasep/.pyenv/shims/pytest`，均不在最小 PATH 内。

**D → E 的差别就是本 spike 的核心发现**：同一台机器，换 login shell 从「全挂」变「全通」。

## 证据文件

- `~/.zprofile`：`eval "$(/opt/homebrew/bin/brew shellenv)"`、`/Library/Frameworks/Python.framework/Versions/3.11/bin`、MacPorts——PATH 的真正来源。
- `~/.bash_profile`：只有 conda 初始化、x-cmd、cargo、`~/.local/bin`——**没有** homebrew，**没有** pyenv shims。这解释了 D 组为什么挂。
- `SHELL=/bin/zsh`（用户登录 shell）。
- `python3 -m pytest --version` → `pytest 9.1.1`；`/Users/sakurasep/.pyenv/shims/pytest` 存在。**本机装有真实 pytest**（早前「环境缺 pytest」的判断有误：仓库根 `pytest.py` 是一个透传 shim，会把 `python3 -m pytest` 交给真实 pytest 9.1.1；`tests/pytest.py` 只是无真实 pytest 时的 unittest discover 兜底桩）。

## `verify_env` 形态（结论）

- 位置：项目映射 `project_mappings.items[project_id].verify_env`，**字符串→字符串的 dict**，缺省 `{}`。
- 注入方式：在起 verify 子进程前合并进环境（覆盖同名键），**不写入磁盘、不落库明文、不进日志**。
- 必填项建议：`PATH`。项目若依赖虚拟环境/工具链，还需 `VIRTUAL_ENV`、`NODE_ENV`、`PYTHONPATH` 等。
- **禁止**放凭据：token/密钥走凭据文件（如 Kaneo 的 `~/.config/kaneo-mcp/credentials.json`），不进 `verify_env`。
- 兜底：`verify_env` 未提供 `PATH` 时，用「`$SHELL -l -c` 探到的 PATH」作缺省，而不是硬编码 `/usr/bin:/bin`。

## 对引擎的实现要求（已同步 engine_builder 与 protocol §5.18.6）

1. **默认 verify shell 改为 `$SHELL -l -c`**（读 `os.environ.get("SHELL") or "/bin/zsh"`），`/bin/bash -l -c` 仅作 `$SHELL` 缺失时的兜底；`KANPILOT_VERIFY_SHELL` 可整体覆盖。
2. `verify_env` 合并后再起子进程；缺省 PATH 由 login shell 探测得到。
3. 超时独立可配（`KANPILOT_VERIFY_TIMEOUT`，默认 600s），超时与「命令不存在（rc=127）」必须区分归因：rc=127 是**环境类**失败（阶梯 2），不是质量失败。
