#!/usr/bin/env python3
"""
华尔街见闻 全球新闻抓取 — 200条最新咨讯
QPS<5, 每次请求间隔1-5s | 断点续抓
存储为skill格式, 供景气周期轮动策略调用
"""
import os, sys, io, re, json, time, random, hashlib
from pathlib import Path
from datetime import datetime

# 修复Windows编码
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 存储路径
BASE = Path(__file__).parent
TODAY = datetime.now().strftime("%Y%m%d")
DATA_DIR = BASE / TODAY
DATA_DIR.mkdir(parents=True, exist_ok=True)

# QPS控制器
class RateLimiter:
    def __init__(self, min_s=1.0, max_s=5.0):
        self.min_s = min_s
        self.max_s = max_s
        self.last_call = 0

    def wait(self):
        elapsed = time.time() - self.last_call
        if elapsed < self.min_s:
            delay = random.uniform(self.min_s, self.max_s)
            time.sleep(delay)
        elif elapsed < self.max_s:
            delay = random.uniform(self.min_s - elapsed * 0.3, self.max_s - elapsed * 0.1)
            if delay > 0:
                time.sleep(delay)
        else:
            time.sleep(random.uniform(0.3, 1.5))
        self.last_call = time.time()

rl = RateLimiter(min_s=1.0, max_s=5.0)

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def safe_filename(title):
    """将标题转为安全文件名"""
    # 保留中英文数字，其余替换为下划线
    name = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]+', '_', title)
    return name[:60].strip('_')


def get_existing_urls():
    """读取已有skill的URL去重"""
    index_file = DATA_DIR / "skill_index.json"
    if not index_file.exists():
        return set(), 0
    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        urls = {s["url"] for s in data.get("skills", [])}
        return urls, data.get("total", 0)
    except:
        return set(), 0


def save_md(item):
    """保存单条新闻为.md skill文件"""
    title_clean = safe_filename(item["title"])
    date_str = item["date"]
    uid = hashlib.md5(item["url"].encode()).hexdigest()[:8]
    fname = f"{date_str}_news_{uid}_{title_clean}.md"
    fpath = DATA_DIR / fname

    content = f"""# {item['title']}

> 来源: {item.get('source', '华尔街见闻')}
> 日期: {item['date']}
> 时间: {item.get('time', '')}
> 原文链接: {item['url']}

## 正文

{item.get('content', '')}

---
*自动抓取 | 景气周期轮动策略新闻源*
"""
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(content)


