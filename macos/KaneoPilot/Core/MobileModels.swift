import Foundation

struct MobileAddress: Decodable, Identifiable, Equatable {
    let address: String
    let name: String
    var id: String { address }
}

struct MobileDevice: Decodable, Identifiable, Equatable {
    let deviceId: String
    let name: String
    let createdAt: String
    let revokedAt: String?
    var id: String { deviceId }
}

struct MobileAccessStatus: Decodable {
    let apiVersion: Int
    let serverId: String?
    let enabled: Bool
    let bindHost: String?
    let port: Int
    let endpoint: String?
    let certificateSha256: String?
    let addresses: [MobileAddress]
    let devices: [MobileDevice]
}

struct MobilePairing: Decodable {
    let pairingUri: String
    let expiresAt: String

    var expiration: Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: expiresAt) ?? ISO8601DateFormatter().date(from: expiresAt)
    }
    func isValid(at now: Date = Date()) -> Bool { expiration.map { $0 > now } ?? false }
}
