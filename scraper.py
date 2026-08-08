#!/usr/bin/env python3
"""
华尔街见闻 9频道并行抓取器 — GitHub Actions 版
每小时整点运行（北京 6:00-23:00），每次目标 ~500 条。
纯标准库，零外部依赖。
"""

import json
import os
import re
import ssl
import sys
import hashlib
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# ── 配置 ──────────────────────────────────────────────
TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_DIR = os.path.join(BASE_DIR, "news")
ARCHIVE_DIR = os.path.join(BASE_DIR, "news-archive")
os.makedirs(NEWS_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

CHANNELS = [
    "global-channel", "a-stock-channel", "us-stock-channel",
    "xgb-channel", "commodity-channel", "forex-channel",
    "bond-channel", "financing-channel", "tmt-channel",
]

CH_LABEL = {
    "a-stock-channel": "A股", "us-stock-channel": "美股",
    "xgb-channel": "港股", "hk-stock-channel": "港股",
    "commodity-channel": "大宗商品", "oil-channel": "能源",
    "global-channel": "宏观", "forex-channel": "外汇",
    "bond-channel": "债券", "financing-channel": "金融",
    "tmt-channel": "TMT", "ai-channel": "AI",
}

MAX_FILE_MB = 1.0
MAX_ITEMS_TRUNCATE = 5000
DAYS_ARCHIVE_MIN = 8
DAYS_ARCHIVE_MAX = 14
INDEX_RETAIN_DAYS = 30

# ── 工具函数 ──────────────────────────────────────────
def ts_to_iso(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=TZ)
    return dt.strftime("%Y-%m-%dT%H:%M")

def today_str() -> str:
    return date.today().isoformat()

def is_monday_6am() -> bool:
    """判断当前是否为周一 6:00（北京）"""
    now = datetime.now(TZ)
    return now.weekday() == 0 and now.hour == 6

def make_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

# ── 频道抓取 ──────────────────────────────────────────
def fetch_channel(channel: str) -> list[dict]:
    """抓取单个频道，返回有效条目列表"""
    url = f"https://api-one.wallstcn.com/apiv1/content/lives?channel={channel}&limit=200&first=0"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=20, context=make_ssl_context())
        data = json.loads(resp.read().decode("utf-8"))
        items = []
        for item in data.get("data", {}).get("items", []):
            content = (item.get("content_text") or "").strip()
            if not content:
                continue
            ch0 = (item.get("channels") or [None])[0] or channel
            items.append({
                "id": item["id"],
                "ts": item["display_time"],
                "content": content,
                "channel": ch0,
            })
        print(f"  {channel}: {len(data.get('data',{}).get('items',[]))} raw → {len(items)} valid")
        return items
    except Exception as e:
        print(f"  {channel}: ERROR — {e}")
        return []

def norm_text(s: str) -> str:
    """归一化文本：去空白、去标点，仅保留字母数字与中文，用于内容去重"""
    s = (s or "").lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", s)
    return s

def content_hash(s: str) -> Optional[str]:
    """内容指纹：过短文本不可靠，返回 None；否则返回 md5 前缀"""
    n = norm_text(s)
    if len(n) < 8:
        return None
    return hashlib.md5(n.encode("utf-8")).hexdigest()[:16]

def load_recent_ids_exclude_today(date_str: str, days: int = 2) -> tuple[set, set]:
    """
    加载今天之前最近 days 天的所有新闻 id + 内容哈希。
    用于跨日去重：若某条新闻已在前几天出现过，则今日不再重复存储。
    返回 (id_set, hash_set)。
    """
    ids: set = set()
    hashes: set = set()
    try:
        base = date.fromisoformat(date_str)
    except ValueError:
        return ids, hashes
    for delta in range(1, days + 1):
        d = (base - timedelta(days=delta)).isoformat()
        fp = os.path.join(NEWS_DIR, f"{d}.json")
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            for it in data.get("items", []):
                iid = it.get("i") or it.get("id")
                if iid:
                    ids.add(iid)
                h = content_hash(it.get("c", ""))
                if h:
                    hashes.add(h)
        except Exception:
            continue
    return ids, hashes

