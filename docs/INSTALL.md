# 安装与首次接通

## 安装包是什么

`wx-agent-v0.1.0-macos-arm64-source.zip` 是包含安装脚本的源码包；`wechat-native-v0.1.0-skill.zip` 只包含 AI 操作入口。两者都不包含微信客户端或账号数据。

## 必要环境

- macOS、Apple Silicon（arm64）；Python 3.11 或以上，Node.js 22 或以上。
- Xcode Command Line Tools 中的 `clang++`，用于构建本地原生桥。
- 压缩消息解码：Python 3.14 及以上使用标准库 zstd；Python 3.11–3.13 还需要本机 `libzstd`，环境检查会核验。
- 已登录的 Codex CLI，用于自动回复时生成草稿。手动读取和发送不需要调用模型。
- 自行准备并获授权使用的微信测试副本。历史基线为 4.1.13 / 269602，默认路径 `~/Applications/WeChat REA Test.app`。包内不含创建或重签该副本的自动化；安装器不会修改原版微信、关闭 SIP 或修改系统权限数据库。
- 当前本人账号、本机数据库的有效访问配置。读取器消费本地 `verified-dbkeys.json`；本包不提供自动获取数据库密钥的流程，也不包含任何现成账号密钥。缺少该条件时，安装源码不能完成读取、发送回执核验及自动回复。

原版和测试副本默认共用同一微信数据目录，不可同时运行。登录和系统正常授权由用户本人完成。

## 安装源码

先用 `python3 --version` 确认是 3.11 或以上；macOS 系统 Python 若仍为 3.9，请用已安装新版解释器的绝对路径执行以下脚本，并通过 `--python /新版解释器绝对路径` 指定安装环境。

在解压后的 `wx-agent` 目录执行：

```sh
python3 scripts/install.py --install-skill --build-bridge
```

安装器会创建 Python 虚拟环境、安装 Python 与 Node Frida 依赖、复制 runtime 和 Skill、生成启动入口并构建桥。若已有 `wechat-native` Skill，默认保留并报告；需要明确替换时添加 `--replace-skill`。Frida 默认装到安装前缀的 `sdk/`，不依赖解压目录；`--frida-module` 可选择自己已有的稳定 SDK 路径。依赖已自行准备时，可使用 `--no-deps --python /绝对路径/python3 --frida-module /稳定路径/frida.js`（同时跳过 pip/npm）。

安装目录默认是 `~/Library/Application Support/WeChatLocalTool`；使用 `--prefix /绝对路径` 自定义。若此目录已用于旧版运行环境，升级前先暂停旧服务并保留自己的配置；不要在正在自动回复时覆盖代码。此次发布没有自动迁移旧单人监听配置。

安装后可删除原解压目录；运行入口、环境检查和桥构建工具均位于安装前缀，分别为 `bin/wx-agent`、`bin/wx-agent-doctor` 和 `bin/wx-agent-build-bridge`。自定义外部 SDK 路径仍需保留。

安装完成不代表已经接通微信，也不会启动微信、写入联系人规则、注册监听服务或发送消息。

## 环境检查

按安装器输出的 `next_check` 执行，或使用安装后的独立入口：

```sh
"$HOME/Library/Application Support/WeChatLocalTool/bin/wx-agent-doctor"
```

检查工具用来定位 Python/Node/Frida、测试副本和本地数据访问文件是否存在；原生桥编译结果以安装阶段的编译回执为准。通过环境检查不等于完成登录、原生权限或真实消息验收。

实际路径写入安装目录的 `runtime/runtime-local.json`。可通过安装器的 `--python`、`--node`、`--codex`、`--frida-module`、`--clang`、`--test-app`、`--original-app`、`--data-root`、`--cache` 等参数配置；也支持对应的 `WECHAT_*` 环境变量。该 JSON 是本机私有配置，不应提交到仓库。

## 首次配置

先核对版本与状态：

```sh
"$HOME/Library/Application Support/WeChatLocalTool/bin/wx-agent" native adapt
"$HOME/Library/Application Support/WeChatLocalTool/bin/wx-agent" status
```

完成本机账号核验后，用实际账号数据目录名及本人精确微信 ID 初始化；下面是占位参数，不能原样执行：

```sh
"$HOME/Library/Application Support/WeChatLocalTool/bin/wechat-autoreply" init \
  --account '实际账号目录名' --self-username '本人精确微信ID'
```

之后由 AI 按 Skill 解析精确联系人、读取必要上下文，并在用户指定范围内启用回复。不要把文档示例目标当作用户已授权对象。新版本先完成 [适配核验](../skills/wechat-native/references/compatibility.md)；需要真实测试发送时，另由用户明确授权目标和内容。

暂停全部自动回复：

```sh
"$HOME/Library/Application Support/WeChatLocalTool/bin/wx-agent" pause
"$HOME/Library/Application Support/WeChatLocalTool/bin/wx-agent" status
```

默认服务名为 `com.wxagent.wechat-autoreply`。旧安装使用其他服务名时，应按旧工具的实际状态暂停，不能把新服务停止误认为旧服务也已停止。

## 可选语音编码

安装时添加 `--voice` 安装 `pilk`；运行程序接收 Tencent SILK V3 文件，不能将 WAV/MP3 改后缀冒充 SILK。编码器通过 macOS `afconvert` 转换并回读真实时长，见 [媒体操作](../skills/wechat-native/references/operations.md)。
