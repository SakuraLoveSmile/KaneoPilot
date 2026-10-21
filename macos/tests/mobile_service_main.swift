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
        let host = manager.configuration!.addresses[0].address
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
            for _ in 0..<40 where manager.starting { try? await Task.sleep(for: .milliseconds(100)) }
            if mode == "ready" || mode == "real-core" {
                precondition(manager.running && !manager.starting && !manager.isSimulation)
                if mode == "real-core" {
                    // Pass the probe deadline: ready cleanup must not be repeated.
                    try? await Task.sleep(for: .seconds(7))
                    precondition(manager.running && manager.error == nil, manager.error ?? "service not running")
                    await manager.createPairing()
                    precondition(manager.pairing?.isValid() == true)
                    await manager.refresh()
                    precondition(manager.running && manager.configuration?.enabled == true)
                }
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
}
