import SwiftUI

/// 工作台优先，记录与配置保留在次级分组。
struct RootView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var section: AppViewModel.Section = .dashboard
    @State private var settingsSegment: SettingsSegment = .projects

    var body: some View {
        NavigationSplitView {
            List(selection: $section) {
                Section("工作台") {
                    sidebarItem(.dashboard)
                    sidebarItem(.tasks)
                    sidebarItem(.pending)
                }
                Section("记录") {
                    sidebarItem(.sessions)
                    sidebarItem(.runs)
                    sidebarItem(.archive)
                    sidebarItem(.trash)
                }
                Section("配置") { sidebarItem(.settings) }
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 150, ideal: 180, max: 220)
        } detail: {
            VStack(spacing: 0) {
                if let banner = model.banner {
                    BannerView(banner: banner, stale: model.hasData) {
                        Task { await model.refreshAll() }
                    }
                }
                sectionView
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .navigationTitle("Kaneo 调度台")
            .toolbar { toolbar }
            .overlay(alignment: .top) {
                if let toast = model.toast {
                    ToastView(text: toast)
                        .padding(.top, model.banner != nil ? 56 : Spacing.s)
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
            }
            .animation(.easeInOut(duration: 0.2), value: model.toast)
        }
        // 由窗口根呈现：需要处理的导航详情也能打开，避免列内 presenter 被 push 页面占用。
        .sheet(isPresented: $model.planningPresented) {
            PlanningView()
                .environmentObject(model)
                .frame(minWidth: 760, idealWidth: 760, minHeight: 600)
        }
        .task { await model.refreshAll() }
        .task { await model.loadSaved() }
        .task { await pollLoop() }
        .onChange(of: section) { _, newSection in
            // 归档/回收站/会话节按需加载（与看板周期刷新解耦）
            switch newSection {
            case .archive, .trash, .sessions:
                Task { await model.loadSaved() }
            default:
                break
            }
        }
        .alert("操作失败", isPresented: alertPresented) {
            Button("好", role: .cancel) { model.alertMessage = nil }
        } message: {
            Text(model.alertMessage ?? "")
        }
    }

    @ViewBuilder
    private func sidebarItem(_ item: AppViewModel.Section) -> some View {
        HStack {
            Label(item.title, systemImage: item.icon)
            if item == .pending && model.attentionCount > 0 {
                Spacer(minLength: Spacing.s)
                Text("\(model.attentionCount)").font(.kpSmall).foregroundStyle(.secondary)
            }
        }
        .tag(item)
        .accessibilityLabel(item == .pending ? "\(item.title)，\(model.attentionCount) 项" : item.title)
    }

    @ViewBuilder
    private var sectionView: some View {
        switch section {
        case .dashboard:
            AutomationDashboardView(onTasks: { column in
                model.taskColumnFilter = column?.rawValue
                section = .tasks
            }, onPending: { section = .pending }, onSettings: { group in
                settingsSegment = group
                section = .settings
            })
        case .tasks:
            TasksSectionView()
        case .sessions:
            SessionsListView()
        case .pending:
            PendingView()
        case .archive:
            SavedTasksView(mode: .archive)
        case .trash:
            SavedTasksView(mode: .trash)
        case .runs:
            RunsView()
        case .settings:
            SettingsView(initialSegment: settingsSegment)
        }
    }

    // 周期刷新保持数据新鲜；窗口重开时 .task 重新执行。
    private func pollLoop() async {
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(30))
            guard !Task.isCancelled else { return }
            await model.refreshAll()
        }
    }

    private var alertPresented: Binding<Bool> {
        Binding(get: { model.alertMessage != nil },
                set: { if !$0 { model.alertMessage = nil } })
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        // 命令不可用原因提示（ui-spec §2）
        ToolbarItem(placement: .status) {
            Group {
                if let action = model.actionInFlight {
                    Label("正在执行：\(action)", systemImage: "hourglass")
                } else if let banner = model.banner {
                    Text(banner.message)
                } else if let at = model.lastRefreshAt {
                    Text("更新于 \(TimeFormat.fullString(ISO8601DateFormatter().string(from: at)))")
                        .accessibilityLabel("上次刷新时间")
                } else if !model.coreFound {
                    Text("未找到调度核心")
                }
            }
            .font(.kpSmall)
            .foregroundStyle(.secondary)
            .lineLimit(1)
        }
        if section != .dashboard {
            ToolbarItem(placement: .primaryAction) {
                // 暂停/恢复自动推进（全局态）
                Button {
                    Task { await model.toggleGlobalPause() }
                } label: {
                    Label(model.globalPaused ? "恢复自动推进" : "暂停自动推进",
                          systemImage: model.globalPaused ? "play.fill" : "pause.fill")
                }
                .labelStyle(.titleAndIcon)
                .help(model.globalPaused ? "恢复自动推进（当前全局暂停）" : "暂停自动推进")
                .accessibilityLabel(model.globalPaused ? "恢复自动推进" : "暂停自动推进")
                .disabled(model.busy || !model.coreFound || model.overview == nil || model.banner != nil)
            }
        }
        ToolbarItem(placement: .primaryAction) {
            Button {
                Task { await model.refreshAll() }
            } label: {
                Label("刷新", systemImage: "arrow.clockwise")
            }
            .labelStyle(.iconOnly)
            .help("刷新（⌘R）")
            .accessibilityLabel("刷新")
            .keyboardShortcut("r", modifiers: .command)
            .disabled(model.busy)
        }
    }
}
