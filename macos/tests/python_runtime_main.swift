import Foundation

// The app's Python resolver and the way CoreClient launches the core with it. Modes (argument 1):
//   resolve <root>   resolution order, no PATH fallback, environment sanitizing, invocation shape (pure)
//   launch <root>    a fake interpreter records how CoreClient started it (argv and environment)
//   missing <root>   an unusable interpreter is an error, never a fall back to a python3 on PATH
// No network, no database, no real core.

private var assertions = 0

private func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    assertions += 1
    if !condition() {
        FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
        exit(1)
    }
}

private func makeFile(_ url: URL, mode: Int, _ text: String = "#!/bin/sh\nexit 0\n") throws {
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try Data(text.utf8).write(to: url)
    try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: url.path)
}

private func lines(_ url: URL) throws -> [String] {
    try String(contentsOf: url, encoding: .utf8).split(separator: "\n").map(String.init)
}

@main
struct PythonRuntimeFixtures {
    static func resolution(root: URL) throws {
        let fm = FileManager.default
        let resources = root.appendingPathComponent("App.app/Contents/Resources")
        let runtime = resources.appendingPathComponent("python/bin")
        let override = root.appendingPathComponent("override/python")
        let plain = root.appendingPathComponent("plain/python")
        let pathBin = root.appendingPathComponent("pathbin")
        try makeFile(override, mode: 0o755)
        try makeFile(plain, mode: 0o644)
        try makeFile(pathBin.appendingPathComponent("python3"), mode: 0o755)
        let onPath = ["PATH": pathBin.path + ":/usr/bin:/bin"]

        // Nothing configured: no interpreter, even though a python3 sits on PATH.
        check(PythonRuntime.resolve(environment: onPath, resourceURL: resources) == nil,
              "no override and nothing bundled resolves to nothing, whatever PATH holds")
        check(PythonRuntime.resolve(environment: onPath, resourceURL: nil) == nil, "no resource directory resolves to nothing")

        // The bundled entry is a symlink to the versioned binary, as in the shipped runtime.
        try makeFile(runtime.appendingPathComponent("python3.13"), mode: 0o755)
        try fm.createSymbolicLink(atPath: runtime.appendingPathComponent("python3").path, withDestinationPath: "python3.13")
        let bundled = runtime.appendingPathComponent("python3")
        check(PythonRuntime.resolve(environment: onPath, resourceURL: resources)?.path == bundled.path,
              "the bundled runtime is Resources/python/bin/python3")

        // The override wins when it is usable; an unusable one never blocks the bundled runtime.
        check(PythonRuntime.resolve(environment: ["KANPILOT_PYTHON": override.path], resourceURL: resources)?.path == override.path,
              "KANPILOT_PYTHON overrides the bundled runtime")
        check(PythonRuntime.resolve(environment: ["KANPILOT_PYTHON": override.path], resourceURL: nil)?.path == override.path,
              "KANPILOT_PYTHON works without a bundled runtime (fixtures and development)")
        for unusable in [plain.path, root.appendingPathComponent("absent/python").path, ""] {
            check(PythonRuntime.resolve(environment: ["KANPILOT_PYTHON": unusable], resourceURL: resources)?.path == bundled.path,
                  "an unusable override (\(unusable.isEmpty ? "empty" : unusable)) falls through to the bundled runtime")
            check(PythonRuntime.resolve(environment: ["KANPILOT_PYTHON": unusable], resourceURL: nil) == nil,
                  "an unusable override without a bundled runtime resolves to nothing")
        }

        // A runtime that lost its execute bit is not an interpreter.
        try fm.setAttributes([.posixPermissions: 0o644], ofItemAtPath: runtime.appendingPathComponent("python3.13").path)
        check(PythonRuntime.resolve(environment: onPath, resourceURL: resources) == nil, "a non-executable runtime is refused")

        // Environment: nothing inherited may point the interpreter somewhere else.
        let inherited = ["PATH": "/usr/bin:/bin", "HOME": "/Users/fixture", "PYTHONHOME": "/h", "PYTHONPATH": "/p",
                         "PYTHONSTARTUP": "/s", "PYTHONUSERBASE": "/u", "PYTHONUNBUFFERED": "1",
                         "PYTHONDONTWRITEBYTECODE": "0", "KEEP_ME": "yes"]
        let cleaned = PythonRuntime.environment(inherited)
        for key in ["PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"] {
            check(cleaned[key] == nil, "\(key) is not passed on")
        }
        check(cleaned["PYTHONNOUSERSITE"] == "1" && cleaned["PYTHONDONTWRITEBYTECODE"] == "1",
              "user site packages are off and no bytecode is written into the app bundle")
        check(cleaned["PATH"] == "/usr/bin:/bin" && cleaned["HOME"] == "/Users/fixture"
              && cleaned["PYTHONUNBUFFERED"] == "1" && cleaned["KEEP_ME"] == "yes", "every other variable is kept")
        check(inherited["PYTHONPATH"] == "/p", "the input environment is not modified")

        // Invocation shape: the interpreter itself, the core script as its first argument, no env/PATH hop.
        let core = CoreLocation(path: "/core/kanpilot.py", usesPython: true)
        let launch = core.invocation(["ping", "--json", "{}"], environment: ["KANPILOT_PYTHON": override.path])
        check(launch?.executable.path == override.path
              && launch?.arguments == ["/core/kanpilot.py", "ping", "--json", "{}"],
              "a Python core runs as <interpreter> <script> <command> ...")
        check(launch?.executable.path != "/usr/bin/env" && launch?.arguments.contains("python3") == false,
              "nothing resolves python3 through env or PATH")
        check(core.invocation(["ping"], environment: onPath) == nil,
              "a Python core without a usable interpreter has no invocation")
        let shim = CoreLocation(path: "/core/kanpilot", usesPython: false)
        let direct = shim.invocation(["ping"], environment: [:])
        check(direct?.executable.path == "/core/kanpilot" && direct?.arguments == ["ping"],
              "an executable core runs directly and needs no interpreter")
        check(shim.displayString == "/core/kanpilot", "an executable core is shown by its path")
        check(core.displayString.hasSuffix(" /core/kanpilot") && !core.displayString.hasPrefix("python3 "),
              "a Python core is shown with the interpreter it runs on")
    }

