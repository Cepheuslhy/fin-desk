#!/usr/bin/env python3
"""
统一新闻抓取调度器 — 4源全量抓取 → 生成Skill
================================================================
源1: https://wallstreetcn.com/news/global   → 100条资讯 (scrape_daily.py)
源2: https://wallstreetcn.com/live/global   → 200条快讯 (scrape_daily.py, 列表直接获取)
源3: https://news.cctv.com/world/           → 100条国际新闻 (scrape_cctv.py)
源4: https://jingji.cctv.com/              → 100条经济新闻 (scrape_cctv.py)

QPS<5 | 间隔1-5s | 内容不重复 | 统一 skill_index.json
输出: 新闻时事理念/20260701/
================================================================
"""
import os, sys, io, re, json, time, random, hashlib
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).parent
TODAY = datetime.now().strftime("%Y%m%d")
DATA_DIR = BASE / TODAY
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ==================== 全局状态 ====================
ALL_URLS = set()
ALL_CONTENT_HASHES = set()

# ==================== RateLimiter ====================
class RateLimiter:
    def __init__(self, lo=1.0, hi=5.0):
        self.lo = lo; self.hi = hi; self.last = 0
    def wait(self):
        e = time.time() - self.last
        if e < self.lo: time.sleep(random.uniform(self.lo, self.hi))
        elif e < self.hi:
            d = random.uniform(max(0.3, self.lo - e * 0.3), max(0.5, self.hi - e * 0.1))
            if d > 0: time.sleep(d)
        else: time.sleep(random.uniform(0.3, 1.5))
        self.last = time.time()

rl = RateLimiter(lo=1.0, hi=5.0)

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def safe_name(t):
    return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]+', '_', str(t))[:55].strip('_')

# ==================== 统一索引管理 ====================
def load_index():
    f = DATA_DIR / "skill_index.json"
    if f.exists():
        try:
            d = json.load(open(f, encoding="utf-8"))
            return {s["url"] for s in d.get("skills", [])}, d.get("skills", [])
        except: pass
    return set(), []

def save_index(skills):
    seen = set()
    unique = []
    for s in skills:
        sid = s.get("id", s.get("url", ""))
        if sid not in seen:
            seen.add(sid)
            unique.append(s)
    with open(DATA_DIR / "skill_index.json", "w", encoding="utf-8") as f:
        json.dump({
            "total": len(unique),
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sources": ["wallstreetcn.com", "cctv.com"],
            "date": TODAY,
            "skills": unique
        }, f, ensure_ascii=False, indent=2)

def save_md(item):
    title_safe = safe_name(item["title"])
    uid = item["id"]
    fname = f"{item['date']}_{item['type']}_{uid}_{title_safe}.md"
    tags_str = ", ".join(item.get("tags", []))
    with open(DATA_DIR / fname, "w", encoding="utf-8") as f:
        f.write(f"""# {item['title']}
> 来源: {item.get('source','')} | 日期: {item['date']} | 类型: {item['type']}
> 原文: {item['url']} | 标签: {tags_str}
## 正文
{item.get('content','')}
---
""")

def skill_entry(item):
    return {"id": item["id"], "title": item["title"], "url": item["url"],
            "type": item["type"], "date": item["date"], "time": item.get("time",""),
            "source": item.get("source",""), "tags": item.get("tags",[]),
            "voteup_count": 0}

def sync_index(new_skills):
    urls, old = load_index()
    merged = list(old)
    seen = {s["url"] for s in merged}
    for s in new_skills:
        if s["url"] not in seen:
            merged.append(s)
            seen.add(s["url"])
    save_index(merged)

def scroll_load(page, rounds=5):
    for _ in range(rounds):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.8)
            btn = page.query_selector("button:has-text('加载更多'), [class*='load-more'], [class*='LoadMore']")
            if btn: 
                try: btn.click(); time.sleep(0.5)
                except: pass
        except: pass

