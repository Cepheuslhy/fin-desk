#!/usr/bin/env python3
"""
华尔街见闻 资讯+快讯 综合抓取 — 各200条最新咨讯
QPS<5, 间隔1-5s | 断点续抓 | 自动去重
- 资讯: https://wallstreetcn.com/news/global (200条)
- 快讯: https://wallstreetcn.com/live/global (200条)
存储为skill格式, 供景气周期轮动策略调用
"""
import os, sys, io, re, json, time, random, hashlib
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",errors="replace")

BASE = Path(__file__).parent
TODAY = datetime.now().strftime("%Y%m%d")
DATA_DIR = BASE / TODAY
DATA_DIR.mkdir(parents=True, exist_ok=True)

class RateLimiter:
    def __init__(self, min_s=1.0, max_s=5.0):
        self.min_s = min_s; self.max_s = max_s; self.last = 0
    def wait(self):
        e = time.time()-self.last
        d = random.uniform(self.min_s, self.max_s) if e<self.min_s else random.uniform(max(0.3,self.min_s-e*0.3), max(0.5,self.max_s-e*0.1)) if e<self.max_s else random.uniform(0.3,1.5)
        if d>0: time.sleep(d)
        self.last = time.time()

rl = RateLimiter()
ALL_URLS = set()  # 全局去重

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def safe_name(t):
    return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]+','_',str(t))[:60].strip('_')

def load_index():
    f = DATA_DIR/"skill_index.json"
    if f.exists():
        try:
            d = json.load(open(f, encoding="utf-8"))
            return {s["url"] for s in d.get("skills",[])}, d.get("total",0), d.get("skills",[])
        except: pass
    return set(), 0, []

def save_skill(item, idx_items):
    """保存单条skill"""
    title = safe_name(item["title"])
    uid = item["id"]
    fname = f"{item['date']}_{item['type']}_{uid}_{title}.md"
    with open(DATA_DIR/fname, "w", encoding="utf-8") as f:
        f.write(f"""# {item['title']}

> 来源: {item.get('source','华尔街见闻')}
> 日期: {item['date']}
> 时间: {item.get('time','')}
> 类型: {item['type']}
> 原文: {item['url']}

## 正文

{item.get('content','')}

## 标签

{', '.join(item.get('tags',[]))}

---
*自动抓取 | 景气周期轮动策略数据源*
""")
    idx_items.append({
        "id": item["id"], "title": item["title"],
        "url": item["url"], "type": item["type"],
        "date": item["date"], "time": item.get("time",""),
        "source": item.get("source",""), "tags": item.get("tags",[]),
        "voteup_count": 0
    })

def save_index(all_idx):
    with open(DATA_DIR/"skill_index.json","w",encoding="utf-8") as f:
        json.dump({"total":len(all_idx),"updated":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "source":"wallstreetcn.com","skills":all_idx},f,ensure_ascii=False,indent=2)

def extract_article(page, ctx, url, timeout=20000):
    """提取文章/快讯正文"""
    rl.wait()
    pg = None
    try:
        pg = ctx.new_page()
        pg.goto(url, timeout=timeout, wait_until="domcontentloaded")
        time.sleep(0.8)
        # 多选择器尝试
        content = pg.evaluate("""
            () => {
                const sels = ['.rich-text','.article-content','.article-body',
                    '[class*="article-content"]','[class*="RichText"]',
                    '.live-content','.flashes-item-content','.flash-content',
                    'article','.page-article-content','.content','main'];
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim().length > 20) return el.innerText;
                }
                const b = document.body;
                return b ? b.innerText.substring(0, 5000) : '';
            }
        """)
        return (content or "").strip()
    except:
        return ""
    finally:
        if pg:
            try: pg.close()
            except: pass

def scroll_and_click(page, rounds=8):
    """滚动页面+点击加载更多"""
    for _ in range(rounds):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)
            btn = page.query_selector(
                "button:has-text('加载更多'), .load-more, [class*='LoadMore'], "
                "[class*='load-more'], .article-list-more, .live-load-more"
            )
            if btn:
                try: btn.click(); time.sleep(0.8)
                except: pass
        except: pass

def scrape_articles(page, ctx, max_items=200):
    """抓取资讯 articles"""
    existing, _, _ = load_index()
    ALL_URLS.update(existing)
    items = []
    idx_new = []

    url = "https://wallstreetcn.com/news/global"
    log(f"[资讯] 导航: {url}")
    try: page.goto(url, timeout=30000, wait_until="networkidle")
    except: time.sleep(3); page.goto(url, timeout=60000, wait_until="load")
    time.sleep(3)
    log("预加载...")
    scroll_and_click(page, 10)
    log("预加载完成")

    stall = 0; start = time.time()
    log(f"=== [资讯] 已有:{len(existing)} | 目标:{max_items} ===")

    while len(items) < max_items and stall < 15:
        rl.wait()
        for _ in range(4): scroll_and_click(page, 1)

        try:
            raw = page.evaluate("""
                () => {
                    const links = document.querySelectorAll('a[href*="/articles/"]');
                    const seen = new Set();
                    return Array.from(links).map(a => {
                        const url = a.href;
                        if (seen.has(url)) return null;
                        seen.add(url);
                        const t = a.querySelector('[class*="title"], h2, h3') || a;
                        return {url, title: t.innerText.trim().split('\\n')[0]};
                    }).filter(x => x && x.title.length > 5);
                }
            """)
        except:
            stall += 1; continue

        new = 0
        for it in raw:
            url = it["url"]
            if url in ALL_URLS or len(items) >= max_items:
                continue

            content = extract_article(page, ctx, url)
            if len(content) < 30:
                continue

            uid = hashlib.md5(url.encode()).hexdigest()[:10]
            item = {
                "id": uid, "title": it["title"], "content": content,
                "url": url, "type": "article", "date": TODAY,
                "time": datetime.now().strftime("%H:%M"),
                "source": "华尔街见闻", "tags": []
            }
            save_skill(item, idx_new)
            items.append(item)
            ALL_URLS.add(url)
            new += 1

            log(f"  [资讯 +{len(items)}/{max_items}] {it['title'][:45]}")

            if len(items) % 10 == 0:
                _, _, old_idx = load_index()
                save_index(old_idx + idx_new)
                log(f"  [断点] {len(items)}条 {time.time()-start:.0f}s")

        stall = 0 if new else stall+1
        if stall: log(f"  [无新] {stall}/15")

    if items:
        _, _, old_idx = load_index()
        save_index(old_idx + idx_new)
    log(f"=== [资讯] 完成: +{len(items)} ===\n")
    return items

