# 自动检查及更换夸克追更来源

这是 `Cp0204/quark-auto-save` v0.8.7 基础上的个人 fork。沿用原来的 Cookie、CloudSaver、PanSou、转存、重命名和定时器，新增每任务的来源恢复。源码许可仍为 AGPL-3.0。

## 使用

1. 先建一个正常的转存任务，选择**实际放视频的叶目录**，设置固定的剧名、云端目标目录及匹配/重命名规则。
2. 展开任务的“失效自动换源”，启用，填写固定剧名、必要的别名、年份、季号、起始集。保存配置。
3. 点“检测当前来源”，看检测结果。点“查找备源”会搜索并预览，**不会直接采用**；候选须有通过验证的视频，才能确认换源。
4. 正常转存执行前会自动检查来源。网页单任务运行、原生定时和直接调用 `do_save_task()` 都走同一入口。此功能不单独提高原生转存频率。

来源明确取消、删除或空目录复查确认时自动找源；长期无新集时找更高集数来源。接入方也可传 `expected_episode`，表示一个已到更新时间但当前源缺少的目标集。自动搜索失败后至少间隔 30 分钟，网页手动检查可绕过冷却。

搜索标题用于找候选。实际目录、原视频名、季集号及已有重命名规则决定能否采用；标题中的“更新至XXX集”不是集数证据。错季、剧场版、预告、集号不明确、改名会丢集号的来源不会采用。只有标题像而文件身份不明确时等待确认，不盲目转存。仅修改来源链接及旧失效标记，保留云端目标、规则和已有文件；启用任务只转存设定季与起始集以后的正片。

网络超时、403、验证码、账号凭证或提取码错误不视为永久失效。原来的 Cookie 或资源服务出问题时仍需修复凭证/网络，无法通过换源绕过。

## 配置例子（不含凭证）

在原任务内增加：

```json
"source_recovery": {
  "enabled": true,
  "title": "遮天",
  "aliases": ["Shrouding the Heavens"],
  "year": 2023,
  "season": 1,
  "min_episode": 183,
  "media_id": "224839",
  "stale_hours": 24,
  "allow_ai": false
}
```

`media_id`供外部影视桥接固定作品身份。QAS 自身不查询 TMDB、不自动创建媒体库、不负责 NAS 整文件下载；NAS 已有桥接负责按固定 ID 去重、下载和整理。

## 可选的小模型

系统配置中打开“换源 AI 辅助”，填兼容 Chat Completions 的**基础地址**、模型名及 API Key，或填环境变量名。基础地址如 `https://模型服务/v1`，程序补 `/chat/completions`。同时启用该任务的 `allow_ai`。

默认关闭。普通检查、规则已明确接受/拒绝的候选不调用模型；只有作品别名不明确且其他规则已通过才调用。模型只能选择当前候选 ID，不能给新链接、改集号、执行工具或越过错季等硬规则。发给模型的是裁剪后的目录/文件名/作品摘要，不含 Cookie、搜索服务口令、分享链接、提取码或签名下载链接。失败时保留原源；每任务默认每日最多 3 次（UTC 日期），单次最多 400 输出 token；自动找源失败冷却 30 分钟。

## 状态、并发和 API

状态保存在 `CONFIG_PATH + '.sources.json'`，权限 0600；含内部候选链接和最多 20 次来源历史，按登录凭证级别保护。UI 仅返回安全摘要与已保存的当前分享链接，显示北京时间。

同一状态文件跨进程互斥。探测最多 20 次网盘 HTTP 请求、10 个候选、3 层目录，共 120 秒探测预算；搜索每服务 30 秒预算。账号检查、超时及分页均纳入预算。配置保存有独立短锁及原子替换，变更任务规则或顺序后旧请求不能覆盖新配置。

- `GET /data` 返回 `data.config_revision`。
- `POST /update`、`POST /source_recovery` 带 `X-Config-Revision`；版本不一致返回 409，重新读取并核对后再操作。
- `POST /source_recovery`：`{"task_index":0,"mode":"check"}`；`mode`也可 `search` 或 `apply`（需要 `candidate_id`）。返回 202 后轮询状态；不重复提交。
- `GET /source_recovery_status?task_index=0&taskname=遮天&savepath=...`：只读缓存，不执行搜索。任务名/目录用于防止重排后的索引误配。
- SDK：设置 `quark_auto_save.CONFIG_DATA`、`CONFIG_PATH`，调用 `account.do_save_task(task, expected_episode=None)`。禁用恢复的任务沿用原生行为。

仅保存过的任务能持久换源。用未保存表单运行临时任务时，配置 CAS 会拒绝更改，应先保存再运行。

## 构建与测试

```sh
python3 -m venv /tmp/qas-venv
/tmp/qas-venv/bin/pip install -r requirements.txt
/tmp/qas-venv/bin/python -m unittest discover -s tests -v
docker build -f Dockerfile.fork --pull=false \
  --build-arg BASE_IMAGE=<当前v0.8.7镜像ID> \
  --build-arg FORK_REVISION="$(git rev-parse HEAD)" \
  -t quark-auto-save-fork:source-recovery .
```

`Dockerfile.fork`复用现有 v0.8.7 镜像，不下载新依赖。部署时保持原配置/媒体挂载、网络、端口和时区。后续从本 fork 的 `codex/auto-source-recovery` 分支更新并重新构建；安装上游 latest 会替换这些功能。不要把真实配置、状态、Cookie、口令、API Key 上传仓库。

当前已有模拟失效/换源、错误候选、模型非法输出、分页/耗时、并发 CAS、单任务 CLI 持久化及 API 防重复测试。真实自动换源只能在出现合格备源时验证；未来新集整文件下载/入库和手机播放需现场验收。
