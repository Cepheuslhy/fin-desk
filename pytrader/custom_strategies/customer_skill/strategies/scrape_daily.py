#!/usr/bin/env python3
"""
华尔街见闻 综合抓取 — 资讯100条 + 快讯200条(列表直接获取)
QPS<5, 间隔1-5s | 去重 | 快讯不点击详情
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

rl = RateLimiter()
ALL_URLS = set()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def safe_name(t):
    return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]+', '_', str(t))[:60].strip('_')

def load_index():
    f = DATA_DIR / "skill_index.json"
    if f.exists():
        try:
            d = json.load(open(f, encoding="utf-8"))
            return {s["url"] for s in d.get("skills", [])}, d.get("skills", [])
        except: pass
    return set(), []

def save_index(skills):
    with open(DATA_DIR / "skill_index.json", "w", encoding="utf-8") as f:
        json.dump({"total": len(skills), "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "source": "wallstreetcn.com", "skills": skills}, f, ensure_ascii=False, indent=2)

def save_skill(item):
    title = safe_name(item["title"])
    fname = f"{item['date']}_{item['type']}_{item['id']}_{title}.md"
    tags = ", ".join(item.get("tags", []))
    with open(DATA_DIR / fname, "w", encoding="utf-8") as f:
        f.write(f"""# {item['title']}