    static func launch(root: URL) async throws {
        let core = root.appendingPathComponent("core/kanpilot.py").path
        let client = CoreClient(location: CoreLocation(path: core, usesPython: true))
        let answer = try await client.run("ping", payload: ["a": 1], timeout: 30)
        check(answer["ok"] as? Bool == true, "the core answered through the interpreter KANPILOT_PYTHON names")
        let record = root.appendingPathComponent("record")
        let arguments = try lines(record.appendingPathComponent("argv-ping"))
        check(arguments == [core, "ping", "--json", "{\"a\":1}"],
              "argv is <script> <command> --json <payload>: no python3 word, the script comes first")
        let environment = try lines(record.appendingPathComponent("env-ping"))
        check(environment.contains("PYTHONNOUSERSITE=1") && environment.contains("PYTHONDONTWRITEBYTECODE=1"),
              "the core runs without user site packages and without writing bytecode")
        check(!environment.contains { $0.hasPrefix("PYTHONPATH=") || $0.hasPrefix("PYTHONHOME=") || $0.hasPrefix("PYTHONSTARTUP=") },
              "inherited interpreter variables do not reach the core")
        check(environment.contains { $0.hasPrefix("PATH=" + NSHomeDirectory() + "/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:") },
              "the user's tool directories still lead PATH for pi, codex and gh")
        check(!environment.contains { $0.hasPrefix("KANPILOT_SCHEDULER_PID=") },
              "an ordinary command carries no scheduler identity, even if one was inherited")

        _ = try await client.run("executor-instance", timeout: 30)
        let scheduler = try lines(record.appendingPathComponent("env-executor-instance"))
        check(scheduler.contains("KANPILOT_SCHEDULER_PID=\(ProcessInfo.processInfo.processIdentifier)"),
              "executor-instance names this app as its scheduler")
    }

    static func missing(root: URL) async throws {
        let core = root.appendingPathComponent("core/kanpilot.py").path
        let client = CoreClient(location: CoreLocation(path: core, usesPython: true))
        do {
            _ = try await client.run("ping", timeout: 30)
            check(false, "a missing interpreter must not fall back to a python3 on PATH")
        } catch let error as CoreError {
            if case .launchFailed(let detail) = error {
                check(detail == PythonRuntime.missingMessage, "the error says the built-in runtime is missing")
                check(error.isConnectivity, "it is reported as a connectivity problem")
            } else {
                check(false, "unexpected core error: \(error)")
            }
        } catch {
            check(false, "unexpected error: \(error)")
        }
        check(!FileManager.default.fileExists(atPath: root.appendingPathComponent("record/path-python-ran").path),
              "no python3 from PATH was started")

        // An executable core needs no interpreter at all.
        let shim = root.appendingPathComponent("core-shim").path
        let direct = try await CoreClient(location: CoreLocation(path: shim, usesPython: false)).run("ping", timeout: 30)
        check(direct["shim"] as? Bool == true, "an executable core still runs when no interpreter is available")
    }

    static func main() async throws {
        let arguments = CommandLine.arguments
        guard arguments.count == 3 else {
            FileHandle.standardError.write(Data("usage: python-runtime-fixture resolve|launch|missing <root>\n".utf8))
            exit(2)
        }
        let root = URL(fileURLWithPath: arguments[2], isDirectory: true)
        switch arguments[1] {
        case "resolve": try resolution(root: root)
        case "launch": try await launch(root: root)
        case "missing": try await missing(root: root)
        default:
            FileHandle.standardError.write(Data("unknown mode \(arguments[1])\n".utf8))
            exit(2)
        }
        print("python-runtime fixtures (\(arguments[1])): \(assertions) assertions passed")
    }
}
