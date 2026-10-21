import Foundation

@main
struct MobileServiceFixtures {
    @MainActor static func main() async throws {
        let mode = CommandLine.arguments[1]
        let path = ProcessInfo.processInfo.environment["KANPILOT_CORE"]!
        let client = CoreClient(location: CoreLocation(path: path, usesPython: true))
        let foreign = Process()
        foreign.executableURL = URL(fileURLWithPath: "/bin/sleep")
        foreign.arguments = ["30"]
        try foreign.run()
        defer { if foreign.isRunning { foreign.terminate() }; foreign.waitUntilExit() }
        let manager = MobileServiceManager(client: client, localNetworkContextEnabled: mode == "real-core")
        await manager.refresh()
        precondition(manager.configuration?.enabled == false && !manager.running)
        // Core address order also includes LAN/VPN interfaces. This fixture
        // promises loopback-only traffic, independently of runner interfaces.
        let loopbackOnly = mode == "real-core" || mode == "loopback-host"
        let host = loopbackOnly ? "127.0.0.1" : manager.configuration!.addresses[0].address
        precondition(manager.configuration!.addresses.contains { $0.address == host },
                     "fixture address unavailable: \(host)")
        let port = Int(ProcessInfo.processInfo.environment["KANPILOT_MOBILE_TEST_PORT"] ?? "47831")!
        await manager.configure(enabled: true, host: host, port: port)
        if mode == "simulation" {
            precondition(manager.running && manager.isSimulation && !manager.starting)
            await manager.createPairing()
            precondition(manager.pairing?.isValid() == true)
            precondition(manager.pairing?.pairingUri.hasPrefix("kaneopilot://pair?data=") == true)
            let device = manager.configuration!.devices.first!
            precondition(device.revokedAt == nil)
            await manager.revoke(device)
            precondition(manager.configuration!.devices.first!.revokedAt != nil)
            let expired = MobilePairing(pairingUri: "test", expiresAt: "2020-01-01T00:00:00Z")
            precondition(!expired.isValid())
            await manager.configure(enabled: false, host: host, port: 47831)
            precondition(!manager.running && manager.pairing == nil && manager.configuration?.enabled == false)
        } else {
            precondition(!manager.running, "enabled must not be treated as ready")
            let clock = ContinuousClock()
            let started = clock.now
            // Product startup has a 15s watchdog, followed by up to 5s owned
            // shutdown. Allow 1s scheduling margin, but require a real terminal
            // state rather than interpreting elapsed fixture time as failure.
            let deadline = started.advanced(by: .seconds(21))
            while manager.starting && clock.now < deadline {
                try await Task.sleep(for: .milliseconds(100))
            }
            print("mobile manager \(mode) startup settled after \(started.duration(to: clock.now))")
            precondition(!manager.starting, startupDiagnostic(manager, mode: mode))
            if mode == "ready" || mode == "delayed-ready" || loopbackOnly {
                precondition(manager.running && !manager.starting && !manager.isSimulation,
                             startupDiagnostic(manager, mode: mode))
                if mode == "delayed-ready" {
                    precondition(started.duration(to: clock.now) >= .seconds(4),
                                 "delayed-ready must exercise startup beyond the old 4s fixture deadline")
                }
                if mode == "real-core" {
                    // Pass the probe deadline: ready cleanup must not be repeated.
                    try? await Task.sleep(for: .seconds(7))
                    precondition(manager.running && manager.error == nil, manager.error ?? "service not running")
                    await manager.createPairing()
                    precondition(manager.pairing?.isValid() == true)
                    await manager.refresh()
                    precondition(manager.running && manager.configuration?.enabled == true)
                }
            } else if mode == "startup-timeout" {
                precondition(!manager.running && manager.error?.contains("15 秒") == true,
                             startupDiagnostic(manager, mode: mode))
                precondition(started.duration(to: clock.now) >= .seconds(15),
                             "fixture must wait for the product startup watchdog")
            } else {
                precondition(!manager.running && manager.error?.contains("端口") == true, manager.error ?? "missing startup failure")
            }
            let stopped = await withCheckedContinuation { continuation in
                manager.stopOwnedProcessForAppExit { continuation.resume(returning: $0) }
            }
            precondition(stopped && !manager.running)
        }
        precondition(foreign.isRunning, "manager must preserve foreign processes")
        print("mobile manager \(mode): passed")
    }

    @MainActor
    private static func startupDiagnostic(_ manager: MobileServiceManager, mode: String) -> String {
        "mobile service startup failed: mode=\(mode), enabled=\(manager.configuration?.enabled.description ?? "nil"), "
            + "running=\(manager.running), starting=\(manager.starting), simulation=\(manager.isSimulation), "
            + "endpoint=\(manager.configuration?.endpoint ?? "nil"), error=\(manager.error ?? "nil")"
    }
}
