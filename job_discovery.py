#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOSS 直聘岗位发现脚本（VSCode 直接运行）
==========================================
在 VSCode 终端中运行：
    python job_discovery.py

功能：
1. 调用主应用 API 解释求职目标
2. 启动 BOSS 直聘岗位发现工作流
3. 轮询等待发现/过滤/匹配完成
4. 导出岗位到 ai_filtered_nonoutsourced.json
5. 可选调用 resume_matcher.py 做简历技能匹配

前置条件：
- Java 后端已启动
- BOSS 直聘已登录（登录态在 .profiles/boss/default）
"""

import argparse
import json
import random
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime


MAIN_API = "http://localhost:8081"
OUTPUT_JOBS_FILE = Path("ai_filtered_nonoutsourced.json")

# 已投递记录文件通配（读取所有 contact_results*.json 中 status=SUCCEEDED 的岗位）
SENT_RESULTS_GLOB = "contact_results*.json"

# 搜索方向/关键词清单：岗位标题包含其中任意一个关键词字样即命中（子串匹配，见 filter_jobs_by_keyword），
# 精细筛选交给 DeepSeek 简历匹配（resume_matcher.py）。
# 用于：预设方向轮询（每批 ROTATION_BATCH_SIZE 个）、--direction/--directions、--fuzzy（全部一次跑完）
DEFAULT_DIRECTIONS = [
    "AI 产品评测",
    "AI 产品质量",
    "大模型评测",
    "LLM 评测",
    "模型评测",
    "AI QA",
    "智能产品质量",
    "AI 产品测试",
    "初级 AI 应用产品",
    "AI 产品助理",
    "AI 应用产品经理",
    "企业 AI 应用",
    "知识库产品",
    "AI 解决方案助理",
]
GOAL_SUFFIX = "深圳 15K以上 全职 非外包"
# 简历驱动搜索（方式 B）的公共条件：只保留城市和薪资下限，不限上限、不限全职/外包，
# 岗位取舍完全由 DeepSeek 直接比对简历与岗位决定（忽略关键词清单的字样过滤）
RESUME_GOAL_SUFFIX = "深圳 10K以上"
# 每个关键词搜索完成后的间隔（秒），实际取值在 min~max 之间随机，避免固定节奏被识别为脚本
INTERVAL_MIN_SECONDS = 45
INTERVAL_MAX_SECONDS = 90
# 每个搜索最多翻页数（首页 + N 次滚动），限制单个关键词的操作量
MAX_SCROLL_PAGES = 3
# 方向轮询状态文件：记录当前周期的剩余方向，跑完自动开新周期
ROTATION_STATE_FILE = Path("discovery_rotation_state.json")
ROTATION_BATCH_SIZE = 3


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def contains_excluded_keywords(job: dict, keywords: list[str]) -> bool:
    """检查岗位标题或描述中是否包含排除关键词。"""
    text = f"{job.get('title', '')} {job.get('description', '')}".lower()
    return any(kw.lower() in text for kw in keywords)


def filter_excluded_jobs(jobs: list[dict], keywords: list[str]) -> tuple[list[dict], int]:
    """过滤掉包含排除关键词的岗位，返回过滤后的列表和跳过的数量。"""
    if not keywords:
        return jobs, 0
    filtered = []
    skipped = 0
    for job in jobs:
        if contains_excluded_keywords(job, keywords):
            skipped += 1
            log(f"跳过含排除关键词岗位：{job.get('title')} @ {job.get('company')}")
            continue
        filtered.append(job)
    return filtered, skipped


def load_sent_job_ids() -> set[str]:
    """读取所有已投递结果文件，返回投递成功的 platformJobId 集合。"""
    sent: set[str] = set()
    for result_file in Path(".").glob(SENT_RESULTS_GLOB):
        try:
            with result_file.open("r", encoding="utf-8") as f:
                records = json.load(f)
            for record in records:
                if record.get("status") == "SUCCEEDED" and record.get("platformJobId"):
                    sent.add(record["platformJobId"])
        except Exception as e:
            log(f"读取已投递记录 {result_file} 失败：{e}")
    return sent


def filter_sent_jobs(jobs: list[dict], sent_ids: set[str]) -> tuple[list[dict], int]:
    """过滤掉已投递成功的岗位，返回过滤后的列表和跳过的数量。"""
    if not sent_ids:
        return jobs, 0
    filtered = []
    skipped = 0
    for job in jobs:
        if job.get("platformJobId") in sent_ids:
            skipped += 1
            log(f"跳过已投递岗位：{job.get('title')} @ {job.get('company')}")
            continue
        filtered.append(job)
    return filtered, skipped


def _norm_title(text: str) -> str:
    """标题归一化：去所有空白字符并转小写，用于字样包含匹配。"""
    return re.sub(r"\s+", "", text or "").lower()


def filter_jobs_by_keyword(jobs: list[dict], keyword: str) -> tuple[list[dict], int]:
    """按「标题含关键词字样」过滤岗位：标题（去空格转小写）必须完整包含关键词字样。

    如「模型评测」→ 标题含「模型评测」即保留（子串匹配，不要求全等）。
    返回 (保留的岗位, 剔除的数量)。
    """
    key = _norm_title(keyword)
    if not key:
        return jobs, 0
    kept = [j for j in jobs if key in _norm_title(j.get("title", ""))]
    return kept, len(jobs) - len(kept)


def call_api(method: str, url: str, data: dict | None = None, timeout: int = 30) -> dict:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        raise RuntimeError(f"HTTP {e.code}: {err_body}") from e


def health_check() -> None:
    try:
        call_api("GET", f"{MAIN_API}/actuator/health", timeout=5)
    except Exception as e:
        raise RuntimeError(f"Java 后端未启动（{e}）")


def interpret_goal(goal_text: str) -> dict:
    """解释求职目标，返回 goal 对象。"""
    log(f"解释求职目标：{goal_text[:60]}...")
    resp = call_api(
        "POST",
        f"{MAIN_API}/api/recruitment/goals/interpret",
        {"goal": goal_text},
        timeout=120,
    )
    log(f"目标已激活，goalId={resp.get('id')}")
    return resp


def start_workflow(platform: str, goal_id: int) -> dict:
    """启动指定平台的岗位发现工作流。"""
    log(f"启动 {platform} 岗位发现工作流...")
    resp = call_api(
        "POST",
        f"{MAIN_API}/api/recruitment/workflows",
        {"platform": platform, "goalId": goal_id},
        timeout=60,
    )
    log(f"工作流已启动，taskId={resp.get('taskId')}")
    return resp


def poll_workflow(task_id: str, max_wait_seconds: int = 600) -> dict:
    """轮询工作流状态，直到发现/过滤/匹配完成或失败。"""
    log("开始轮询工作流状态...")
    start = time.time()
    while time.time() - start < max_wait_seconds:
        snapshot = call_api(
            "GET",
            f"{MAIN_API}/api/recruitment/workflows/{task_id}",
            timeout=30,
        )
        status = snapshot.get("status")
        stage = snapshot.get("stage")
        discovered = snapshot.get("discovered", 0)
        matched = snapshot.get("matched", 0)
        error = snapshot.get("error")

        log(f"状态：{status} | 阶段：{stage} | 发现 {discovered} 个 | 匹配 {matched} 个")

        if status in ("AWAITING_CONFIRMATION", "COMPLETED"):
            return snapshot
        if status == "FAILED":
            raise RuntimeError(f"工作流失败：{error}")

        time.sleep(5)

    raise RuntimeError("轮询超时，工作流未完成")


def run_one_search(goal_text: str, platform: str, max_wait: int) -> list[dict]:
    """用指定求职目标跑一轮岗位发现工作流，返回岗位列表。"""
    goal = interpret_goal(goal_text)
    goal_id = goal.get("id")
    if not goal_id:
        raise RuntimeError("无法获取 goalId")

    workflow = start_workflow(platform, goal_id)
    task_id = workflow.get("taskId")
    if not task_id:
        raise RuntimeError("无法获取 taskId")

    snapshot = poll_workflow(task_id, max_wait_seconds=max_wait)
    return snapshot.get("jobs", [])


def merge_jobs(job_lists: list[list[dict]]) -> list[dict]:
    """合并多轮搜索结果，按 platformJobId 去重。"""
    merged: dict[str, dict] = {}
    for jobs in job_lists:
        for job in jobs:
            job_id = job.get("platformJobId")
            if job_id:
                merged[job_id] = job
    return list(merged.values())


# ============ 方向轮询 ============
# 每次运行自动取 ROTATION_BATCH_SIZE 个未跑的方向，跑完标记；
# 全部方向跑完后自动开启新周期（重新打乱顺序），如此循环。

def load_rotation_state() -> dict:
    if ROTATION_STATE_FILE.exists():
        try:
            with ROTATION_STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"读取轮询状态失败（将重新开始一个周期）：{e}")
    return {"cycle": 0, "remaining": [], "completed": {}}


def save_rotation_state(state: dict) -> None:
    with ROTATION_STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def pick_rotation_batch(state: dict) -> list[str]:
    """从状态里取下一批要跑的方向；剩余为空则开启新周期。"""
    if not state.get("remaining"):
        state["remaining"] = DEFAULT_DIRECTIONS.copy()
        random.shuffle(state["remaining"])
        state["cycle"] = state.get("cycle", 0) + 1
        state["completed"] = {}
        log(f"方向轮询：第 {state['cycle']} 周期开始（{len(state['remaining'])} 个方向已重新打乱）")
    return state["remaining"][:ROTATION_BATCH_SIZE]


def mark_direction_done(state: dict, direction: str) -> None:
    if direction in state.get("remaining", []):
        state["remaining"].remove(direction)
    state.setdefault("completed", {})[direction] = datetime.now().isoformat(timespec="seconds")
    save_rotation_state(state)


def generate_directions_from_resume(config: dict, resume_text: str) -> list[str]:
    """让 AI 根据简历内容生成适合招聘平台搜索的岗位方向关键词列表。"""
    log("调用 AI 分析简历，生成搜索方向...")
    system_prompt = (
        "你是一名求职顾问。根据候选人简历，生成适合在 BOSS 直聘搜索的岗位方向关键词。"
        "输出必须是严格 JSON 字符串数组，不要包含 Markdown 代码块或额外说明。"
    )
    user_prompt = f"""【简历内容】
{resume_text[:4000]}

