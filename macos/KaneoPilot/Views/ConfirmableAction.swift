import Foundation

enum ConfirmableAction: Identifiable {
        case restore, acceptDuplicate, markSent, markDone, releaseLost, cancelPlan
        case retryExecution(runID: String)

        var id: String {
            switch self {
            case .restore: return "restore"
            case .acceptDuplicate: return "accept_duplicate"
            case .markSent: return "mark_sent"
            case .markDone: return "mark_done"
            case .releaseLost: return "release_lost"
            case .cancelPlan: return "cancel-request"
            case .retryExecution: return "resume"
            }
        }
        var title: String {
            switch self {
            case .restore: return "恢复原任务"
            case .acceptDuplicate: return "接受可能重复，重新派发"
            case .markSent: return "登记已发出"
            case .markDone: return "登记完成"
            case .releaseLost: return "释放失联请求"
            case .cancelPlan: return "终止排队计划"
            case .retryExecution: return "重新执行（沿用计划）"
            }
        }
        var message: String {
            switch self {
            case .restore:
                return "认定上一次派发未发出，任务回到就绪并重新排队派发请求。"
            case .acceptDuplicate:
                return "发送状态不明：接受可能重复执行的风险，仍会重新排队派发请求。"
            case .markSent:
                return "人工确认消息已发出：任务转为执行中并建立监控请求。"
            case .markDone:
                return "将任务归档为完成，不再自动推进。"
            case .cancelPlan:
                return "撤销尚未领取的规划请求，保留材料与历史，等待手动重新规划。若已被领取，本操作会拒绝终止。"
            case .releaseLost:
                return "取消失联请求并把任务恢复到可安全推进的状态。"
            case .retryExecution:
                return "沿用已确认计划重新排队执行，保留已有改动和原失败记录。执行端领取后会再次调用模型。"
            }
        }
        var destructive: Bool {
            switch self {
            case .acceptDuplicate, .markDone, .releaseLost, .cancelPlan: return true
            case .restore, .markSent, .retryExecution: return false
            }
        }
        var expectedExecutionRunID: String? {
            if case .retryExecution(let runID) = self { return runID }
            return nil
        }
    }
