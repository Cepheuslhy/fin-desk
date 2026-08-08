#!/usr/bin/env python3
"""
华尔街见闻快速抓取 — 资讯10条 + 快讯10条(列表直接获取)
QPS<5, 间隔1-5s | 去重 | 轻量快抓
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
        self.lo = lo
        self.hi = hi
        self.last = 0

    def wait(self):
        e = time.time() - self.last
        if e < self.lo:
            time.sleep(random.uniform(self.lo, self.hi))
        elif e < self.hi:
            d = random.uniform(max(0.3, self.lo - e * 0.3), max(0.5, self.hi - e * 0.1))
            if d > 0: time.sleep(d)
        else:
            time.sleep(random.uniform(0.3, 1.5))
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
            urls = {s["url"] for s in d.get("skills", [])}
            return urls, d.get("skills", [])
        except:
            pass
    return set(), []


def save_index(skills):
    with open(DATA_DIR / "skill_index.json", "w", encoding="utf-8") as f:
        json.dump({
            "total": len(skills),
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "wallstreetcn.com",
            "skills": skills
        }, f, ensure_ascii=False, indent=2)


def save_md(item):
    title = safe_name(item["title"])
    fname = f"{item['date']}_{item['type']}_{item['id']}_{title}.md"
    tags_str = ", ".join(item.get("tags", []))
    with open(DATA_DIR / fname, "w", encoding="utf-8") as f:
        f.write(f"""# {item['title']}

> 来源: {item.get('source', '')}
> 日期: {item['date']}
> 时间: {item.get('time', '')}
> 类型: {item['type']}
> 原文: {item['url']}
> 标签: {tags_str}

## 正文

{item.get('content', '')}