> 来源: {item.get('source','')} | 日期: {item['date']} | 类型: {item['type']}
> 原文: {item['url']} | 标签: {tags}
## 正文
{item.get('content','')}
---""")

def scroll_load(page, rounds=5):
    for _ in range(rounds):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
            btn = page.query_selector("button:has-text('加载更多'), [class*='load-more'], [class*='LoadMore']")
            if btn:
                try: btn.click(); time.sleep(0.5)
                except: pass
        except: pass

# ====== 资讯抓取 100条 ======
def scrape_articles(page, ctx, n=100):
    existing, _ = load_index()
    ALL_URLS.update(existing)
    items = []; new_skills = []
    log(f"[资讯] 导航 + 预加载...")
    try: page.goto("https://wallstreetcn.com/news/global", timeout=30000, wait_until="networkidle")
    except: time.sleep(3); page.goto("https://wallstreetcn.com/news/global", timeout=60000, wait_until="load")
    time.sleep(2); scroll_load(page, 8)
    
    stall = 0; start = time.time()
    while len(items) < n and stall < 10:
        rl.wait(); scroll_load(page, 3)
        try:
            raw = page.evaluate("""
                () => { const links=document.querySelectorAll('a[href*=\"/articles/\"]');
                const seen=new Set();
                return Array.from(links).map(a=>{if(seen.has(a.href))return null;seen.add(a.href);
                const t=a.querySelector('[class*=\"title\"],h2,h3')||a;
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
                content=pg.evaluate("""
                    ()=>{const sel='.rich-text,.article-content,article,main,[class*=\"article-content\"]';
                    const el=document.querySelector(sel);if(el&&el.innerText.trim().length>30)return el.innerText;
                    return document.body?document.body.innerText.substring(0,3000):'';}""")
            except:pass
            finally:
                if pg:
                    try:pg.close()
                    except:pass
            if not content or len(content.strip())<30: continue
            uid=hashlib.md5(url.encode()).hexdigest()[:10]
            item={"id":uid,"title":it["title"],"content":content.strip(),"url":url,
                  "type":"article","date":TODAY,"time":datetime.now().strftime("%H:%M"),
                  "source":"华尔街见闻","tags":[]}
            save_skill(item); items.append(item); ALL_URLS.add(url); new+=1
            log(f"  [资讯 +{len(items)}/{n}] {it['title'][:45]}")
            new_skills.append({"id":uid,"title":it["title"],"url":url,"type":"article",
                              "date":TODAY,"time":item["time"],"source":item["source"],
                              "tags":[],"voteup_count":0})
            if len(items)%20==0:
                _,oi=load_index(); save_index(oi+new_skills)
                log(f"  [断点]{len(items)}条 {time.time()-start:.0f}s")
        stall=0 if new else stall+1
    
    if items:
        _,oi=load_index(); save_index(oi+new_skills)
    log(f"=== [资讯] {len(items)}条 ===\n")
    return new_skills

# ====== 快讯抓取 200条(列表直接获取) ======
def scrape_flashes(page, n=200):
    existing, _ = load_index()
    ALL_URLS.update(existing)
    new_skills = []
    log(f"[快讯] 导航 + 预加载...")
    try: page.goto("https://wallstreetcn.com/live/global", timeout=30000, wait_until="networkidle")
    except: time.sleep(3); page.goto("https://wallstreetcn.com/live/global", timeout=60000, wait_until="load")
    time.sleep(2); scroll_load(page, 15)
    
    stall = 0; start = time.time()
    while len(new_skills) < n and stall < 15:
        rl.wait(); scroll_load(page, 5)
        try:
            raw = page.evaluate("""
                ()=>{
                    const items=document.querySelectorAll('[class*=\"live\"] [class*=\"item\"],.live-item,[class*=\"flash\"],.item');
                    const results=[];
                    items.forEach(el=>{
                        const text=el.innerText.trim();
                        if(text.length>10&&text.length<2000){
                            const lines=text.split('\\n');
                            results.push({title:lines[0].substring(0,80),content:text,
                                         time:lines.length>1?lines[1]:''});
                        }
                    });
                    return results;
                }""")
            if not raw or len(raw)<5:
                raw = page.evaluate("""
                    ()=>{
                        const items=document.querySelectorAll('.live-item-content,[class*=\"content\"]');
                        return Array.from(items).map(el=>({
                            title:el.innerText.trim().split('\\n')[0].substring(0,80),
                            content:el.innerText.trim()}))
                            .filter(x=>x.content.length>10&&x.content.length<2000);
                    }""")
        except: stall+=1; continue
        
        new=0
        for it in (raw or []):
            content=(it.get("content","") or "").strip()
            if len(content)<10 or len(new_skills)>=n: continue
            ch=hashlib.md5(content[:100].encode()).hexdigest()[:8]
            fake_url=f"https://wallstreetcn.com/live/{ch}"
            if fake_url in ALL_URLS: continue
            
            uid=hashlib.md5(content.encode()).hexdigest()[:10]
            item={"id":uid,"title":it.get("title",content[:50]),"content":content,
                  "url":fake_url,"type":"flash","date":TODAY,
                  "time":it.get("time",datetime.now().strftime("%H:%M")),
                  "source":"华尔街见闻快讯","tags":["快讯"]}
            save_skill(item); ALL_URLS.add(fake_url); new+=1
            t=item["title"][:45]
            log(f"  [快讯 +{len(new_skills)}/{n}] {t}")
            new_skills.append({"id":uid,"title":item["title"],"url":fake_url,
                              "type":"flash","date":TODAY,"time":item["time"],
                              "source":item["source"],"tags":["快讯"],"voteup_count":0})
            if len(new_skills)%30==0:
                _,oi=load_index(); save_index(oi+new_skills)
                log(f"  [断点]{len(new_skills)}条 {time.time()-start:.0f}s")
        stall=0 if new else stall+1
    
    if new_skills:
        _,oi=load_index(); save_index(oi+new_skills)
    log(f"=== [快讯] {len(new_skills)}条 ===\n")
    return new_skills

def main():
    print("="*60)
    print(f"  华尔街见闻 资讯100+快讯200 | QPS<5 | {TODAY}")
    print(f"  存储: {DATA_DIR}")
    print("="*60)
    
    from playwright.sync_api import sync_playwright
    _, old_skills = load_index()
    all_skills = list(old_skills)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36")
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        
        a = scrape_articles(page, ctx, 100)
        f = scrape_flashes(page, 200)
        browser.close()
    
    existing_urls = {s["url"] for s in all_skills}
    for s in a + f:
        if s["url"] not in existing_urls:
            all_skills.append(s); existing_urls.add(s["url"])
    save_index(all_skills)
    log(f"=== 全部完成! 资讯{len(a)}+快讯{len(f)}={len(a)+len(f)} | 索引{len(all_skills)} ===")

if __name__ == "__main__":
    main()