请生成 8-12 个岗位搜索方向关键词，要求：
1. 每个关键词不超过 12 个字，是招聘平台上真实存在的岗位方向（如：大模型评测、AI产品经理）
2. 围绕简历中的核心技能和经历，同时覆盖候选人有能力胜任、但自己可能没想到的方向
3. 不要输出任何解释，只输出 JSON 字符串数组，例如：["方向1", "方向2", "方向3"]"""
    from resume_matcher import call_deepseek
    content = call_deepseek(config, [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[-1].strip()
    directions = json.loads(cleaned)
    if not isinstance(directions, list):
        raise RuntimeError("AI 返回的不是列表")
    result = [str(d).strip() for d in directions if str(d).strip()]
    if not result:
        raise RuntimeError("AI 未返回任何方向")
    return result[:12]


def build_resume_searches() -> list[tuple[str, str]]:
    """简历驱动（方式 B）：AI 根据简历生成查询方向，公共条件只保留「深圳 10-15K」，
    搜回来的岗位不做关键词字样过滤，直接交给 DeepSeek 比对简历与岗位。
    失败则回退到预设方向列表。返回 (方向名, goal文本) 列表。"""
    try:
        from resume_matcher import load_config, read_resume_docx
        config = load_config()
        resume_text = read_resume_docx(config["resume_path"])
        batch = generate_directions_from_resume(config, resume_text)
        searches = [(d, f"{d} {RESUME_GOAL_SUFFIX}") for d in batch]
        log(f"简历驱动搜索：AI 生成了 {len(searches)} 个方向（公共条件：{RESUME_GOAL_SUFFIX}，忽略关键词清单过滤）")
        for d, _ in searches:
            log(f"  - {d}")
        return searches
    except Exception as e:
        log(f"简历生成方向失败（{e}），回退到预设方向列表")
        batch = DEFAULT_DIRECTIONS.copy()
        random.shuffle(batch)
        return [(d, f"{d} {GOAL_SUFFIX}") for d in batch]


def save_jobs(jobs: list[dict]) -> None:
    with OUTPUT_JOBS_FILE.open("w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    log(f"岗位已保存：{OUTPUT_JOBS_FILE.resolve()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BOSS 直聘岗位发现脚本（关键词模糊搜索 + 简历驱动双模式轮换）")
    parser.add_argument(
        "--goal",
        type=str,
        default=None,
        help="自定义完整求职目标文本，只跑这一次搜索（不过滤）",
    )
    parser.add_argument(
        "--direction",
        type=str,
        default=None,
        help="只跑某一个方向（如：大模型评测），自动拼接公共条件，标题须含该关键词字样",
    )
    parser.add_argument(
        "--directions",
        type=str,
        default=None,
        help="跑指定的一批方向，逗号分隔（如：大模型评测,LLM 评测,AI QA）；不给则自动轮询下一批 3 个方向",
    )
    parser.add_argument(
        "--fuzzy",
        action="store_true",
        help="模糊搜索：DEFAULT_DIRECTIONS 全部 14 个关键词一次跑完（不占用轮询进度），标题含字样即命中",
    )
    parser.add_argument(
        "--from-resume",
        action="store_true",
        help="简历驱动：AI 根据简历生成方向，只保留「深圳 10-15K」，忽略关键词清单过滤，直接由 DeepSeek 比对简历与岗位",
    )
    parser.add_argument(
        "--platform",
        type=str,
        default="boss",
        help="招聘平台（默认 boss）",
    )
    parser.add_argument(
        "--max-wait",
        type=int,
        default=600,
        help="最大轮询等待秒数（默认 600）",
    )
    parser.add_argument(
        "--match-resume",
        action="store_true",
        help="发现完成后自动调用 resume_matcher.py 做简历匹配",
    )
    parser.add_argument(
        "--no-match-resume",
        action="store_true",
        help="发现完成后不调用 resume_matcher.py",
    )
    parser.add_argument(
        "--exclude-keywords",
        type=str,
        default="实习生,实习",
        help="排除包含这些关键词的岗位，逗号分隔（默认：实习生,实习）",
    )
    parser.add_argument(
        "--no-exclude-sent",
        action="store_true",
        help="默认会排除已投递成功的岗位，加此参数可关闭该去重",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    log("检查服务状态...")
    health_check()
    log("服务正常")

    # 组装搜索任务列表：fuzzy > from-resume > 自定义 goal > 单方向 > 一批方向 > 双模式轮换（默认）
    # keyword_filter 为 True 时，搜回的岗位须标题含关键词字样（方式 A）；False 则不过滤（方式 B / 自定义 goal）
    rotation_state = None
    keyword_filter = True
    if args.fuzzy:
        log(f"本轮模式：模糊搜索（{len(DEFAULT_DIRECTIONS)} 个关键词一次跑完，标题含字样即命中）")
        keywords = DEFAULT_DIRECTIONS.copy()
        random.shuffle(keywords)
        searches = [(kw, f"{kw} {GOAL_SUFFIX}") for kw in keywords]
    elif args.from_resume:
        log("本轮模式：简历驱动搜索（只保留「深圳 10-15K」，忽略关键词清单，DeepSeek 直接比对简历与岗位）")
        keyword_filter = False
        searches = build_resume_searches()
    elif args.goal:
        keyword_filter = False
        searches = [("自定义目标", args.goal)]
    elif args.direction:
        searches = [(args.direction, f"{args.direction} {GOAL_SUFFIX}")]
    elif args.directions:
        batch = [d.strip() for d in args.directions.split(",") if d.strip()]
        searches = [(d, f"{d} {GOAL_SUFFIX}") for d in batch]
    else:
        # 双模式轮换：预设方向轮询 与 简历驱动 交替执行，模式记录在状态文件 last_mode 字段
        rotation_state = load_rotation_state()
        if rotation_state.get("last_mode", "preset") == "resume":
            # 上次跑的是简历驱动，本次用预设轮询
            rotation_state["last_mode"] = "preset"
            batch = pick_rotation_batch(rotation_state)
            save_rotation_state(rotation_state)
            searches = [(d, f"{d} {GOAL_SUFFIX}") for d in batch]
            log(
                f"方向轮询（预设方向）：第 {rotation_state['cycle']} 周期，"
                f"本批 {len(searches)} 个方向（{len(rotation_state['remaining'])} 个待跑）"
            )
        else:
            # 上次跑的是预设轮询（或无记录），本次用简历驱动
            rotation_state["last_mode"] = "resume"
            save_rotation_state(rotation_state)
            keyword_filter = False
            log("本轮模式：简历驱动搜索（AI 根据简历生成方向；只保留「深圳 10-15K」，忽略关键词清单）")
            searches = build_resume_searches()

    log(f"本轮共 {len(searches)} 个搜索任务，任务间隔 {INTERVAL_MIN_SECONDS}-{INTERVAL_MAX_SECONDS} 秒随机")

    # 逐个搜索任务跑；某个失败（疑似触发风控/登录失效）立即中止剩余任务
    all_jobs: list[list[dict]] = []
    failed_searches = []
    for index, (name, goal_text) in enumerate(searches):
        log(f"===== [{index + 1}/{len(searches)}] 搜索：{name} =====")
        try:
            jobs = run_one_search(goal_text, args.platform, args.max_wait)
            # 标题字样过滤：关键词模式下岗位标题必须包含该关键词字样（子串匹配，不要求全等）；
            # 简历驱动/自定义 goal 模式不过滤，交给 DeepSeek 直接比对
            if keyword_filter:
                jobs, dropped = filter_jobs_by_keyword(jobs, name)
                if dropped:
                    log(f"「{name}」标题字样过滤：剔除 {dropped} 个不含关键词的岗位，保留 {len(jobs)} 个")
            log(f"「{name}」发现 {len(jobs)} 个岗位")
            all_jobs.append(jobs)
            if rotation_state is not None:
                mark_direction_done(rotation_state, name)
        except Exception as e:
            log(f"「{name}」搜索失败：{e}")
            log("疑似触发风控或登录失效，立即中止剩余任务！请检查浏览器窗口，稍后重跑（失败任务已保留，不会跳过）")
            failed_searches.append(name)
            break

        if index < len(searches) - 1:
            interval = random.randint(INTERVAL_MIN_SECONDS, INTERVAL_MAX_SECONDS)
            log(f"间隔 {interval} 秒后搜索下一个任务...")
            time.sleep(interval)

    if rotation_state is not None:
        if not rotation_state["remaining"]:
            log(f"第 {rotation_state['cycle']} 周期全部方向跑完，下次运行将开启新周期")
        else:
            log(f"本周期剩余 {len(rotation_state['remaining'])} 个方向：{'、'.join(rotation_state['remaining'])}")

    jobs = merge_jobs(all_jobs)
    log(f"合并去重后共 {len(jobs)} 个岗位（来自 {len(all_jobs)} 个成功搜索任务）")
    if failed_searches:
        log(f"失败任务 {len(failed_searches)} 个：{'、'.join(failed_searches)}，可重跑本脚本补跑（已投递岗位会自动去重）")

    # 过滤排除关键词
    exclude_keywords = [kw.strip() for kw in args.exclude_keywords.split(",") if kw.strip()]
    jobs, skipped_count = filter_excluded_jobs(jobs, exclude_keywords)
    if skipped_count:
        log(f"因含排除关键词跳过 {skipped_count} 个，剩余 {len(jobs)} 个")

    # 过滤已投递成功的岗位（BOSS 平台本身不拦截已沟通过的岗位，会反复出现在搜索结果里）
    if args.no_exclude_sent:
        log("已禁用已投递去重")
    else:
        sent_ids = load_sent_job_ids()
        jobs, sent_skipped = filter_sent_jobs(jobs, sent_ids)
        if sent_skipped:
            log(f"因已投递成功跳过 {sent_skipped} 个（来自 {len(sent_ids)} 条历史记录），剩余 {len(jobs)} 个")

    if not jobs:
        log("过滤后没有可用岗位，请调整求职目标或排除关键词")
        return

    log(f"匹配到 {len(jobs)} 个岗位")

    # 保存（只存岗位列表，不生成占位招呼语；招呼语由 resume_matcher.py 结合简历+JD 生成）
    save_jobs(jobs)

    # 简历匹配
    if args.match_resume and not args.no_match_resume:
        log("开始简历技能匹配...")
        import subprocess
        result = subprocess.run(
            ["python", "resume_matcher.py"],
            cwd=Path.cwd(),
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError("简历匹配失败")

    log("=" * 40)
    log("岗位发现完成")
    if args.match_resume:
        log("已生成 resume_matched_jobs.json 和 resume_greeting_messages.json")
        log("投递命令：python run_contact_sender.py")
    else:
        log("尚未生成招呼语。投递前请先运行：python resume_matcher.py")
        log("然后再运行：python run_contact_sender.py")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"脚本异常退出：{e}")
        raise
