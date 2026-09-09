---
name: qiuzhi-pipeline
description: 执行 npe_get_jobs 求职自动化流程（BOSS 岗位发现 → DeepSeek 简历匹配 → 批量打招呼投递），含登录刷新与各类报错的处理规程
type: prompt
whenToUse: 当用户要求跑求职流程、发现新岗位、投递岗位、刷新 BOSS 登录、或排查 npe_get_jobs 项目的报错时使用
arguments:
  - stage
---

# npe_get_jobs 求职自动化流程

项目根目录：`C:\Users\YHR\npe_get_jobs`（以下所有命令都在该目录、conda base 环境的 `python` 下执行，**不要用 WindowsApps 的 python3.12.exe**）。

可选参数 `$stage`：`discover`（只发现+匹配）/ `send`（只投递）/ `login`（只刷登录）/ 空或 `all`（全流程）。用户提到具体阶段时按阶段执行，否则跑全流程。

## 步骤 0：前置检查

1. 服务健康检查：`GET http://localhost:8081/actuator/health` 和 `GET http://localhost:17321/health`。
2. 任一不通：Java 后端未启动——在后台启动 `cd C:/Users/YHR/npe_get_jobs && java -jar target/npe_get_jobs-v1.1.0.jar`（JDK 用 `C:\Users\YHR\.jdks\ms-21.0.8\bin\java`），等启动完成后再继续。

## 步骤 1：确认 BOSS 登录态

- 跑 `python login_boss.py`：弹出受控 Chrome，已登录则秒过，未登录则等用户扫码（最多 180 秒）。
- 每次全流程开始前都执行一次（BOSS 登录态约 1 小时过期，发现+匹配一轮 30-60 分钟，先刷新可避免中途 409）。

## 步骤 2：岗位发现 + 简历匹配

- 命令：`python job_discovery.py --match-resume`（在后台运行，长任务，完成后会自动通知）。
- **铁律：同一时刻只允许一个 job_discovery 实例**。启动第二个会报 `HTTP 409 workflow already running`——这不是要修复的错误，先确认没有别的实例在跑（后台任务、未关闭的终端）。
- 模式：无参数时双模式自动轮换（`discovery_rotation_state.json` 的 `last_mode` 交替：预设方向轮询 3 个/批 ↔ 简历驱动）。显式插队：`--fuzzy`（14 个关键词全量）、`--directions "a,b"`、`--direction "x"`、`--goal "..."`、`--from-resume`。
- 搜索语义：关键词模式下岗位标题必须**包含关键词字样**（子串匹配，如「模型评测」命中「大模型评测工程师」）；简历驱动模式忽略关键词过滤，公共条件只保留「深圳 10K以上」，岗位取舍交给 DeepSeek。
- 搜索中途失败（疑似风控）会中止剩余任务并保留进度：重跑本脚本即可，已投递岗位自动去重。

## 步骤 3：DeepSeek 匹配（一般由 --match-resume 自动触发）

- 产物：`resume_matched_jobs.json`（≥60 分进投递池）、`resume_greeting_messages.json`（结合简历+JD 的专属招呼语）、`resume_low_score_jobs.json`（低分留档含欠缺理由）。
- 报错处理：
  - `finish_reason=content_filter` → 内容触发安全过滤，重试无用，告知用户检查输入；
  - `finish_reason=length` → 代码会自动翻倍 max_tokens（上限 16000）重试，属自愈，不用管；
  - 空内容 / HTTP 429 → 限流或服务抖动，等 10-20 分钟后重跑；
  - DeepSeek 持续不可用 → 拆成两步：先 `python job_discovery.py`（发现不依赖 DeepSeek），恢复后单独 `python resume_matcher.py`。
- 若单独跑匹配：确认 `ai_filtered_nonoutsourced.json` 是想要的那批岗位（发现脚本会覆盖它）。

## 步骤 4：批量投递

- 命令：`python run_contact_sender.py`（后台运行）。默认读 `resume_matched_jobs.json` + `resume_greeting_messages.json`；缺文件会明确报错提示先跑匹配，**绝不回退到通用招呼语**。
- 投递前的自动排除（加载阶段完成，不进入聊天）：
  - 已投递成功 + 已沟通过（`contact_results*.json` 去重）；
  - 含「实习生,实习」关键词（`--exclude-keywords` 可改）；
  - **年限要求 4 年及以上的岗位**（3 年以内含 3 年可投递；`EXPERIENCE_REQUIRED_PATTERN` 匹配「4年/5年/10年…（以上）」写法）。
- 熔断：连续失败 ≥2 个即停止（疑似风控/服务异常），已成功的进度保留，修复后重跑自动续上；`ALREADY_CONTACTED` 不计入失败。
- 辅助参数：`--dry-run` 空跑预览、`--start/--limit` 切片、`--no-exclude-sent` 强制重投。
- 用户反馈投递异常时，先看 `contact_results.json` 里对应岗位的 status/reason 定位。

## 关键配置位置（改逻辑前先和用户确认）

- `job_discovery.py` 顶部：`DEFAULT_DIRECTIONS`（14 个关键词）、`GOAL_SUFFIX`（关键词模式公共条件，默认「深圳 15K以上 全职 非外包」）、`RESUME_GOAL_SUFFIX`（简历驱动，默认「深圳 10K以上」）、`INTERVAL_MIN/MAX_SECONDS`、`MAX_SCROLL_PAGES`。
- `resume_matcher_config.json`：DeepSeek key/model、`score_threshold`（默认 60）、简历路径。
- `resume_matcher.py` 顶部：`MATCH_BATCH_SIZE`（每批比对岗位数，默认 3）、`DEFAULT_ASSUMED_SKILLS`（默认具备不扣分的技能，如 PRD/Figma/AI 工具）。
- `run_contact_sender.py` 顶部：`EXPERIENCE_REQUIRED_PATTERN`（年限排除规则）、`DELAY_SECONDS`、`FAIL_ABORT_THRESHOLD`。

## 汇报约定

每个阶段结束向用户简报：发现多少岗位、匹配通过多少/低分多少、投递成功/失败/跳过各多少、产物文件路径、下一轮默认进入哪种模式。遇到 409/熔断/DeepSeek 错误时给出下一步指令，不要静默重试超过 2 次。