def scrape_all() -> list[dict]:
    """并行抓取全部 9 个频道，合并去重"""
    print("[1] 抓取 9 个频道…")
    all_raw = []
    for ch in CHANNELS:
        all_raw.extend(fetch_channel(ch))

    print(f"    原始有效条目: {len(all_raw)}")

    # 按 id 去重（保留首次出现）
    seen = set()
    unique = []
    for r in all_raw:
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)

    print(f"    跨频道去重后: {len(unique)}")
    return unique

# ── 转换与合并 ────────────────────────────────────────
def convert_items(raw_items: list[dict]) -> list[dict]:
    """将原始条目转换为紧凑格式"""
    result = []
    for r in raw_items:
        cl = r["channel"]
        label = CH_LABEL.get(cl, cl)
        result.append({
            "i": r["id"],
            "t": ts_to_iso(r["ts"]),
            "c": r["content"],
            "ch": label,
            "cl": cl,
            "u": f"/livenews/{r['id']}",
        })
    return result

def load_existing(date_str: str) -> tuple[set, list]:
    """加载已有数据，返回 (id_set, items_list)"""
    filepath = os.path.join(NEWS_DIR, f"{date_str}.json")
    if not os.path.exists(filepath):
        return set(), []
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    ids = {item["i"] for item in data.get("items", [])}
    return ids, data.get("items", [])

def save_daily(date_str: str, all_items: list):
    """保存当日文件，超 1MB 截断"""
    total = len(all_items)
    output = json.dumps(
        {"date": date_str, "count": total, "items": all_items},
        ensure_ascii=False, separators=(",", ":"),
    )
    size_mb = len(output.encode("utf-8")) / 1024 / 1024
    if size_mb > MAX_FILE_MB:
        print(f"    ⚠ 文件 {size_mb:.2f}MB > {MAX_FILE_MB}MB，截断至 {MAX_ITEMS_TRUNCATE} 条")
        all_items = all_items[:MAX_ITEMS_TRUNCATE]
        total = len(all_items)
        output = json.dumps(
            {"date": date_str, "count": total, "items": all_items},
            ensure_ascii=False, separators=(",", ":"),
        )

    filepath = os.path.join(NEWS_DIR, f"{date_str}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(output)
    size_kb = os.path.getsize(filepath) / 1024
    print(f"    保存: {total} 条, {size_kb:.1f}KB")
    return total

# ── 索引更新 ──────────────────────────────────────────
def update_index(date_str: str):
    """更新 news/index.json"""
    idx_path = os.path.join(NEWS_DIR, "index.json")
    if os.path.exists(idx_path):
        with open(idx_path, "r", encoding="utf-8") as f:
            idx = json.load(f)
    else:
        idx = {"dates": [], "updatedAt": ""}

    dates = idx.get("dates", [])
    # 确保 today 在首位
    if date_str in dates:
        dates.remove(date_str)
    dates.insert(0, date_str)

    # 保留最近 30 天
    today_dt = date.today()
    dates = [d for d in dates if (today_dt - date.fromisoformat(d)).days <= INDEX_RETAIN_DAYS]

    idx["dates"] = dates
    idx["updatedAt"] = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
    print(f"    索引更新: {len(dates)} 个日期")

# ── 周度归档（仅周一 6:00） ───────────────────────────
def week_archive():
    """将 8-14 天前的 daily 文件合并为周度归档"""
    today_dt = date.today()
    idx_path = os.path.join(NEWS_DIR, "index.json")
    if not os.path.exists(idx_path):
        return

    with open(idx_path, "r", encoding="utf-8") as f:
        idx = json.load(f)

    # 找出 8-14 天前的日期
    to_archive = []
    for d in idx.get("dates", []):
        try:
            dt = date.fromisoformat(d)
            delta = (today_dt - dt).days
            if DAYS_ARCHIVE_MIN <= delta <= DAYS_ARCHIVE_MAX:
                to_archive.append(d)
        except ValueError:
            pass

    if not to_archive:
        print("[归档] 没有需要归档的日期")
        return

    print(f"[归档] 待合并: {to_archive}")

    # 按周分组
    weeks = {}
    for d in to_archive:
        dt = date.fromisoformat(d)
        iso = dt.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        weeks.setdefault(week_key, []).append(d)

    for week_key, days in weeks.items():
        days.sort()
        merged_items = []
        for d in days:
            filepath = os.path.join(NEWS_DIR, f"{d}.json")
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 去重合并
                seen = {item["i"] for item in merged_items}
                for item in data.get("items", []):
                    if item["i"] not in seen:
                        seen.add(item["i"])
                        merged_items.append(item)

        archive_path = os.path.join(ARCHIVE_DIR, f"{week_key}.json")
        archive_data = {
            "week": week_key,
            "dates": days,
            "count": len(merged_items),
            "items": merged_items,
        }
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(archive_data, f, ensure_ascii=False, separators=(",", ":"))
        print(f"    归档: {week_key} ({len(days)}天, {len(merged_items)}条, {os.path.getsize(archive_path)/1024:.1f}KB)")

        # 删除旧 daily
        for d in days:
            df = os.path.join(NEWS_DIR, f"{d}.json")
            if os.path.exists(df):
                os.remove(df)
                print(f"    删除: {d}.json")

    # 更新 news-archive/index.json
    arch_idx_path = os.path.join(ARCHIVE_DIR, "index.json")
    if os.path.exists(arch_idx_path):
        with open(arch_idx_path, "r", encoding="utf-8") as f:
            arch_idx = json.load(f)
    else:
        arch_idx = {"updatedAt": "", "weeks": []}

    existing_weeks = set(arch_idx.get("weeks", []))
    for wk in weeks:
        existing_weeks.add(wk)
    arch_idx["weeks"] = sorted(existing_weeks, reverse=True)
    arch_idx["updatedAt"] = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    with open(arch_idx_path, "w", encoding="utf-8") as f:
        json.dump(arch_idx, f, ensure_ascii=False, separators=(",", ":"))

    # 从 index 移除已归档日期
    remaining = [d for d in idx.get("dates", []) if d not in to_archive]
    idx["dates"] = remaining
    idx["updatedAt"] = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))

    print(f"    归档完成: {len(to_archive)} 天已合并, 索引剩余 {len(remaining)} 天")

