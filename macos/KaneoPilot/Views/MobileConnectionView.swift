import SwiftUI
import AppKit
import CoreImage.CIFilterBuiltins

struct MobileConnectionView: View {
    @ObservedObject var manager: MobileServiceManager = .shared
    @State private var host = ""
    @State private var port = "47831"
    @State private var seeded = false
    @State private var copied = false
    @State private var changingAddress = false

    private var addresses: [MobileAddress] {
        (manager.configuration?.addresses ?? []).filter {
            !$0.address.hasPrefix("127.") && $0.address != "0.0.0.0"
        }
    }

    private var enabled: Bool { manager.configuration?.enabled == true }
    private var validDraft: Bool {
        addresses.contains { $0.address == host }
            && Int(port).map { (1024...65535).contains($0) } == true
    }

    /// 状态行标题（状态词与原来一致）。
    private var statusText: String {
        manager.starting ? "正在启动…" : manager.running
            ? (manager.isSimulation ? "运行中（隔离模拟，无网络监听）" : "运行中")
            : (enabled ? "已启用，服务未运行" : "已关闭")
    }

    /// 按行拆开（ui-spec §4.9 行结构）：状态、本机地址、操作、配对码、已配对设备各自一行。
    var body: some View {
        Section("手机连接") {
            SettingsRow(statusText,
                        detail: "通过局域网或已有 VPN 连接安卓手机。Mac 保留数据和执行器；关闭窗口后仍可连接，退出 App 后停止。",
                        status: manager.running ? Color.kpGreen : Color.kpGray) {
                HStack(spacing: Spacing.s) {
                    if manager.starting { ProgressView().controlSize(.small) }
                    Button("刷新") { Task { await manager.refresh(); seedDraft() } }
                        .disabled(manager.busy)
                        .accessibilityLabel("刷新手机连接状态")
                }
            }
            LabeledContent {
                HStack(spacing: Spacing.m) {
                    Picker("本机地址", selection: $host) {
                        Text("请选择地址").tag("")
                        ForEach(addresses) { address in
                            Text("\(address.name) · \(address.address)").tag(address.address)
                        }
                        if !host.isEmpty, !addresses.contains(where: { $0.address == host }) {
                            Text("\(host)（当前不可用）").tag(host)
                        }
                    }
                    .labelsHidden()
                    .fixedSize()
                    Text("端口").font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    TextField("端口", text: $port)
                        .labelsHidden()
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 80)
                        .accessibilityLabel("手机连接端口")
                }
                .disabled(enabled || manager.busy || manager.starting)
            } label: {
                SettingsRowLabel(title: "本机地址", detail: manager.configuration?.endpoint, selectable: true)
            }
            if manager.configuration != nil && addresses.isEmpty {
                Text("未发现可用的本机 IPv4 地址，请先连接局域网或 VPN 后刷新。")
                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
            }
            HStack(spacing: Spacing.s) {
                if enabled {
                    Button("停用手机连接") {
                        Task { await manager.configure(enabled: false, host: host, port: Int(port) ?? 47831) }
                    }.accessibilityLabel("停用手机连接")
                    if !manager.running && !manager.starting {
                        Button("重试启动") { Task { await manager.retry() } }
                    }
                } else {
                    Button("启用手机连接") {
                        let previous = manager.configuration?.bindHost
                        if let previous, !previous.isEmpty, previous != host,
                           manager.configuration?.devices.contains(where: { $0.revokedAt == nil }) == true {
                            changingAddress = true
                        } else { enable() }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!validDraft)
                    .accessibilityLabel("启用手机连接")
                }
                if manager.running {
                    Button(manager.pairing == nil ? "生成配对二维码" : "重新生成配对码") {
                        copied = false
                        Task { await manager.createPairing() }
                    }
                    .accessibilityLabel("生成手机配对二维码")
                }
                if enabled {
                    Spacer(minLength: 0)
                    Text("更换地址或端口前请先停用。地址变化将使旧设备失效，需要重新配对。")
                        .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                        .multilineTextAlignment(.trailing)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .disabled(manager.busy || manager.starting)
            if let error = manager.error {
                SettingsNotice(text: error, icon: "exclamationmark.circle.fill", tone: Color.kpRed)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel("手机连接错误：\(error)")
            }
            if let pairing = manager.pairing, manager.running {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    if pairing.isValid(at: context.date) {
                        HStack(alignment: .top, spacing: Spacing.l) {
                            if let image = Self.qrImage(pairing.pairingUri) {
                                Image(nsImage: image).interpolation(.none).resizable()
                                    .frame(width: 220, height: 220)
                                    .padding(Spacing.s).background(.white)
                                    .accessibilityLabel("手机配对二维码，五分钟内有效")
                            }
                            VStack(alignment: .leading, spacing: Spacing.s) {
                                Text("在安卓端扫描，或复制配对内容后粘贴。配对内容包含一次性凭证，请仅交给自己的手机。")
                                    .font(.kpBody).fixedSize(horizontal: false, vertical: true)
                                Text("剩余 \(max(0, Int(pairing.expiration!.timeIntervalSince(context.date)))) 秒；成功配对后失效")
                                    .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                                Button(copied ? "已复制配对内容" : "复制配对内容") {
                                    guard pairing.isValid() else { return }
                                    NSPasteboard.general.clearContents()
                                    NSPasteboard.general.setString(pairing.pairingUri, forType: .string)
                                    copied = true
                                }.accessibilityLabel("复制手机配对内容")
                            }
                        }
                    } else {
                        Text("配对码已到期，请重新生成。")
                            .font(.kpLabel).foregroundStyle(Color.kpSecondary)
                    }
                }
            }
            let devices = manager.configuration?.devices.filter { $0.revokedAt == nil } ?? []
            if devices.isEmpty {
                SettingsRow("已配对设备", detail: "尚无已配对设备")
            }
            ForEach(devices) { device in
                SettingsRow(device.name, detail: "配对时间：\(device.createdAt)") {
                    Button("撤销设备") { Task { await manager.revoke(device) } }
                        .disabled(manager.busy)
                        .accessibilityLabel("撤销设备 \(device.name)")
                }
            }
        }
        .confirmationDialog("更换地址将使已配对设备失效，需要重新配对。", isPresented: $changingAddress) {
            Button("更换地址并启用") { enable() }
            Button("取消", role: .cancel) { }
        }
        .task {
            while !Task.isCancelled {
                await manager.refresh()
                seedDraft()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func seedDraft() {
        guard !seeded, let status = manager.configuration else { return }
        host = addresses.contains(where: { $0.address == status.bindHost })
            ? (status.bindHost ?? "") : (addresses.first?.address ?? "")
        port = String(status.port)
        seeded = true
    }

    private func enable() {
        guard let value = Int(port), validDraft else { return }
        Task { await manager.configure(enabled: true, host: host, port: value) }
    }

    static func qrImage(_ value: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(value.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage,
              let image = CIContext().createCGImage(output, from: output.extent) else { return nil }
        return NSImage(cgImage: image, size: NSSize(width: image.width, height: image.height))
    }
}
