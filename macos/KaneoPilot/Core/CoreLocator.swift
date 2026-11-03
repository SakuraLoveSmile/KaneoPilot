import Foundation

/// 解析到的核心位置。`usesPython == true` 时经内置 Python 解释器调用（`PythonRuntime`），
/// 否则作为可执行 shim 直接调用。
struct CoreLocation {
    let path: String
    let usesPython: Bool

    var displayString: String {
        guard usesPython else { return path }
        return "\(PythonRuntime.resolve()?.path ?? PythonRuntime.missingMessage) \(path)"
    }

    /// 经本核心位置运行 `arguments` 的可执行文件与完整参数。需要 Python 而内置运行时缺失时返回 nil：
    /// 调用方给出自己的错误，不回退到 `PATH` 里的 `python3`。
    func invocation(_ arguments: [String],
                    environment: [String: String] = ProcessInfo.processInfo.environment)
        -> (executable: URL, arguments: [String])? {
        guard usesPython else { return (URL(fileURLWithPath: path), arguments) }
        guard let interpreter = PythonRuntime.resolve(environment: environment) else { return nil }
        return (interpreter, [path] + arguments)
    }
}

/// App 内置 Python 运行时（docs/build-release.md「内置 Python 运行时与架构」）。
/// 解析顺序：环境变量 `KANPILOT_PYTHON`（须存在且可执行，供夹具与开发使用）→
/// `<App>/Contents/Resources/python/bin/python3`。**不**查 `PATH`，也不回退到系统 `python3`。
enum PythonRuntime {
    static let missingMessage = "内置 Python 运行时缺失，请重新安装 KaneoPilot"

    static func resolve(environment: [String: String] = ProcessInfo.processInfo.environment,
                        resourceURL: URL? = Bundle.main.resourceURL) -> URL? {
        let fm = FileManager.default
        if let override = environment["KANPILOT_PYTHON"], !override.isEmpty,
           fm.isExecutableFile(atPath: override) {
            return URL(fileURLWithPath: override)
        }
        guard let bundled = resourceURL?.appendingPathComponent("python/bin/python3"),
              fm.isExecutableFile(atPath: bundled.path) else { return nil }
        return bundled
    }

    /// Python 子进程的环境：去掉会把解释器指向别处的继承变量，不加载用户站点包，也不往 App 包里写字节码。
    /// 各调用方自己的 PATH 前缀、代理与实例标识照旧在此之后设置。
    static func environment(_ base: [String: String] = ProcessInfo.processInfo.environment) -> [String: String] {
        var env = base
        for key in ["PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"] {
            env.removeValue(forKey: key)
        }
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env
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
            "local.kaneopilot.execution-ask-relay-acceptance", "local.kaneopilot.mobile-ui-check",
            "local.kaneopilot.software-update-check"]
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

    /// `.py` 或无执行位 → 经内置 Python 调用（`PythonRuntime`）；可执行且非 .py → 直接调用。
    static func needsPython(_ path: String) -> Bool {
        if (path as NSString).pathExtension == "py" { return true }
        return !FileManager.default.isExecutableFile(atPath: path)
    }
}
