import Foundation
import ServiceManagement

enum LoginItem {
    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
            || SMAppService.mainApp.status == .requiresApproval
    }
    static var needsApproval: Bool { SMAppService.mainApp.status == .requiresApproval }

    static func setEnabled(_ on: Bool) throws {
        if on {
            guard SMAppService.mainApp.status != .enabled,
                  SMAppService.mainApp.status != .requiresApproval else { return }
            try SMAppService.mainApp.register()
        } else if SMAppService.mainApp.status != .notRegistered {
            try SMAppService.mainApp.unregister()
        }
    }
}