def scrape_live(page, ctx, max_items=200):
    """抓取快讯 live/flashes"""
    _, _, old_idx = load_index()
    ALL_URLS.update({s["url"] for s in old_idx})
    items = []
    idx_new = []

    url = "https://wallstreetcn.com/live/global"
    log(f"[快讯] 导航: {url}")
    try: page.goto(url, timeout=30000, wait_until="networkidle")
    except: time.sleep(3); page.goto(url, timeout=60000, wait_until="load")
    time.sleep(3)
    log("预加载快讯...")
    scroll_and_click(page, 12)
    log("预加载完成")

    stall = 0; start = time.time()
    before_count = len(old_idx)
    log(f"=== [快讯] 已有总计:{before_count} | 目标:{max_items} ===")

    while len(items) < max_items and stall < 12:
        rl.wait()
        for _ in range(3):
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.8)
            except: pass
        try:
            btn = page.query_selector(
                "button:has-text('加载更多'), .live-load-more, [class*='load-more']"
            )
            if btn:
                btn.click(); time.sleep(1)
        except: pass

        try:
            raw = page.evaluate("""
                () => {
                    const items = document.querySelectorAll(
                        '.live-item, .flashes-item, [class*="flash"] a[href], ' +
                        '[class*="live"] a[href*="/article/"], a[href*="/live/"]'
                    );
                    const seen = new Set();
                    return Array.from(items).map(el => {
                        const a = el.href ? el : el.querySelector('a[href*="/article/"], a[href*="/live/"]');
                        if (!a || !a.href) return null;
                        const url = a.href;
                        if (seen.has(url)) return null;
                        seen.add(url);
                        const t = el.querySelector('[class*="content"], [class*="title"], p') || el;
                        const title = t.innerText.trim().substring(0, 80);
                        return title.length > 5 ? {url, title} : null;
                    }).filter(x => x);
                }
            """)
            if not raw or len(raw) < 3:
                raw = page.evaluate("""
                    () => {
                        const links = document.querySelectorAll('a[href*="/article/"]');
                        const seen = new Set();
                        return Array.from(links).map(a => {
                            if (seen.has(a.href)) return null;
                            seen.add(a.href);
                            return {url: a.href, title: a.innerText.trim().substring(0, 80)};
                        }).filter(x => x && x.title.length > 5);
                    }
                """)
        except:
            stall += 1; continue

        new = 0
        for it in (raw or []):
            it_url = it["url"]
            if it_url in ALL_URLS or len(items) >= max_items:
                continue

            content = extract_article(page, ctx, it_url, timeout=15000)
            if len(content) < 15:
                continue

            uid = hashlib.md5(it_url.encode()).hexdigest()[:10]
            item = {
                "id": uid, "title": it["title"], "content": content,
                "url": it_url, "type": "flash", "date": TODAY,
                "time": datetime.now().strftime("%H:%M"),
                "source": "华尔街见闻快讯", "tags": ["快讯"]
            }
            save_skill(item, idx_new)
            items.append(item)
            ALL_URLS.add(it_url)
            new += 1

            total = len(old_idx) + len(items)
            log(f"  [快讯 +{len(items)}/总{total}] {it['title'][:50]}")

            if len(items) % 10 == 0:
                _, _, oi = load_index()
                save_index(oi + idx_new)
                log(f"  [断点] 快讯{len(items)}条 {time.time()-start:.0f}s")

        stall = 0 if new else stall+1

    if items:
        _, _, oi = load_index()
        save_index(oi + idx_new)
    log(f"=== [快讯] 完成: +{len(items)} ===\n")
    return items

def main():
    print("="*60)
    print("  华尔街见闻 资讯+快讯 综合抓取")
    print("  资讯200条 | 快讯200条 | QPS<5 | 去重")
    print(f"  存储: {DATA_DIR}")
    print("="*60)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36")
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        # 抓取资讯
        articles = scrape_articles(page, ctx, 200)

        # 抓取快讯
        flashes = scrape_live(page, ctx, 200)

        browser.close()

    total = len(articles) + len(flashes)
    _, _, final_idx = load_index()
    log(f"=== 全部完成! 资讯{len(articles)} + 快讯{len(flashes)} = {total}条 | "
        f"索引总计{len(final_idx)}条 | {DATA_DIR} ===")

if __name__ == "__main__":
    main()