# ==================== 源1: 华尔街见闻资讯 100条 ====================
def scrape_ws_articles(page, ctx, n=100):
    existing_urls, _ = load_index()
    ALL_URLS.update(existing_urls)
    items = []; new_skills = []
    
    log(f"[华尔街资讯] 导航: https://wallstreetcn.com/news/global")
    try: page.goto("https://wallstreetcn.com/news/global", timeout=30000, wait_until="networkidle")
    except: time.sleep(3); page.goto("https://wallstreetcn.com/news/global", timeout=60000, wait_until="load")
    time.sleep(2); scroll_load(page, 8)
    
    stall = 0; start = time.time()
    while len(items) < n and stall < 10:
        rl.wait(); scroll_load(page, 3)
        try:
            raw = page.evaluate("""() => {
                const links=document.querySelectorAll('a[href*="/articles/"]');
                const seen=new Set();
                return Array.from(links).map(a=>{if(seen.has(a.href))return null;seen.add(a.href);
                const t=a.querySelector('[class*="title"],h2,h3')||a;
                return {url:a.href,title:t.innerText.trim().split('\\n')[0]};})
                .filter(x=>x&&x.title.length>8&&!x.url.includes('/live/'));}""")
        except: stall+=1; continue
        
        new=0
        for it in (raw or []):
            url=it["url"]
            if url in ALL_URLS or len(items)>=n: continue
            rl.wait(); content=""; pg=None
            try:
                pg=ctx.new_page(); pg.goto(url,timeout=15000,wait_until="domcontentloaded");time.sleep(0.5)
                content=pg.evaluate("""()=>{const sel='.rich-text,.article-content,article,main,[class*="article-content"]';
                    const el=document.querySelector(sel);if(el&&el.innerText.trim().length>30)return el.innerText;
                    return document.body?document.body.innerText.substring(0,3000):'';}""")
            except:pass
            finally:
                if pg:
                    try:pg.close()
                    except:pass
            if not content or len(content.strip())<30: continue
            uid=hashlib.md5(url.encode()).hexdigest()[:10]
            item={"id":uid,"title":it["title"][:80],"content":content.strip()[:5000],"url":url,
                  "type":"article","date":TODAY,"time":datetime.now().strftime("%H:%M"),
                  "source":"华尔街见闻","tags":["全球资讯"]}
            save_md(item); items.append(item); ALL_URLS.add(url); new+=1
            log(f"  [资讯 +{len(items)}/{n}] {it['title'][:50]}")
            new_skills.append(skill_entry(item))
            if len(items)%20==0:
                sync_index(new_skills)
                log(f"  [断点]{len(items)}条 {time.time()-start:.0f}s")
        stall=0 if new else stall+1
    
    sync_index(new_skills)
    log(f"=== [华尔街资讯] 完成: {len(items)}条 ===\n")
    return len(items)

# ==================== 源2: 华尔街见闻快讯 200条(列表直接获取) ====================
def scrape_ws_flashes(page, n=200):
    existing_urls, _ = load_index()
    ALL_URLS.update(existing_urls)
    new_skills = []
    
    log(f"[华尔街快讯] 导航: https://wallstreetcn.com/live/global")
    try: page.goto("https://wallstreetcn.com/live/global", timeout=30000, wait_until="networkidle")
    except: time.sleep(3); page.goto("https://wallstreetcn.com/live/global", timeout=60000, wait_until="load")
    time.sleep(2); scroll_load(page, 15)
    
    stall = 0; start = time.time(); scraped = 0
    while scraped < n and stall < 15:
        rl.wait(); scroll_load(page, 5)
        try:
            raw = page.evaluate("""()=>{
                const items=document.querySelectorAll('[class*="live"] [class*="item"],.live-item,[class*="flash"],.item,.live-item-content,[class*="content"]');
                const results=[];
                items.forEach(el=>{
                    const text=el.innerText.trim();
                    if(text.length>10&&text.length<3000){
                        const lines=text.split('\\n');
                        results.push({title:lines[0].substring(0,80),content:text,time:lines.length>1?lines[1]:''});
                    }
                });
                return results;
            }""")
            if not raw or len(raw)<5:
                raw = page.evaluate("""()=>{
                    const items=document.querySelectorAll('.live-item-content,[class*="content"]');
                    return Array.from(items).map(el=>({title:el.innerText.trim().split('\\n')[0].substring(0,80),content:el.innerText.trim()}))
                        .filter(x=>x.content.length>10&&x.content.length<3000);
                }""")
        except: stall+=1; continue
        
        new=0
        for it in (raw or []):
            content=(it.get("content","") or "").strip()
            if len(content)<10 or scraped>=n: continue
            ch=hashlib.md5(content[:150].encode()).hexdigest()[:10]
            fake_url=f"https://wallstreetcn.com/live/flash_{ch}"
            if fake_url in ALL_URLS or ch in ALL_CONTENT_HASHES: continue
            
            uid=hashlib.md5(content.encode()).hexdigest()[:10]
            title=it.get("title","").strip() or content[:60]
            item={"id":uid,"title":title[:80],"content":content[:3000],"url":fake_url,
                  "type":"flash","date":TODAY,"time":it.get("time",datetime.now().strftime("%H:%M")),
                  "source":"华尔街见闻快讯","tags":["快讯","全球快讯"]}
            save_md(item); ALL_URLS.add(fake_url); ALL_CONTENT_HASHES.add(ch); scraped+=1; new+=1
            log(f"  [快讯 +{scraped}/{n}] {title[:50]}")
            new_skills.append(skill_entry(item))
            if scraped%30==0:
                sync_index(new_skills)
                log(f"  [断点]{scraped}条 {time.time()-start:.0f}s")
        stall=0 if new else stall+1
    
    sync_index(new_skills)
    log(f"=== [华尔街快讯] 完成: {scraped}条 ===\n")
    return scraped

