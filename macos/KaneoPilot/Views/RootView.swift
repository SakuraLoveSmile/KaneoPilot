import SwiftUI

/// 工作台优先，记录与配置保留在次级分组。
struct RootView: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var section: AppViewModel.Section = .dashboard
    @State private var settingsSegment: SettingsSegment = .projects
    @ObservedObject private var scheduler = AppRuntime.shared.scheduler
    @Environment(\.colorScheme) private var colorScheme

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
            .safeAreaInset(edge: .bottom) { automationBlock }
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
        .onChange(of: model.attentionTaskToOpen) { _, id in
            if id != nil { section = .dashboard }
        }
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
                Text("\(model.attentionCount)")
                    .font(.kpSmall)
                    .padding(.horizontal, Spacing.s)
                    .padding(.vertical, Spacing.xs)
                    .background(Color.kpOrange.opacity(colorScheme == .dark ? 0.20 : 0.14), in: Capsule())
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
        // 命令不可用原因提示（ui-spec §2）：平时不占位，仅在命令进行中、横幅或核心缺失时出现
        if model.actionInFlight != nil || model.banner != nil || !model.coreFound {
            ToolbarItem(placement: .status) {
                Group {
                    if let action = model.actionInFlight {
                        Label("正在执行：\(action)", systemImage: "hourglass")
                            .labelStyle(.titleAndIcon)
                            .padding(.horizontal, Spacing.m)
                            .padding(.vertical, Spacing.s)
                            .fixedSize(horizontal: true, vertical: false)
                    } else if let banner = model.banner {
                        Text(banner.message)
                    } else {
                        Text("未找到调度核心")
                    }
                }
                .font(.kpSmall)
                .foregroundStyle(Color.kpSecondary)
                .lineLimit(1)
            }
        }
        // 全局暂停只在工具栏留一个状态胶囊（ui-spec §2）；暂停与刷新入口在侧栏底部，⌘R 在 App 菜单。
        if model.globalPaused {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await model.toggleGlobalPause() }
                } label: {
                    Label {
                        Text("已暂停 · 恢复")
                            .foregroundStyle(.primary)
                    } icon: {
                        Image(systemName: "pause.circle.fill")
                            .foregroundStyle(Color.kpOrange)
                    }
                    .font(.kpSmall)
                    .padding(.horizontal, Spacing.s)
                    .padding(.vertical, Spacing.xs)
                    .background(Color.kpOrange.opacity(colorScheme == .dark ? 0.20 : 0.14), in: Capsule())
                }
                .buttonStyle(.plain)
                .help("全局已暂停：不再自动领取新任务。点击恢复自动推进")
                .accessibilityLabel("已暂停，恢复自动推进")
                .disabled(model.busy || !model.coreFound || model.overview == nil || model.banner != nil)
            }
        }
    }

    /// 上次刷新的完整时间（提示与无障碍值）；从未刷新时为 nil。
    private var lastRefreshFull: String? {
        model.lastRefreshAt.map { TimeFormat.fullString(ISO8601DateFormatter().string(from: $0)) }
    }

    /// 侧栏底部「自动推进」状态块（ui-spec §2）：状态词与菜单栏同源；按钮沿用工具栏原有的禁用条件。
    private var automationBlock: some View {
        let headline = scheduler.automationHeadline
        return VStack(alignment: .leading, spacing: Spacing.s) {
            Label {
                Text(headline.text)
                    .font(.kpBody.weight(.semibold))
                    .foregroundStyle(.primary)
            } icon: {
                Circle()
                    .fill(headline.tone.color)
                    .frame(width: 8, height: 8)
            }
            Text(automationDetail)
                .font(.kpSmall)
                .foregroundStyle(Color.kpSecondary)
                .lineLimit(1)
            // 两个等宽按钮撑满侧栏宽度，各带图标（ui-spec §2）。
            HStack(spacing: Spacing.s) {
                Button {
                    Task { await model.toggleGlobalPause() }
                } label: {
                    Label(model.globalPaused ? "恢复" : "暂停",
                          systemImage: model.globalPaused ? "play.fill" : "pause.fill")
                        .frame(maxWidth: .infinity)
                }
                .disabled(model.busy || !model.coreFound || model.overview == nil || model.banner != nil)
                Button {
                    Task { await model.refreshAll() }
                } label: {
                    Label("刷新", systemImage: "arrow.clockwise")
                        .frame(maxWidth: .infinity)
                }
                .help(refreshHelp)
                .disabled(model.busy)
            }
            .controlSize(.small)
        }
        .padding(Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .top) { Divider() }
    }

    /// 在途数与最近刷新时间；已暂停时说明不再领取新任务。
    private var automationDetail: String {
        let active = scheduler.status?.automation?.activeTasks ?? 0
        if model.globalPaused { return "不再领取新任务，在途 \(active) 个照常" }
        guard let at = model.lastRefreshAt else { return "在途 \(active)" }
        return "在途 \(active) · \(Self.clockFormatter.string(from: at)) 更新"
    }

    private var refreshHelp: String {
        guard let full = lastRefreshFull else { return "刷新（⌘R）" }
        return "刷新（⌘R）· 上次刷新 \(full)"
    }

    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()
}
