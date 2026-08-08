#!/usr/bin/env python3
"""抓取央视新闻 — JSONP API 多页 | QPS<5"""

import json, os, time, hashlib, random, sys, io
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

BASE_DIR = Path(__file__).parent
TODAY = datetime.now().strftime("%Y%m%d")
OUT_DIR = BASE_DIR / TODAY
OUT_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
QPS_MIN, QPS_MAX = 1.2, 4.5
results = {"skills": [], "urls_seen": set(), "hashes_seen": set()}

# CCTV JSONP API端点 (分页: page_1 到 page_5)
CCTV_WORLD_PAGES = [f"https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/world_{p}.jsonp" for p in range(1, 6)]
CCTV_ECONOMY_PAGES = [f"https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/economy_{p}.jsonp" for p in range(1, 6)]


def safe_sleep(sec=None):
    time.sleep(sec if sec else random.uniform(QPS_MIN, QPS_MAX))


def url_hash(url):
    return hashlib.md5(url.encode()).hexdigest()[:12]


def add_skill(title, url, source, tags, body="", typ="article"):
    if not url or url in results["urls_seen"]:
        return False
    h = url_hash(url)
    if h in results["hashes_seen"]:
        results["urls_seen"].add(url)
        return False
    results["hashes_seen"].add(h)
    results["urls_seen"].add(url)
    
    safe_title = "".join(c for c in title[:30] if c.isalnum() or c in " _-").strip()[:20]
    path = OUT_DIR / f"{TODAY}_{typ}_{h}_{safe_title}.md"
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n> 来源: {source} | 日期: {TODAY} | 类型: {typ}\n")
        f.write(f"> 原文: {url} | 标签: {', '.join(tags)}\n## 正文\n{body or title}\n")
    
    results["skills"].append({
        "id": h, "title": title[:80], "url": url, "source": source,
        "tags": tags, "type": typ, "date": TODAY, "content": (body or title)[:200]
    })
    return True


def fetch_jsonp(url):
    safe_sleep(random.uniform(2, 4))
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.encoding = "utf-8"
        text = r.text
        start = text.index("(") + 1
        end = text.rindex(")")
        return json.loads(text[start:end])
    except Exception as e:
        return None


def scrape_cctv_paginated(name, page_urls, target_count=200):
    """多页抓取CCTV新闻"""
    print(f"\n[{name}] {len(page_urls)}页")
    total = 0
    
    for page_idx, api_url in enumerate(page_urls):
        if total >= target_count:
            break
        
        data = fetch_jsonp(api_url)
        if not data:
            print(f"  第{page_idx+1}页: 失败")
            continue
        
        # 提取新闻列表
        news_list = None
        if isinstance(data, dict):
            d = data.get("data", data)
            if isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, list) and v:
                        news_list = v
                        break
            elif isinstance(d, list):
                news_list = d
        
        if not news_list:
            print(f"  第{page_idx+1}页: 空")
            continue
        
        page_count = 0
        for item in news_list:
            if total >= target_count:
                break
            title = item.get("title", "")
            url = item.get("url", "")
            brief = item.get("brief", "")
            if not title or len(title) < 5:
                continue
            if url and not url.startswith("http"):
                url = "https://news.cctv.com" + url
            if add_skill(title[:80], url, name, ["新闻", "央视"], brief or title):
                total += 1
                page_count += 1
        
        print(f"  第{page_idx+1}页: +{page_count}条 (累计{total})")
        if page_count == 0:
            break
    
    print(f"  {name}: {total}条")
    return total


def save_index():
    existing_path = OUT_DIR / "skill_index.json"
    existing = {"skills": []}
    if existing_path.exists():
        try:
            with open(existing_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except:
            pass
    
    seen_ids = {s["id"] for s in existing["skills"]}
    for s in results["skills"]:
        if s["id"] not in seen_ids:
            existing["skills"].append(s)
            seen_ids.add(s["id"])
    
    existing["date"] = TODAY
    existing["total"] = len(existing["skills"])
    existing["sources"] = existing.get("sources", {})
    existing["sources"]["cctv_world"] = sum(1 for s in existing["skills"] if "国际新闻" in s.get("source", ""))
    existing["sources"]["cctv_economy"] = sum(1 for s in existing["skills"] if "经济新闻" in s.get("source", ""))
    
    with open(existing_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    
    print(f"\n  索引合并: {existing_path} (总计{existing['total']}条)")


if __name__ == "__main__":
    print("=" * 60)
    print(f"  央视新闻抓取 (JSONP API 多页) | {TODAY}")
    print(f"  目标: 国际200 + 经济200 | QPS: {QPS_MIN}-{QPS_MAX}s")
    print("=" * 60)
    
    t0 = time.time()
    
    c1 = scrape_cctv_paginated("央视国际新闻", CCTV_WORLD_PAGES, 200)
    c2 = scrape_cctv_paginated("央视经济新闻", CCTV_ECONOMY_PAGES, 200)
    
    save_index()
    
    elapsed = time.time() - t0
    print(f"\n  总计: {c1+c2}条 | 耗时: {elapsed:.0f}s")
