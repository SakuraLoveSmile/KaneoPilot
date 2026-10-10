# GitHub SSH 别名识别修复（2026-10-06）

状态：待体验，用户尚未验收。工作区全部此前会话/UI修改保留，未提交或推送。

## 结果

Blog的origin使用github-sakuralovesmile别名，实际OpenSSH配置指向ssh.github.com:443、用户git。原解析器仅识别字面github.com，导致origin_not_github。现由同一GitHub仓库解析器识别合法SCP/SSH URL别名，在仓库cwd以ssh -G读取本机配置，传输配置检查和展开共用100ms截止，不连接远端。预检、PR仓库身份及推送目标共用逻辑；实际地址保持原别名以保留多账号认证。

标准HTTPS/SSH地址兼容；未知/非GitHub/非法地址、超时与不能可靠展开的SSH传输或variant覆写拒绝。有效别名目标限github.com/ssh.github.com、git用户、22/443端口。保留单一推送目标、owner/repo一致性、URL rewrite、Review/租约等既有守卫。不解释执行仓库自定义shell传输命令，不跨检查缓存SSH展开。OpenSSH处理用户可信Host/Include/Match配置（Match exec属于OpenSSH原有配置行为）；禁CanonicalizeHostname以免DNS规范化，不宣称对任意用户配置命令的通用沙箱。

## 本次验证

- 核心Git/自动处理两组初轮128 passed；最终受影响别名/transport/variant检查50 passed、93 deselected。测试使用临时Git/DB与隔离SSH配置，Host/Include/GitHub443/下划线别名、未知主机、非法输入、失败/超时、配置变动与自定义传输拒绝已覆盖。
- PR/pipeline兼容组34 passed、29 subtests passed，另1项在terminal completed而observer仍running/exit_code尚未写回的窗口失败；原断言及生命周期未改，独立复跑该项1 passed。保留此瞬态失败记录，不冒充首轮全绿。
- 两次独立reviewer只读复核没有发现确定缺陷；未以静态审查代替测试。测试输出未独立保存日志，以本会话工具记录为证据；准确命令见tests.json，未收录无法核对过滤器的中间48项结果。
- 主agent实际读取Blog仓库与用户SSH配置：正确识别SakuraLoveSmile/Sakura-Blog；在完整临时DB/根/伪凭据下调用公共get-project-mappings（refresh_remote=false），origin_not_github已消失。五次54–83ms，中位59.787ms，符合200ms目标；其他默认预检codes是隔离环境值，不代表用户正式设置状态。详见blog-read-only-check.json。
- 包内真实核心再次读取Blog、核验单一推送地址与稳定URL rewrite，返回原SSH别名。仅git remote/config/ls-remote --get-url/ssh -G读取，没有fetch/push、创建PR、改Blog origin、用户SSH配置或正式DB。详见blog-push-target-read-only.json。

## 交付

最新Release安装包0.2（20261006.2），arm64：dist/release-github-alias-fix-20261006-223623/KaneoPilot.dmg。签名与DMG校验通过，挂载后的文件逐一与.app一致，107个包内核心文件与当前源码一致；包内parser的真实Blog身份核验通过，详见build-manifest.json。此前配置浮层和执行会话修改均包含在此包，旧安装包保留。

正式应用未由agent替换/安装或重启；当前已安装App的警告尚不作为新版原生通过证据。安装新版后重新打开项目与仓库页读取最新预检结果。此修复不宣称真实GitHub推送/PR交付或完整自动流程已验收，用户未授权此类远端动作。本轮未启动新GUI候选或改系统设置。
