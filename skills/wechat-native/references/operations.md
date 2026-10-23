# 进阶操作

本文件中的 `ctl` 指完整安装器生成的 `~/Library/Application Support/WeChatLocalTool/bin/wx-agent`；自定义安装前缀时使用安装结果中的入口。原生入口转发到本地 runtime 的 `wechat.py`，不使用 UI。

## 只读和精确对象

```sh
ctl native contacts --query '准确备注或昵称'
ctl native read --to 精确ID --limit 40
ctl native read --to 精确ID --database message/message_0.db --after 123 --limit 40
ctl native read --to 精确ID --exact '完整文字'
ctl native poll --to 精确ID --limit 100
ctl native watch --to 精确ID --once
```

`read` 不改增量游标；`poll` 首次建立当前 baseline，后续持久推进独立读取游标；`has_more=true` 继续取下一批。不要用 `--from-start` 回放全部历史，除非用户明确需要。`watch --interval 2` 是前台只读轮询，只有明确需要连续只读监听时才使用，停止要终止其实际进程；它不等于自动回复服务，不会唤醒当前对话。不要遗留无人管理的 watch 进程。

联系人搜索返回 `contacts[{wxid,display_name,type,is_group,...}]` 和 `has_more`。多匹配/截断需用户确定唯一对象，不能只因为目标名字熟悉就挑第一项。`filehelper` 是微信文件传输助手的固定收件人 ID。

## 条件与风格

`listen --condition-json` 接受：

| 规则 | JSON |
| --- | --- |
| 用户指定所有消息均可接话 | `{"mode":"all"}` |
| 群里确实 @ 本人 | `{"mode":"mention"}` |
| 包含关键词 | `{"mode":"keywords","keywords":["聚餐","时间"]}` |
| 用户描述的语义条件 | `{"mode":"semantic","instruction":"只有问到本周聚餐安排时参与，其他话题不回复"}` |

任一规则可加 `senders` 精确发送者 ID 白名单。@ 判断来自微信元数据，不能用正文是否含“@”代替。新的群规则必须由用户给出；已配置群可复用明确保存的条件。

`--quiet-seconds 10` 是默认值，用户指定其他静默窗口时再调整。`--style-file /私有临时文件` 设置该目标关系和口吻；不要将私聊正文或敏感关系写入全局 Skill。普通恢复保留原队列；显式改条件会按当前消息尾部建立新规则状态，不能清掉 unknown/inflight 来强行开启。

## 图片

```sh
ctl native send-image --to 精确ID --file /绝对路径/image.png --request-id 本次唯一ID
ctl native reconcile-media --request-id 原ID
```

当前发送接受 PNG/JPEG，真实验证过 PNG。必须有明确素材和接收对象；生成新图片时另用可用图像能力完成用户的创作请求。不要用测试图冒充用户指定的图片。发送结果核对请求身份、本地记录、图片摘要和服务器编号；unknown 只复核，不换 ID 重发。

## 语音

```sh
ctl native send-voice --to 精确ID --file /绝对路径/voice.silk --duration-ms 4240 --request-id 本次唯一ID
```

必须是完整 Tencent SILK V3 编码（头含 `\x02#!SILK_V3`），时长 1–60 秒。不能把 WAV/MP3 文件改后缀当 SILK 发送。已有转换器：

```sh
"$HOME/Library/Application Support/WeChatLocalTool/venv/bin/python3" \
  "$HOME/Library/Application Support/WeChatLocalTool/runtime/encode_voice.py" \
  --input /绝对路径/audio.wav --output /私有临时路径/voice.silk
```

转换器依赖 `pilk`，使用 `afconvert` 转 16 kHz 单声道 PCM，编码后解码回读，返回真实 `duration_ms`。编码环境缺失时先报告，不乱用整数秒返回值。新合成语音使用明确可用的系统/TTS 声音，不声称模仿了用户本人声音。媒体接收目前只读记录与类型，不代表可以下载、播放、转写内容。

## 手动接管后的恢复

当前 Agent 代聊时若先暂停了一个原本启用的目标，发完且回执明确后：

```sh
ctl listen --to 原精确ID --name '原名称'
```

这会保留原风格与规则；不恢复其他已暂停目标。发生未知发送状态则保持该目标关闭，先用原 request-id 复核。多条发送每次都有独立请求 ID，发送前重读避免新消息到达后继续发旧稿。
