# KaneoPilot

macOS 调度工作台与 Android 前台控制台。Mac 保存数据、仓库和执行器，手机通过局域网或已有 VPN 查看进度、回答问题、确认计划，以及暂停后续自动推进。

- [手机连接、安装与构建](docs/mobile-console-guide.md)
- [移动接口与安全契约](docs/mobile-console-contract.md)
- [云端构建与 Release 规范](docs/build-release.md)

安装包统一由 [GitHub Actions → Packages](https://github.com/SakuraLoveSmile/KaneoPilot/actions/workflows/packages.yml) 构建，本机不再编译打包。测试包选择分支运行 workflow；正式版本按 annotated `vX.Y.Z` 标签发布，下载入口为 [Releases](https://github.com/SakuraLoveSmile/KaneoPilot/releases)。

## 首次配置 Kaneo 项目

公开源码不包含维护者的工作区或项目标识。先在应用使用的同一本地数据库中配置工作区：

```sh
python3 core/kanpilot.py set-config --json '{"workspace_id":"<你的 Kaneo 工作区 ID>"}'
python3 core/kanpilot.py get-project-mappings --json '{}'
```

如果应用使用了自定义数据库，两条命令都需设置相同的 `KANPILOT_DB`。随后在桌面端打开“项目与仓库配置”，点击“获取 Kaneo 项目”，选择本地仓库并保存。未配置工作区时，项目查询会提示配置，且不会发出请求。

已有 `project_mappings` 和工作区配置继续生效，映射、版本及自动化设置保留。此前仅依赖源码中的默认标识、从未保存配置的安装，需要按上述步骤配置一次。已保存映射绑定的工作区不能通过 `set-config` 切换到其他工作区，以免现有项目被错误复用。