---
*快速抓取 | 景气周期轮动策略数据源*
""")


# ====== 资讯抓取 (10条，需点进详情页) ======
def scrape_articles(page, ctx, n=10):
    existing, _ = load_index()
    ALL_URLS.update(existing)
    items = []
    new_skills = []

    log(f"[资讯] 导航...")
    try:
        page.goto("https://wallstreetcn.com/news/global", timeout=30000, wait_until="networkidle")
    except:
        time.sleep(3)
        page.goto("https://wallstreetcn.com/news/global", timeout=60000, wait_until="load")
    time.sleep(2)

    # 滚动加载足够
    for _ in range(4):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
        except:
            pass

    # 提取列表
    rl.wait()
    raw = page.evaluate("""
        () => {
            const links = document.querySelectorAll('a[href*="/articles/"]');
            const seen = new Set();
            return Array.from(links).map(a => {
                if (seen.has(a.href)) return null;
                seen.add(a.href);
                const t = a.querySelector('[class*="title"], h2, h3') || a;
                return {url: a.href, title: t.innerText.trim().split('\\n')[0]};
            }).filter(x => x && x.title.length > 8 && !x.url.includes('/live/'));
        }
    """)

    count = 0
    for it in (raw or []):
        url = it["url"]
        if url in ALL_URLS or count >= n:
            continue

        rl.wait()
        content = ""
        pg = None
        try:
            pg = ctx.new_page()
            pg.goto(url, timeout=15000, wait_until="domcontentloaded")
            time.sleep(0.6)
            content = pg.evaluate("""
                () => {
                    const sel = '.rich-text, .article-content, .article-body, ' +
                               '[class*="article-content"], [class*="RichText"], article, main';
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim().length > 30) return el.innerText;
                    return document.body ? document.body.innerText.substring(0, 3000) : '';
                }
            """)
        except:
            pass
        finally:
            if pg:
                try: pg.close()
                except: pass

        if not content or len(content.strip()) < 30:
            continue

        uid = hashlib.md5(url.encode()).hexdigest()[:10]
        item = {
            "id": uid, "title": it["title"], "content": content.strip(),
            "url": url, "type": "article", "date": TODAY,
            "time": datetime.now().strftime("%H:%M"),
            "source": "华尔街见闻", "tags": []
        }
        save_md(item)
        items.append(item)
        ALL_URLS.add(url)
        count += 1
        log(f"  [资讯 +{count}/{n}] {it['title'][:50]}")
        new_skills.append({
            "id": item["id"], "title": item["title"], "url": item["url"],
            "type": "article", "date": TODAY, "time": item["time"],
            "source": item["source"], "tags": [], "voteup_count": 0
        })

    log(f"=== [资讯] 完成: {len(items)}条 ===")
    return new_skills


# ====== 快讯抓取 (10条，直接从列表获取内容不点击) ======
def scrape_flashes(page, n=10):
    existing, _ = load_index()
    ALL_URLS.update(existing)
    new_skills = []

    log(f"[快讯] 导航...")
    try:
        page.goto("https://wallstreetcn.com/live/global", timeout=30000, wait_until="networkidle")
    except:
        time.sleep(3)
        page.goto("https://wallstreetcn.com/live/global", timeout=60000, wait_until="load")
    time.sleep(2)

    # 滚动加载快讯列表
    for _ in range(6):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.2)
            btn = page.query_selector(
                "button:has-text('加载更多'), .live-load-more, [class*='load-more']"
            )
            if btn:
                try: btn.click(); time.sleep(0.5)
                except: pass
        except:
            pass

    rl.wait()
    # 直接从列表中提取快讯内容（不点击进入详情页）
    raw = page.evaluate("""
        () => {
            const items = document.querySelectorAll(
                '.live-item, .flashes-item, [class*="flash-item"], ' +
                '[class*="LiveCard"], [class*="live-card"], [class*="FlashCard"]'
            );
            const results = [];
            items.forEach(el => {
                const content_el = el.querySelector(
                    '[class*="content"], [class*="Content"], p, .text, ' +
                    '[class*="desc"], [class*="body"], .live-item-content'
                );
                const time_el = el.querySelector(
                    '[class*="time"], time, .date, [class*="Time"]'
                );
                const title_el = el.querySelector(
                    '[class*="title"], h3, h4, strong'
                );
                
                const content = (content_el || el).innerText.trim();
                const time = time_el ? time_el.innerText.trim() : '';
                const title = title_el ? title_el.innerText.trim() : content.split('\\n')[0];
                
                if (content && content.length > 10 && content.length < 2000) {
                    results.push({
                        title: title.substring(0, 80),
                        content: content,
                        time: time
                    });
                }
            });
            return results;
        }
    """)

    # 如果上面方式没获取到，尝试另一种方式
    if not raw or len(raw) < 3:
        raw = page.evaluate("""
            () => {
                const items = document.querySelectorAll('[class*="live"] [class*="item"], ' +
                    '[class*="flash"], .live-item, .item');
                const results = [];
                items.forEach(el => {
                    const text = el.innerText.trim();
                    if (text.length > 10 && text.length < 2000) {
                        const lines = text.split('\\n');
                        results.push({
                            title: lines[0].substring(0, 80),
                            content: text,
                            time: lines.length > 1 ? lines[1] : ''
                        });
                    }
                });
                return results;
            }
        """)

    count = 0
    for it in (raw or []):
        title = it.get("title", "").strip()
        content = it.get("content", "").strip()
        if not content or count >= n:
            continue

        # 用内容hash去重
        content_hash = hashlib.md5(content[:100].encode()).hexdigest()[:8]
        fake_url = f"https://wallstreetcn.com/live/{content_hash}"
        if fake_url in ALL_URLS:
            continue

        uid = hashlib.md5(content.encode()).hexdigest()[:10]
        item = {
            "id": uid, "title": title or content[:50], "content": content,
            "url": fake_url, "type": "flash", "date": TODAY,
            "time": it.get("time", datetime.now().strftime("%H:%M")),
            "source": "华尔街见闻快讯", "tags": ["快讯"]
        }
        save_md(item)
        ALL_URLS.add(fake_url)
        count += 1

        log(f"  [快讯 +{count}/{n}] {title[:50] if title else content[:50]}")
        new_skills.append({
            "id": item["id"], "title": item["title"], "url": item["url"],
            "type": "flash", "date": TODAY, "time": item["time"],
            "source": item["source"], "tags": ["快讯"], "voteup_count": 0
        })

    log(f"=== [快讯] 完成: {len(new_skills)}条 ===")
    return new_skills


def main():
    print("=" * 60)
    print(f"  华尔街见闻 快抓 [资讯10条 + 快讯10条]")
    print(f"  QPS<5 | 去重 | {TODAY}")
    print(f"  存储: {DATA_DIR}")
    print("=" * 60)

    from playwright.sync_api import sync_playwright

    # 加载已有索引
    _, old_skills = load_index()
    all_skills = list(old_skills)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"
        )
        page = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # 抓取资讯
        article_skills = scrape_articles(page, ctx, n=10)

        # 抓取快讯(列表直接获取)
        flash_skills = scrape_flashes(page, n=10)

        browser.close()

    # 合并保存
    existing_urls = {s["url"] for s in all_skills}
    for s in article_skills + flash_skills:
        if s["url"] not in existing_urls:
            all_skills.append(s)
            existing_urls.add(s["url"])

    save_index(all_skills)
    log(f"=== 全部完成! 资讯{len(article_skills)} + 快讯{len(flash_skills)} = "
        f"{len(article_skills)+len(flash_skills)}条 | 索引总计{len(all_skills)}条 ===")


if __name__ == "__main__":
    main()