def update_index(items):
    """更新skill_index.json"""
    index_file = DATA_DIR / "skill_index.json"
    existing_urls, existing_count = get_existing_urls()

    old_items = []
    if index_file.exists():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                old_items = json.load(f).get("skills", [])
        except:
            pass

    new_items = [it for it in items if it["url"] not in existing_urls]
    all_items = old_items + new_items

    index = {
        "total": len(all_items),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "https://wallstreetcn.com/news/global",
        "skills": all_items
    }
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def scrape_news_list(page, max_items=200):
    """
    抓取华尔街见闻新闻列表
    策略: 滚动加载 + 点击"加载更多"
    """
    existing_urls, existing_count = get_existing_urls()
    all_seen = set(existing_urls)
    items = []

    target_url = "https://wallstreetcn.com/news/global"
    log(f"导航: {target_url}")
    try:
        page.goto(target_url, timeout=30000, wait_until="networkidle")
    except:
        time.sleep(3)
        page.goto(target_url, timeout=60000, wait_until="load")
    time.sleep(3)

    # 预滚动加载
    log("预加载新闻列表...")
    for i in range(10):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)
            # 尝试点击加载更多
            load_btn = page.query_selector(
                "button:has-text('加载更多'), .load-more, [class*='load-more'], "
                "[class*='LoadMore'], .article-list-more"
            )
            if load_btn:
                try:
                    load_btn.click()
                    time.sleep(1)
                except:
                    pass
        except:
            pass
    log("预加载完成")

    stall = 0
    max_stall = 15
    start_time = time.time()
    log(f"=== 开始抓取新闻 | 已有:{existing_count} | 目标:{max_items} | QPS<5 ===")

    while len(items) + existing_count < max_items and stall < max_stall:
        rl.wait()

        # 每轮滚动3次+尝试点击加载更多
        for _ in range(4):
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.5)
            except:
                pass
        # 点击加载更多
        try:
            load_btn = page.query_selector(
                "button:has-text('加载更多'), .load-more, [class*='LoadMore']"
            )
            if load_btn:
                load_btn.click()
                time.sleep(1)
        except:
            pass

        # 提取新闻列表
        try:
            raw = page.evaluate("""
                () => {
                    const articles = document.querySelectorAll(
                        'a[href*="/articles/"], a[href*="/news/"], ' +
                        '.article-item, .news-item, [class*="article"]'
                    );
                    const seen = new Set();
                    const results = [];
                    articles.forEach(el => {
                        // 找链接
                        let link = el.href || el.querySelector('a')?.href || '';
                        if (!link) {
                            const a = el.closest('a');
                            if (a) link = a.href;
                        }
                        if (!link || seen.has(link)) return;
                        seen.add(link);

                        // 找标题
                        let title = '';
                        const t = el.querySelector('[class*="title"], h2, h3, h4');
                        if (t) title = t.innerText.trim();
                        else title = el.innerText.split('\\n')[0].trim();
                        if (!title && el.getAttribute('title')) {
                            title = el.getAttribute('title');
                        }

                        // 找时间
                        let time = '';
                        const tm = el.querySelector('[class*="time"], time, [datetime], .date');
                        if (tm) time = tm.innerText || tm.getAttribute('datetime') || '';

                        if (title && link && link.includes('wallstreetcn')) {
                            results.push({url: link, title: title, time: time});
                        }
                    });
                    return results;
                }
            """)

            if not raw or len(raw) == 0:
                # 备选: 直接抓取所有链接
                raw = page.evaluate("""
                    () => {
                        const links = document.querySelectorAll('a[href*="/articles/"]');
                        return Array.from(links).map(a => ({
                            url: a.href,
                            title: a.innerText.trim() || a.getAttribute('title') || '',
                            time: ''
                        }));
                    }
                """)

        except Exception as e:
            log(f"提取异常: {e}")
            stall += 1
            continue

        new = 0
        for it in raw:
            url = it.get("url", "")
            title = (it.get("title", "") or "").strip()

            if not url or not title:
                continue
            if url in all_seen:
                continue
            if len(items) + existing_count >= max_items:
                break

            rl.wait()

            # 提取正文
            content = ""
            pg = None
            try:
                pg = page.context.new_page()
                pg.goto(url, timeout=20000, wait_until="domcontentloaded")
                time.sleep(1.5)

                content = pg.evaluate("""
                    () => {
                        const sel = '.rich-text, .article-content, .article-body, ' +
                                   '[class*="article-content"], [class*="RichText"], ' +
                                   '.page-article-content, article, .content';
                        const el = document.querySelector(sel);
                        if (el && el.innerText.trim().length > 50) return el.innerText;

                        // 备选
                        const main = document.querySelector('main, .article, .page-article');
                        if (main && main.innerText.trim().length > 50) return main.innerText;

                        return '';
                    }
                """)

                if not content.strip() or len(content.strip()) < 30:
                    # 最后一搏: get body text limited
                    content = pg.evaluate("""
                        () => {
                            const body = document.body;
                            if (!body) return '';
                            const text = body.innerText.substring(0, 5000);
                            return text;
                        }
                    """)

            except Exception as ex:
                pass
            finally:
                if pg:
                    try:
                        pg.close()
                    except:
                        pass

            if not content.strip() or len(content.strip()) < 30:
                continue

            item = {
                "id": hashlib.md5(url.encode()).hexdigest()[:10],
                "title": title,
                "content": content.strip(),
                "url": url,
                "type": "news",
                "date": TODAY,
                "time": it.get("time", ""),
                "source": "华尔街见闻",
                "voteup_count": 0
            }

            # 提取内容摘要(前200字)
            summary = content.strip()[:200].replace('\n', ' ')
            item["summary"] = summary

            save_md(item)
            items.append(item)
            all_seen.add(url)
            new += 1

            total = len(items) + existing_count
            elapsed = time.time() - start_time
            t_short = title[:45]
            log(f"  [+{total}/{max_items}] {t_short}")
            log(f"    摘要: {summary[:80]}...")

            # 每10条存索引
            if total % 10 == 0:
                update_index(items[-10:])
                log(f"  [断点] 已存{total}条 | 耗时{elapsed:.0f}s | 速率{total*3600/elapsed:.0f}条/h")

        stall = 0 if new > 0 else stall + 1
        if stall > 0:
            log(f"  [无新内容] stall={stall}/{max_stall}")

    if items:
        update_index(items)
        elapsed = time.time() - start_time
        log(f"=== 本轮完成: +{len(items)}条 | 耗时{elapsed:.0f}s | "
            f"速率{len(items)*3600/elapsed:.0f}条/h ===")
    return items


def main():
    print("=" * 60)
    print("  华尔街见闻 全球新闻抓取 [200条]")
    print("  QPS<5 | 间隔1-5s | 断点续抓")
    print(f"  存储: {DATA_DIR}")
    print("=" * 60)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                       " Chrome/120.0 Safari/537.36"
        )
        page = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # 华尔街见闻不需要登录
        log("访问华尔街见闻全球频道...")

        news = scrape_news_list(page, max_items=200)

        # 保存原始数据
        raw_file = DATA_DIR / "news_raw.json"
        old_data = []
        if raw_file.exists():
            try:
                old_data = json.load(open(raw_file, "r", encoding="utf-8"))
            except:
                pass
        json.dump(old_data + news, open(raw_file, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

        browser.close()

    log(f"=== 完成! 新增{len(news)}条 | 存储目录: {DATA_DIR} ===")


if __name__ == "__main__":
    main()