# ==================== 源3&4: CCTV新闻 (JSONP API) ====================
try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

CCTV_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

CCTV_WORLD = [f"https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/world_{p}.jsonp" for p in range(1, 8)]
CCTV_ECONOMY = [f"https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/economy_{p}.jsonp" for p in range(1, 8)]

def fetch_cctv_jsonp(url):
    try:
        r = requests.get(url, headers=CCTV_HEADERS, timeout=30)
        r.encoding = "utf-8"
        text = r.text
        start = text.index("(") + 1
        end = text.rindex(")")
        return json.loads(text[start:end])
    except:
        return None

def scrape_cctv(name, page_urls, source_label, target=100):
    existing_urls, _ = load_index()
    ALL_URLS.update(existing_urls)
    new_skills = []
    total = 0
    
    log(f"[{name}] 开始抓取, 目标{target}条")
    
    for page_idx, api_url in enumerate(page_urls):
        if total >= target:
            break
        
        rl.wait()
        time.sleep(random.uniform(1, 3))
        data = fetch_cctv_jsonp(api_url)
        if not data:
            log(f"  第{page_idx+1}页: 请求失败")
            continue
        
        news_list = None
        if isinstance(data, dict):
            d = data.get("data", data)
            if isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, list) and v:
                        news_list = v; break
            elif isinstance(d, list):
                news_list = d
        
        if not news_list:
            log(f"  第{page_idx+1}页: 无数据")
            continue
        
        page_count = 0
        for item in news_list:
            if total >= target:
                break
            title = item.get("title", "")
            url = item.get("url", "")
            brief = item.get("brief", "")
            if not title or len(title) < 5:
                continue
            if url and not url.startswith("http"):
                url = "https://news.cctv.com" + url
            if not url or url in ALL_URLS:
                continue
            
            uid = hashlib.md5((url or title).encode()).hexdigest()[:10]
            skill = {"id": uid, "title": title[:80], "content": (brief or title)[:3000],
                     "url": url, "type": "article", "date": TODAY,
                     "time": datetime.now().strftime("%H:%M"),
                     "source": source_label, "tags": ["新闻", "央视"]}
            save_md(skill)
            ALL_URLS.add(url)
            new_skills.append(skill_entry(skill))
            total += 1
            page_count += 1
        
        log(f"  第{page_idx+1}页: +{page_count}条 (累计{total})")
        if page_count == 0:
            break
    
    sync_index(new_skills)
    log(f"=== [{name}] 完成: {total}条 ===\n")
    return total


# ==================== 主函数 ====================
def main():
    print("=" * 65)
    print(f"  统一新闻抓取调度器 | {TODAY}")
    print("  源1: 华尔街资讯200 + 源2: 华尔街快讯200")
    print("  源3: CCTV国际200 + 源4: CCTV经济200")
    print(f"  QPS<5 | 间隔1-5s | 统一去重 | 存储: {DATA_DIR}")
    print("=" * 65)
    
    t0 = time.time()
    results = {}
    
    # —— 阶段1: 华尔街见闻(Playwright浏览器) ——
    log("\n>>> 阶段1: 华尔街见闻抓取 (Playwright)")
    
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36")
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        
        results["ws_articles"] = scrape_ws_articles(page, ctx, 200)
        results["ws_flashes"] = scrape_ws_flashes(page, 200)
        
        browser.close()
    
    # —— 阶段2: CCTV新闻(JSONP API) ——
    log("\n>>> 阶段2: CCTV新闻抓取 (JSONP API)")
    
    results["cctv_world"] = scrape_cctv("CCTV国际新闻", CCTV_WORLD, "央视国际新闻", 200)
    time.sleep(random.uniform(1, 3))
    results["cctv_economy"] = scrape_cctv("CCTV经济新闻", CCTV_ECONOMY, "央视经济新闻", 200)
    
    # —— 汇总 ——
    _, final_skills = load_index()
    elapsed = time.time() - t0
    
    print("\n" + "=" * 65)
    print(f"  📊 抓取完成汇总")
    print(f"  {'─'*50}")
    print(f"  华尔街资讯: {results.get('ws_articles',0)}条")
    print(f"  华尔街快讯: {results.get('ws_flashes',0)}条")
    print(f"  CCTV国际:   {results.get('cctv_world',0)}条")
    print(f"  CCTV经济:   {results.get('cctv_economy',0)}条")
    print(f"  {'─'*50}")
    print(f"  合计: {sum(results.values())}条 | 索引总计: {len(final_skills)}条")
    print(f"  耗时: {elapsed:.0f}s ({elapsed/60:.1f}分钟)")
    print(f"  存储: {DATA_DIR}")
    print("=" * 65)

if __name__ == "__main__":
    main()
