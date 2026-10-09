# codex-app-manager（Python 标准库，无额外依赖）

## 命令：


```bash
codex-app-manager set team.json
codex-app-manager set *.json
codex-app-manager delete *.json
codex-app-manager send SESSION_ID '完整 prompt 文本'
codex-app-manager send SESSION_ID '完整 prompt 文本' --timeout 600
codex-app-manager --socket /absolute/app.sock send SESSION_ID 'prompt'
```


默认使用 codex app-server daemon version 查找已经运行的 App socket。
它不会执行 daemon start、启动独立 app-server 或调用 agent-team init。
正常环境必须能在 PATH 找到 codex。也可显式指定已经运行的 App socket。

JSON 格式沿用 schema_version=1、team_prompt、defaults、sessions。
成员字段：name、id（null 或现有 ID）、cwd、base_prompt。
cwd 相对 JSON 所在目录解析。defaults.cwd 可作为成员默认目录。
配置字段可放 defaults 或成员中：model、reasoning_effort、sandbox、
approval_policy、approvals_reviewer、service_tier、summary。
成员字段覆盖 defaults；null 清除该字段的继承值（不下发 null override）。
新 Session 未指定 sandbox/approval 时默认 workspace-write/on-request。
已有 Session 未指定的配置保留原值。guardian_subagent 别名规范化为 auto_review。

## set：

id 为 null 时创建新的持久 Session，立即原子回写真实 ID。
全部新 ID 已写回后，向每个新 Session 发送 team_prompt + 两个换行 + base_prompt。
已有 ID 先恢复/订阅到当前 App，等待空闲，读取有效配置；已一致时无需更新。
有变化时调用 thread/settings/update，以 thread/settings/updated 确认配置生效；
更新名称，但不发送任何 prompt。配置未变化时服务端不会发送更新通知。
team_prompt/base_prompt 是新成员初始化文本，set 不重新注入已有成员的初始化文本。
接受一个或多个 JSON；全部 JSON 先校验，再操作 App。

## send：

只允许目标已经出现在当前 App 的 thread/loaded/list 中。
不属于当前 App（包括未加载）的目标报错，不自动恢复。
对已加载目标通过 thread/resume 订阅事件；忙碌时等待完成/状态变化事件，
复核 idle 后发送。不使用 turn/steer，不定时轮询模型。
默认一直等待；Ctrl+C 取消等待；--timeout 限制等待空闲的时间。
--rpc-timeout（默认 30 秒）单独限制连接、RPC 和配置确认，不限制模型运行时间。
成功返回只代表 App 已接受新 turn，输出 JSON 含 session_id 和 turn_id；
不等待这次新 turn 运行结束，也不自动处理审批。审批请在 App 中处理。

## delete：

接受一个或多个 JSON，仅读取 sessions 中的 name/id；不要求旧 cwd 仍然存在。
标准输出先以表格显示所有成员的 Session ID、JSON 成员名和 App 会话标题。
null ID 显示为未创建并跳过；重复 ID 只删除一次，成功后清空所有对应条目。
在标准错误输出 (y/N) 确认提示，只有输入 y（或 Y）才执行；回车/EOF/其他输入取消。
thread/delete 是永久删除，并会删除该会话的派生子会话；不会发送 prompt 或恢复会话。
每个 Session 删除成功后才原子将 JSON ID 置为 null，并记录删除状态，后续 set 可重新创建。
RPC 失败/结果未知时保留该 ID、不重试；此前已经成功的删除会保留回写结果。

## 状态与失败：

团队 JSON 的同目录锁文件避免本工具的并发 set；ID 写入检测外部改动。
隐藏的 .<JSON文件名>.codex-app-manager-state 记录创建及初始发送状态，
不会被普通 *.json 匹配。创建结果未知时停止，禁止自动重复创建。
已收到的真实 ID 即使后续配置或发送失败也会保留。
连接断开、写请求超时均不自动重发；检查 App 和状态文件后再决定下一步。
CLI 的 session 锁仅协调本工具，不能与 App UI 操作形成服务器端原子事务；
测试/调度期间请避免在目标 Session 的 UI 同时提交另一条输入。

