import SwiftUI

/// 工作台优先，记录与配置保留在次级分组。
struct RootView: View {
    @EnvironmentObject private var model: AppViewModel
    @ObservedObject private var scheduler = AppRuntime.shared.scheduler
    @Environment(\.colorScheme) private var colorScheme
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
            .safeAreaInset(edge: .bottom, spacing: 0) { automationStatus }
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
                    .font(.kpSmall).monospacedDigit()
                    .foregroundStyle(.primary)
                    .padding(.horizontal, Spacing.s).padding(.vertical, Spacing.xs)
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
        if model.globalPaused {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await model.toggleGlobalPause() }
                } label: {
                    HStack(spacing: Spacing.xs) {
                        Image(systemName: "play.fill").foregroundStyle(Color.kpOrange)
                        Text("已暂停 · 恢复").foregroundStyle(.primary)
                    }
                    .font(.kpLabel)
                    .padding(.horizontal, Spacing.m).padding(.vertical, Spacing.s)
                    .background(Color.kpOrange.opacity(colorScheme == .dark ? 0.20 : 0.14), in: Capsule())
                }
                .buttonStyle(.plain)
                .help("全局已暂停：不再自动领取新任务。点击恢复自动推进")
                .accessibilityLabel("已暂停，恢复自动推进")
                .disabled(pauseDisabled)
            }
        }
    }

    private var pauseDisabled: Bool {
        model.busy || !model.coreFound || model.overview == nil || model.banner != nil
    }

    private var automationStatus: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("自动推进").font(.kpSmall).foregroundStyle(Color.kpSecondary)
            HStack(spacing: Spacing.xs) {
                Circle().fill(scheduler.presentation.tone.color).frame(width: 8, height: 8)
                    .accessibilityHidden(true)
                Text(scheduler.presentation.text).font(.kpBody)
                    .help(scheduler.error ?? scheduler.presentation.text)
            }
            if let error = scheduler.error {
                Text(error).font(.kpLabel).foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            Text(scheduler.status?.paused?.global == true
                 ? "不再领取新任务，在途 \(scheduler.status?.automation?.activeTasks ?? 0) 个照常"
                 : "在途 \(scheduler.status?.automation?.activeTasks ?? 0) · \(lastRefreshClock) 更新")
                .font(.kpSmall).foregroundStyle(Color.kpSecondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: Spacing.s) {
                Button(model.globalPaused ? "恢复" : "暂停") {
                    Task { await model.toggleGlobalPause() }
                }
                .disabled(pauseDisabled)
                .help(model.globalPaused ? "恢复自动推进" : "暂停后不再自动领取新任务，已在执行的任务不受影响")
                .accessibilityLabel(model.globalPaused ? "恢复自动推进" : "暂停自动推进")
                Button {
                    Task { await model.refreshAll() }
                } label: { Label("刷新", systemImage: "arrow.clockwise") }
                .disabled(model.busy)
                .help(refreshHelp)
                .accessibilityValue(lastRefreshFull ?? "")
            }
            .font(.kpLabel).controlSize(.small)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Spacing.m)
        .background(Color.kpControl)
    }

    private var lastRefreshClock: String {
        model.lastRefreshAt.map { Self.clockFormatter.string(from: $0) } ?? "尚未"
    }

    /// 上次刷新的完整时间（提示与无障碍值）；从未刷新时为 nil。
    private var lastRefreshFull: String? {
        model.lastRefreshAt.map { TimeFormat.fullString(ISO8601DateFormatter().string(from: $0)) }
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
