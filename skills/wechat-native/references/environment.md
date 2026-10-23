# 本机环境与故障判断

## 已安装位置

- Skill：`~/.codex/skills/wechat-native`。
- 生产 runtime：`~/Library/Application Support/WeChatLocalTool/runtime`，服务不依赖外置工作卷挂载。
- 维护源码和测试：`wx-agent 仓库的 runtime 目录`。
- 测试微信：`~/Applications/WeChat REA Test.app`；原版：`/Applications/微信.app`，两者共用微信数据，不能同时运行。
- Python：安装器创建的 `~/Library/Application Support/WeChatLocalTool/venv/bin/python3`，需安装 cryptography；Node：安装器配置的 Node.js；Frida SDK 由用户安装并配置，桥编译依赖 Xcode clang++。
- 服务：`com.wxagent.wechat-autoreply`，LaunchAgent 在 `~/Library/LaunchAgents`，日志在 `~/Library/Logs/WeChatLocalTool`。
- 私有状态：微信容器 `Library/Caches/REAResearch/auto-reply`；队列、密钥和发件日志都留在容器内，目录 0700、文件 0600，不输出密钥或媒体 AES/CDN 字段。

仓库携带的历史验证基线为 macOS arm64 微信 4.1.13/269602；不代表新安装环境已验证。基线核心 SHA256：
`6e82322680d7747020c305f67e932b235fe68b22578c24d9c524dabb31a4694a`。
以上是已验证基线，不再以单个全文件哈希拒绝所有新版。首次连接或版本、进程身份变化时校验核心并自动匹配，按进程基址定位、回读已加载映像 UUID；普通调用复用已验证结果和连接，仅检查轻量身份。新版需要多项函数及 vtable 证据一致，发送能力按文字/图片/语音分别验证；详见 [版本适配](compatibility.md)。

上述为默认路径；`runtime/runtime-local.json` 与 `WECHAT_*` 环境变量可覆盖，按实际安装结果核对。

## 状态检查

`control.py status` 查看配置门禁、目标和后台服务，不会启动微信或监听。`control.py native status` 检查测试副本适配状态、进程及允许范围内的原生登录状态；未验证新版只做被动检查，`logged_in=null` 表示未调用登录方法，不能解释成已退出登录。发送自身还会校验登录和版本。

`native transport-status` 查看现有按需连接进程，`native transport-stop` 释放它，两者都不会启动它。连接进程空闲 60 秒退出，无开机自启；它存在只表示近期有原生工具调用，不表示自动回复已开启。`pause` 会一并释放连接，已有 native unknown 仍按原 request-id 只读复核。

不要把旧 `service.json` 的 listening 当成活进程：同时核对 launchctl loaded、服务 PID，以及 listener PID。启动中则短时等待新状态；有 PID 但仍在 starting、waiting_for_database 或 watcher_error，不能报告监听已完成。

## 登录或应用未运行

用户已授权启用本工具且测试副本未运行时，可执行 `control.py native start`；该操作会正常退出原版再启动副本。用户若正在用原版且本次未授权中断，先说明这一步会切换微信会话再确认。手机登录确认只能由本人完成，当前队列保持暂停，不能伪造已登录。

`native restart` 重启测试副本；`native restore-original` 恢复原版，均不作为普通读取/发送失败的自动重试。不要擅自重签或替换原版微信、改 SIP，或重新做侵入式校准来掩盖版本不符。

## macOS 数据访问权限

文件访问卡住时，先对同一受限文件做一次带短超时的读取，并核对当前进程日志；旧的授权等待日志不是当前仍需授权的证明。

当前请求确实等待时，请用户处理正常系统授权入口。拒绝时如实解释，并由本人选择授权；不改 TCC 数据库、不借另一个进程绕过权限，也不默认要求更广的全磁盘访问。放行后先核对实际读取和 listener，再恢复用户明确指定的目标。

用户要求暂停而控制脚本自身被缓存权限卡住时，不继续等待授权才停止。对这个已知服务依次执行 `/bin/launchctl disable gui/当前用户UID/com.wxagent.wechat-autoreply` 与 `bootout`，核对服务卸载和持久禁用；这只停止服务，不绕过数据权限。若发送开关无法写入，说明“服务已停、发送开关尚未确认”；权限恢复后补写关闭开关，不自动重新启动。

## 无法发送或回复停住

- native `unknown/submitting`：查原 request-id，使用只读 `reconcile` / `reconcile-media`；禁止再发一遍试试。
- `blocked`：检查具体原因和 inflight，保留队列证据，不能清空状态强行恢复。
- `model_error`：查看异常类型和后台 stderr；后台同批次不会无休止重试。解决后可由 Agent 显式执行 runtime 的 `autoreply.py retry`；该命令不处理 native unknown。
- `handoff`：可能需要理解未读取媒体、本人作决定或上下文不足。它不会保证通知当前桌面聊天，不声称已经回复。
- 数据库 key/分片/完整性错误：保留游标，停止相关读取或发送，不能把不完整结果当最新完整上下文。

只有用户请求维修或现有代码失败确需修改时才改源码；普通使用调用现有工具。更新 runtime 时先暂停受影响目标、保留游标和回执、验证代码及测试，再恢复原先且仍获授权的目标。运行目录自身启动必须跳过同文件复制，避免暂停后恢复出现 SameFileError。
