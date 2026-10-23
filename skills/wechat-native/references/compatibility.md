# 版本自动定位与验证

`ctl` 表示本技能 `scripts/control.py`，用已配置 Python 执行。适配不改变自动回复开关，当前暂停的目标在检测、验证、部署之后继续暂停。

## 自动检查

```sh
ctl native adapt
ctl native adapt --refresh
ctl native status
```

`adapt` 只读核心文件并写本地适配报告，不启动微信、不调用原生发送、不读取聊天。缓存位于 `~/Library/Application Support/WeChatLocalTool/compatibility`；以二进制 SHA256、profiles 摘要和实现摘要共同失效。报告不含聊天正文或数据库密钥。

日常调用先比较文件设备号、inode、大小、纳秒修改/变更时间及进程 PID、启动时间、可执行路径。身份相同就复用已验证报告，不再读取大核心文件。版本、进程、profiles 或实现文件变化时重新做核心哈希校验；内容相同可复用匹配结果，新内容重新匹配。`--refresh` 始终强制重算与匹配。权限回执单独检查，撤销能力及时生效。

原生调用通过按需启动的本地连接进程复用 Frida 会话。状态检查和发送共享已校验连接，空闲 60 秒后卸载探针并退出。它只响应本地请求，不查询聊天或调用模型；无需新增 launchd 或定时任务。微信重启、连接断开或文件身份变化后，下一次请求重新校验并连接。请求已经写出后断线会保留 `unknown`，不会自动重发。

新进程尚未加载核心模块时，新连接最多等待 8 秒并确认探针就绪；这是提交前的初始化等待，不是发送重试。模块重名、UUID 或地址校验失败直接返回错误。暖请求不重复等待。

诊断连接状态或单独释放连接：

```sh
ctl native transport-status
ctl native transport-stop
```

这两个命令不会启动连接进程。用户要求暂停自动回复仍执行 `ctl pause`：关闭发送开关、停掉后台回复服务，同时释放连接。单独 `transport-stop` 不等于暂停自动回复。更新运行库前先关闭旧连接，避免内存里继续使用旧版工具代码。

| 状态 | 含义及后续 |
| --- | --- |
| `exact` | 与某个 profile 的 ARM64 映像一致；仍需看 `capabilities`，新增 profile 本身不授予发送能力 |
| `matched` | 在不同映像找到唯一一致的函数、调用关系和 vtable 候选；只说明静态匹配成功 |
| `needs_review` | 缺失、歧义、关系或布局变化，报告 `unresolved`；阻止原生调用，不靠旧地址猜测 |
| `capabilities.text/image/voice=true` | 历史已验证基线，或该版本该种消息已有文件传输助手原生回执验证 |

使用完整函数的 ARM64 归一化特征、函数边界、调用方/被调用方关系与 chained-fixup vtable 证据。只归一化地址相关位，字段偏移不被掩盖。新版本函数实现大改、对象布局改变或出现多个等价候选时，需要针对未解决项补充适配；不保证任意未来版本都自动通过。

## 不发送的运行验证

```sh
ctl native adapt --verify-runtime
```

仅针对已运行的测试副本；回读映像 UUID、内存范围，构造并核对三类消息对象，然后销毁，不创建聊天记录或进入发送调度。未知版本只有静态匹配成功才能进入这个显式构造检查。通过后仍不会自行开启发送能力。

## 新版本的真实验证

真实发送只在用户已授权本次文件传输助手测试时执行。先展示固定文字或具体图片/语音素材，再使用当前 `adapt` 返回的完整 SHA256 和唯一 request-id：

```sh
ctl native adapt --test-send text --confirm-core-sha256 当前完整SHA256 --request-id 本次唯一ID
ctl native adapt --test-send image --file /绝对路径/测试图片.png --confirm-core-sha256 当前完整SHA256 --request-id 本次唯一ID
ctl native adapt --test-send voice --file /绝对路径/测试语音.silk --duration-ms 实际时长 --confirm-core-sha256 当前完整SHA256 --request-id 本次唯一ID
```

测试目标固定 `filehelper`，不能传其他联系人或群。先完成构造回读，再通过既有幂等发件流程发送。只有当前核心、请求ID、媒体种类、目标和非零服务器编号均归属这次测试，才记录该种能力已验证。文字通过不会连带打开图片或语音。`unknown` 保持未验证，不换 ID 重发；用原有 `reconcile` 或 `reconcile-media` 只读复核后再处理原测试。

原版更新不等于测试副本也已更新。`adapt --core /应用/Contents/Resources/wechat.dylib` 可以离线检查指定文件；运行检查和发送始终针对配置中的测试副本。此工具不自动复制、重签应用，也不处理系统登录确认。读取聊天仍由独立数据库读取器负责，发送适配成功不代表新版数据库格式或所有历史同步已验证。
