import Foundation

/// 解析到的核心位置。`usesPython == true` 时经 `python3 <path>` 调用，
/// 否则作为可执行 shim 直接调用。
struct CoreLocation {
    let path: String
    let usesPython: Bool

    var displayString: String {
        usesPython ? "python3 \(path)" : path
    }
}

/// 核心路径解析（顺序按 ui-spec §1 / 任务冻结规格）：
/// 1. 环境变量 `KANPILOT_CORE`
/// 2. `<App bundle>/Resources/core/kanpilot.py`（或 `kanpilot`）
/// 3. repo 相对 `../core/kanpilot.py`（源码位置推导，另扫 bundle 祖先目录兜底）
enum CoreLocator {

    static func resolve(environment: [String: String] = ProcessInfo.processInfo.environment) -> CoreLocation? {
        let fm = FileManager.default

        // A standalone acceptance bundle must never fall through to the production core.
        if ["local.kaneopilot.session-reader-check", "local.kaneopilot.desktop-intake-check",
            "local.kaneopilot.ui-simplification", "local.kaneopilot.ui-engine-check",
            "local.kaneopilot.execution-ask-relay-acceptance", "local.kaneopilot.mobile-ui-check"]
            .contains(Bundle.main.bundleIdentifier ?? "") {
            let required = ["KANPILOT_CORE", "KANPILOT_APP_SUPPORT", "KANPILOT_DB", "KANPILOT_LEGACY_ROOT", "KANPILOT_PLAN_ROOT", "KANPILOT_REPOS_ROOT", "KANPILOT_REPORTS_ROOT", "KANEO_CREDENTIALS_PATH"]
            guard required.allSatisfy({ environment[$0]?.isEmpty == false }) else { return nil }
            guard let core = environment["KANPILOT_CORE"], fm.fileExists(atPath: core) else { return nil }
            return CoreLocation(path: core, usesPython: needsPython(core))
        }

        if let envPath = environment["KANPILOT_CORE"], !envPath.isEmpty {
            if fm.fileExists(atPath: envPath) {
                return CoreLocation(path: envPath, usesPython: needsPython(envPath))
            }
        }

        if let resources = Bundle.main.resourceURL {
            for name in ["core/kanpilot.py", "core/kanpilot"] {
                let candidate = resources.appendingPathComponent(name).path
                if fm.fileExists(atPath: candidate) {
                    return CoreLocation(path: candidate, usesPython: needsPython(candidate))
                }
            }
        }

        for candidate in repoCandidates() where fm.fileExists(atPath: candidate) {
            return CoreLocation(path: candidate, usesPython: needsPython(candidate))
        }
        return nil
    }

    /// 诊断用：按解析顺序列出全部候选路径。
    static func searchedPaths(environment: [String: String] = ProcessInfo.processInfo.environment) -> [String] {
        var out: [String] = []
        if let envPath = environment["KANPILOT_CORE"], !envPath.isEmpty {
            out.append("\(envPath)  (KANPILOT_CORE)")
        }
        if let resources = Bundle.main.resourceURL {
            out.append(resources.appendingPathComponent("core/kanpilot.py").path)
            out.append(resources.appendingPathComponent("core/kanpilot").path)
        }
        out.append(contentsOf: repoCandidates())
        return out
    }

    /// repo 布局 `../core/kanpilot.py`：
    /// 本文件位于 <repo>/macos/KaneoPilot/Core/CoreLocator.swift，
    /// 向上三级到 macos/、再上一级即 repo 根。
    static func repoCandidates() -> [String] {
        var out: [String] = []
        let coreDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        let repoRoot = coreDir
            .deletingLastPathComponent()   // KaneoPilot/
            .deletingLastPathComponent()   // macos/
            .deletingLastPathComponent()   // <repo>/
        out.append(repoRoot.appendingPathComponent("core/kanpilot.py").path)
        out.append(repoRoot.appendingPathComponent("core/kanpilot").path)

        // 兜底：从 .app bundle 向上找（例如 <repo>/dist/KaneoPilot.app）
        var dir = Bundle.main.bundleURL
        for _ in 0..<5 {
            dir.deleteLastPathComponent()
            out.append(dir.appendingPathComponent("core/kanpilot.py").path)
            out.append(dir.appendingPathComponent("core/kanpilot").path)
        }
        return out
    }

    /// `.py` 或无执行位 → 经 python3 调用；可执行且非 .py → 直接调用。
    static func needsPython(_ path: String) -> Bool {
        if (path as NSString).pathExtension == "py" { return true }
        return !FileManager.default.isExecutableFile(atPath: path)
    }
}
