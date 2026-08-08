#!/usr/bin/env python3
"""
CCTV新闻快速抓取器 — API直连, 无浏览器依赖
==============================================
源3: https://news.cctv.com/world/      → 200条国际新闻
源4: https://jingji.cctv.com/          → 200条经济新闻

QPS<5 | 间隔1-3s | 自动去重 | 追加到现有skill_index.json
输出: 新闻时事理念/20260707/skill_index.json (追加模式)
"""
import os, sys, io, json, time, random, hashlib, re
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).parent
TODAY = "20260707"  # 固定日期
DATA_DIR = BASE / TODAY
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ==================== QPS控制 ====================
class RL:
    def __init__(self):
        self.last = 0; self.cnt = 0; self.ws = time.time()
    def wait(self):
        self.cnt += 1; now = time.time()
        if now - self.ws >= 1.0: self.cnt = 0; self.ws = now
        if self.cnt >= 4: time.sleep(max(0, 1.0 - (now - self.ws))); self.cnt = 0; self.ws = time.time()
        gap = time.time() - self.last
        if gap < 1: time.sleep(random.uniform(1, 3))
        self.last = time.time()

rl = RL()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ==================== 索引管理 ====================
IDX_FILE = DATA_DIR / "skill_index.json"

def load_index():
    if IDX_FILE.exists():
        with open(IDX_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {s["url"] for s in d["skills"]}, d["skills"]
    return set(), []

def save_index(skills):
    seen = set(); uniq = []
    for s in skills:
        sid = s.get("id", s.get("url", ""))
        if sid not in seen: seen.add(sid); uniq.append(s)
    with open(IDX_FILE, "w", encoding="utf-8") as f:
        json.dump({"total":len(uniq), "updated":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "sources":["wallstreetcn.com","cctv.com"], "date":TODAY, "skills":uniq},
                  f, ensure_ascii=False, indent=2)
    return uniq

def safe_name(t): return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]+', '_', str(t))[:50].strip('_')

def save_md(item):
    fn = f"{item['date']}_{item['type']}_{item['id']}_{safe_name(item['title'])}.md"
    fn = (DATA_DIR / fn[:200])
    with open(fn, "w", encoding="utf-8") as f:
        f.write(f"# {item['title']}\n> 来源:{item.get('source','')} | {item['date']} | {item['type']}\n> {item['url']} | 标签:{','.join(item.get('tags',[]))}\n## 正文\n{item.get('content','')}\n---\n")

# ==================== CCTV JSONP API ====================
try:
    import requests
except:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36","Referer":"https://news.cctv.com/"}

# CCTV API: 按页获取
CCTV_WORLD_URLS = [f"https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/world_{p}.jsonp" for p in range(1, 11)]
CCTV_ECON_URLS = [f"https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/economy_{p}.jsonp" for p in range(1, 11)]