# ── 主流程 ────────────────────────────────────────────
def main():
    date_str = today_str()
    print(f"═══ 交易复盘台 · 新闻抓取 ═══")
    print(f"日期: {date_str}  |  时间: {datetime.now(TZ).strftime('%H:%M')}")

    # 周一 6:00 先执行归档
    if is_monday_6am():
        print("[归档] 周一 6:00 — 执行周度归档")
        week_archive()

    # 抓取 + 处理
    raw_items = scrape_all()
    conv_items = convert_items(raw_items)

    # 加载已有数据，计算新增
    existing_ids, existing_items = load_existing(date_str)
    # 跨日去重：排除前 2 天已存在的新闻（id 或内容哈希），保证 news/ 无重复
    recent_ids, recent_hashes = load_recent_ids_exclude_today(date_str, days=2)
    dup_cross_day = 0
    def _is_duplicate(item: dict) -> bool:
        nonlocal dup_cross_day
        if item["i"] in existing_ids or item["i"] in recent_ids:
            dup_cross_day += 1
            return True
        h = content_hash(item.get("c", ""))
        if h and h in recent_hashes:
            dup_cross_day += 1
            return True
        return False
    new_items = [item for item in conv_items if not _is_duplicate(item)]
    print(f"\n[2] 合并: 存量 {len(existing_ids)} + 新增 {len(new_items)}"
          f" | 跨日去重 {dup_cross_day} 条")

    # 合并（新条目在前）
    all_items = new_items + existing_items
    total = save_daily(date_str, all_items)

    # 更新索引
    print("[3] 更新索引")
    update_index(date_str)

    # 输出 GitHub Actions 可读取的摘要
    summary = f"SUMMARY:new={len(new_items)}:total={total}:size_kb={os.path.getsize(os.path.join(NEWS_DIR, f'{date_str}.json'))//1024}"
    print(f"\n{summary}")

    # 写入 GitHub Actions output
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"new_count={len(new_items)}\n")
            f.write(f"total_count={total}\n")

    print("═══ 完成 ═══")
    return len(new_items), total


if __name__ == "__main__":
    main()