def fetch_cctv_page(url):
    """获取1页CCTV新闻列表"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.encoding = "utf-8"; txt = r.text
        s = txt.index("(") + 1; e = txt.rindex(")")
        return json.loads(txt[s:e])
    except: return None

def extract_news_list(data):
    """从CCTV JSONP响应中提取新闻列表"""
    if isinstance(data, dict):
        d = data.get("data", data)
        if isinstance(d, dict):
            for v in d.values():
                if isinstance(v, list) and v: return v
        elif isinstance(d, list): return d
    return []

def scrape_cctv_source(name, urls, source_label, target=200):
    """抓取CCTV指定栏目, 追加到已有skill_index"""
    existing_urls, existing_skills = load_index()
    existing_urls.update(ALL_URLS)
    
    new_items = 0
    log(f"[{name}] 开始, 目标{target}条")
    
    for pi, url in enumerate(urls):
        if new_items >= target: break
        rl.wait()
        data = fetch_cctv_page(url)
        if not data:
            log(f"  第{pi+1}页: 请求失败"); continue
        
        items = extract_news_list(data)
        if not items:
            log(f"  第{pi+1}页: 无数据"); break
        
        page_added = 0
        for it in items:
            if new_items >= target: break
            title = it.get("title", ""); news_url = it.get("url", "")
            brief = it.get("brief", "")
            if not title or len(title) < 5: continue
            if news_url and not news_url.startswith("http"): news_url = "https://news.cctv.com" + news_url
            if not news_url or news_url in existing_urls: continue
            
            uid = hashlib.md5((news_url or title).encode()).hexdigest()[:10]
            item = {"id":uid, "title":title[:80], "content":(brief or title)[:3000],
                    "url":news_url, "type":"article", "date":TODAY,
                    "time":datetime.now().strftime("%H:%M"), "source":source_label,
                    "tags":["新闻","央视"], "voteup_count":0}
            save_md(item)
            existing_urls.add(news_url); ALL_URLS.add(news_url)
            existing_skills.append({"id":uid,"title":title[:80],"url":news_url,"type":"article",
                                    "date":TODAY,"time":datetime.now().strftime("%H:%M"),
                                    "source":source_label,"tags":["新闻","央视"],"voteup_count":0})
            new_items += 1; page_added += 1
        
        log(f"  第{pi+1}页: +{page_added}条 (累计{new_items})")
        if page_added == 0: break
    
    save_index(existing_skills)
    log(f"=== [{name}] 完成: +{new_items}条 ===\n")
    return new_items


# ==================== 华尔街快讯 ====================
def scrape_ws_flashes_via_api(target=200):
    """尝试通过API获取华尔街快讯"""
    existing_urls, existing_skills = load_index()
    ALL_URLS.update(existing_urls)
    new_count = 0
    log("[华尔街快讯] 尝试API获取...")
    
    # 尝试多个可能的API端点
    api_urls = [
        "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=100",
        "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=200",
    ]
    
    for api_url in api_urls:
        if new_count >= target: break
        try:
            rl.wait()
            r = requests.get(api_url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://wallstreetcn.com/"}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                items = data.get("data", {}).get("items", data.get("data", []))
                if isinstance(data, dict):
                    for k in ["items", "list", "lives", "data"]:
                        if k in data and isinstance(data[k], list): items = data[k]; break
                
                if isinstance(items, list):
                    for it in items:
                        if new_count >= target: break
                        content = it.get("content_text", it.get("content", it.get("title", "")))
                        title = it.get("title", content[:60] if content else "")
                        url = it.get("uri", it.get("url", it.get("id", "")))
                        if not url: url = f"https://wallstreetcn.com/live/global/{hashlib.md5(str(content).encode()).hexdigest()[:8]}"
                        if url in ALL_URLS: continue
                        if not content or len(str(content)) < 10: continue
                        
                        ch = hashlib.md5(str(content)[:150].encode()).hexdigest()[:10]
                        uid = hashlib.md5(str(content).encode()).hexdigest()[:10]
                        item = {"id":uid, "title":str(title)[:80], "content":str(content)[:3000],
                                "url":str(url), "type":"flash", "date":TODAY,
                                "time":it.get("display_time",""), "source":"华尔街快讯",
                                "tags":["快讯","全球快讯"], "voteup_count":0}
                        save_md(item); ALL_URLS.add(str(url))
                        existing_skills.append({"id":uid,"title":str(title)[:80],"url":str(url),
                                                "type":"flash","date":TODAY,"time":it.get("display_time",""),
                                                "source":"华尔街快讯","tags":["快讯","全球快讯"],"voteup_count":0})
                        new_count += 1
                    log(f"  API返回 +{new_count}条快讯")
        except Exception as e:
            log(f"  API尝试失败: {e}"); continue
    
    if new_count > 0:
        save_index(existing_skills)
    log(f"=== [华尔街快讯] 完成: +{new_count}条 ===\n")
    return new_count


# ==================== 主函数 ====================
ALL_URLS = set()

def main():
    print("=" * 60)
    print(f"  CCTV快速抓取器 | {TODAY}")
    print("  源3: CCTV国际200 | 源4: CCTV经济200")
    print("  华尔街快讯: API直连")
    print(f"  QPS<5 | 追加到现有索引 | {DATA_DIR}")
    print("=" * 60)
    
    _, existing = load_index()
    print(f"\n  已有: {len(existing)}条\n")
    
    t0 = time.time()
    r1 = scrape_cctv_source("CCTV国际新闻", CCTV_WORLD_URLS, "央视国际新闻", 200)
    time.sleep(random.uniform(1, 2))
    r2 = scrape_cctv_source("CCTV经济新闻", CCTV_ECON_URLS, "央视经济新闻", 200)
    time.sleep(random.uniform(1, 2))
    r3 = scrape_ws_flashes_via_api(200)
    
    _, final_skills = load_index()
    elapsed = time.time() - t0
    
    print("=" * 60)
    print(f"  📊 补全完成汇总")
    print(f"  {'─'*40}")
    print(f"  CCTV国际: +{r1} | CCTV经济: +{r2} | 华尔街快讯: +{r3}")
    print(f"  {'─'*40}")
    print(f"  新增: {r1+r2+r3}条 | 索引总计: {len(final_skills)}条")
    print(f"  耗时: {elapsed:.0f}s ({elapsed/60:.1f}分钟)")
    print(f"  存储: {DATA_DIR}")
    print("=" * 60)

if __name__ == "__main__":
    main()
