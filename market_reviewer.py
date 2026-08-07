#!/usr/bin/env python3
"""market_reviewer.py — 每日收盘市场复盘生成器
纯标准库，零外部依赖，基于东方财富 em HTTP API。
每天 15:31 收盘后自动运行，生成/更新 market-review.json。
"""

import json, os, ssl, sys, time, urllib.request
from datetime import datetime, date, timedelta, timezone

CST = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REVIEW_FILE = os.path.join(BASE_DIR, "market-review.json")

# ── 指数 secid 映射 ──
INDICES = [
    {"secid": "1.000001", "name": "上证指数",   "type": "main"},
    {"secid": "0.399001", "name": "深证成指",   "type": "main"},
    {"secid": "0.399006", "name": "创业板指",   "type": "main"},
    {"secid": "1.000688", "name": "科创50",    "type": "main"},
    {"secid": "1.000300", "name": "沪深300",   "type": "wide"},
    {"secid": "1.000852", "name": "中证1000",  "type": "wide"},
    {"secid": "0.399303", "name": "国证2000",  "type": "wide"},
    {"secid": "1.000016", "name": "上证50",    "type": "wide"},
    {"secid": "1.000015", "name": "红利指数",   "type": "wide"},
    {"secid": "0.899050", "name": "北证50",    "type": "wide"},
    {"secid": "1.000905", "name": "中证500",   "type": "wide"},
    {"secid": "1.000010", "name": "上证180",   "type": "wide"},
]

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# Rate limiter: 最小请求间隔 500ms（QPS ≤ 2，防止东财反爬 RST）
_last_req_time = 0


def http_get(url, params=None, timeout=15, min_interval=0.5):
    """HTTP GET 请求，返回解析后的 JSON"""
    global _last_req_time
    elapsed = time.time() - _last_req_time
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_req_time = time.time()

    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  HTTP GET error: {e}")
        return {}

# ── 1. 指数实时行情 ──
def fetch_index_spot():
    """拉取所有目标指数的最新行情"""
    secids = ",".join(i["secid"] for i in INDICES)
    params = {
        "fltt": "2",
        "fields": "f2,f3,f4,f6,f7,f12,f14,f15,f16,f17,f18",
        "secids": secids,
    }
    data = http_get("https://push2.eastmoney.com/api/qt/ulist.np/get", params)
    items = data.get("data", {}).get("diff", [])
    result = {}
    for item in items:
        result[item.get("f12", "")] = {
            "close": item.get("f2"), "chg_pct": item.get("f3"),
            "high": item.get("f15"), "low": item.get("f16"),
            "open": item.get("f17"), "amount": item.get("f6"),
            "ampl": item.get("f4"), "name": item.get("f14"),
        }
    return result

# ── 2. 指数历史 K 线 (算 MA) ──
def fetch_index_kline(secid, days=70):
    """拉取指数日K线（腾讯 web.ifzq.gtimg.cn），返回收盘价列表 (最新→最旧)"""
    parts = secid.split(".")
    prefix = "sh" if parts[0] == "1" else "sz"
    code = prefix + parts[1]
    data = http_get(f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days+5},qfq")
    klines = []
    if data and data.get("code") == 0:
        stock = data.get("data", {}).get(code, {})
        klines = stock.get("qfqday", stock.get("day", []))
    if not klines:
        return [], 0
    closes = []
    for line in klines:
        if len(line) >= 3:
            try:
                closes.append(float(line[2]))  # close
            except (ValueError, TypeError):
                pass
    return closes, 0

def calc_ma_pills(close, mas):
    """计算 MA 均线偏差，返回 pills 和偏差百分比"""
    pills = []
    devs = {}
    for label, n in mas.items():
        if n <= len(mas) and mas.get(n):
            ma_val = mas[n]
            if ma_val > 0:
                dev_pct = round((close - ma_val) / ma_val * 100, 2)
                devs[f"ma{n}"] = f"{ma_val:.2f}" if ma_val < 1000 else f"{ma_val:.0f}"
                devs[f"ma{n}Dev"] = f"{dev_pct:+.2f}%"
                pills.append({"label": f"MA{n}", "cls": "p-red" if close >= ma_val else "p-grn"})
            else:
                devs[f"ma{n}"] = "-"
                devs[f"ma{n}Dev"] = "-"
                pills.append({"label": "—", "cls": "p-gray"})
        else:
            devs[f"ma{n}"] = "-"
            devs[f"ma{n}Dev"] = "-"
            pills.append({"label": "—", "cls": "p-gray"})
    return pills, devs

def build_index_entry(mapping, spot, kline_closes, total_amt_days):
    """构建单个指数条目"""
    info = spot.get(mapping["secid"].split(".")[1], {})
    close = info.get("close", 0) or 0
    if not close:
        return None
    close = float(close)
    mas = {}
    for n in [5, 10, 20, 30, 60]:
        vals = kline_closes[:n]
        if len(vals) >= n:
            mas[n] = sum(vals) / n
    pills, devs = calc_ma_pills(close, mas)

    chg = info.get("chg_pct", 0) or 0
    chg_pts = round(float(chg) / 100 * close, 2) if close else 0
    ampl = info.get("ampl", 0) or 0
    amount = round((info.get("amount", 0) or 0) / 100000000, 0)  # 元→亿

    entry = {
        "name": mapping["name"],
        "close": f"{close:.2f}",
        "chg": f"{chg:+.2f}%",
        "chgPts": f"{chg_pts:+.2f}",
        "ampl": f"{ampl}%",
        "amount": str(int(amount)),
        **devs,
        "maPills": pills,
    }
    return entry

# ── 3. 涨停板数据 ──
def fetch_limit_up(date_str):
    """拉取涨停板池数据"""
    params = {"Date": date_str, "PageSize": "200", "pageNo": "1"}
    data = http_get("https://push2ex.eastmoney.com/getTopicZTPool", params)
    if not data:
        return []
    items = data.get("data")
    if items is None:
        return []
    if isinstance(items, list):
        return items
    return items.get("data", items.get("Data", []))

def fetch_blasting(date_str):
    """拉取炸板数据"""
    params = {"Date": date_str, "PageSize": "200", "pageNo": "1"}
    data = http_get("https://push2ex.eastmoney.com/getTopicZTPool", {**params, "Pool": "blasting"})
    if not data:
        return 0
    items = data.get("data")
    if items is None:
        return 0
    if isinstance(items, list):
        return len(items)
    return len(items.get("data", items.get("Data", [])))

# ── 4. 板块排名 ──
def fetch_sector_ranking(top=10):
    """拉取概念板块涨幅排行"""
    params = {
        "pn": "1", "pz": str(top), "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f4,f12,f14,f184,f66,f69,f72,f75,f78,f81,f84,f87,f102,f104,f105",
    }
    data = http_get("https://push2.eastmoney.com/api/qt/clist/get", params)
    items = data.get("data", {}).get("diff", [])
    if not items:
        items = []
    return items

# ── 5. 市场概览（涨跌家数、成交额等）──
def fetch_market_overview():
    """拉取沪深市场涨跌家数、成交额等"""
    params = {
        "fltt": "2",
        "fields": "f2,f3,f4,f12,f14,f104,f105,f106,f152",
        "secids": "1.000001,0.399001",
    }
    data = http_get("https://push2.eastmoney.com/api/qt/ulist.np/get", params)
    items = data.get("data", {}).get("diff", [])
    result = {}
    for item in items:
        result[item.get("f14", "")] = item
    return result


def main():
    now = datetime.now(CST)
    today_str = now.strftime("%Y%m%d")
    today_dt = date.today()
    weekday_str = WEEKDAYS[today_dt.weekday()]

    # 跳过周末
    if today_dt.weekday() >= 5:
        print(f"今日 {weekday_str} — 非交易日，跳过")
        return

    print(f"═══ 市场复盘生成器 ═══")
    print(f"日期: {now.strftime('%Y-%m-%d')} | 时间: {now.strftime('%H:%M')}")

    # 1. 指数实时行情
    print("[1] 拉取指数行情…")
    spot = fetch_index_spot()
    print(f"    获取: {len(spot)} 个指数")

    # 2. K 线 + MA 计算
    print("[2] 拉取指数日K线(70天, 计算MA)…")
    indices = []
    vol_data = {"up": 0, "down": 0, "flat": 0, "up5pct": 0, "down5pct": 0}
    total_turnover = 0
    limit_up_count = 0
    limit_down_count = 0

    for idx_map in INDICES:
        secid = idx_map["secid"]
        kline_closes, _ = fetch_index_kline(secid, 70)
        if not kline_closes:
            continue
        entry = build_index_entry(idx_map, spot, kline_closes, 0)
        if entry:
            indices.append(entry)
    # 两市总成交额 = 上证 + 深证（避免创业板/科创50重复叠加）
    for idx_map in INDICES[:2]:
        entry = next((e for e in indices if e["name"] == idx_map["name"]), None)
        if entry:
            total_turnover += float(entry.get("amount", "0"))
    print(f"    指数条目: {len(indices)} | 两市成交: {total_turnover:.0f}亿")

    # 3. 涨停板数据
    print("[3] 拉取涨停板数据…")
    zt_items = fetch_limit_up(today_str)
    limit_up_count = len(zt_items)
    blast_count = fetch_blasting(today_str)
    seal_rate = round(limit_up_count / (limit_up_count + blast_count) * 100, 1) if (limit_up_count + blast_count) > 0 else 0
    print(f"    涨停: {limit_up_count} | 炸板: {blast_count} | 封板率: {seal_rate}%")

    # 构建 boardLadder（涨停梯队）
    board_ladder = []
    zt_by_board = {}
    for item in zt_items:
        board = item.get("c", 1)
        key = f"{board}板"
        zt_by_board.setdefault(key, [])
        zt_by_board[key].append(item)

    for board_key in sorted(zt_by_board, key=lambda x: -int(x.replace("板", ""))):
        for item in zt_by_board[board_key][:5]:  # 每梯队最多5只
            code = item.get("c_code", "")
            name = item.get("c_name", "")
            chg = item.get("p", 0) or 0
            amount = (item.get("amount", 0) or 0) / 100000000
            seal_amt = (item.get("f_am", 0) or 0) / 100000000
            turnover = item.get("t", 0) or 0
            float_mcap = (item.get("mcap_f", 0) or 0) / 100000000
            first_seal = item.get("t_fst", "")
            blast_cnt = item.get("b", 0) or 0
            industry = item.get("hybk", "")
            board_ladder.append({
                "board": board_key, "code": code, "name": name,
                "chg": f"{chg:+.2f}%", "amount": f"{amount:.2f}",
                "sealAmt": f"{seal_amt:.2f}", "turnover": f"{turnover:.2f}%",
                "floatMcap": f"{float_mcap:.0f}", "firstSeal": first_seal,
                "blast": str(blast_cnt), "industry": industry,
                "attr": "—", "attrPill": "p-gray",
            })

    # 4. 板块排名
    print("[4] 拉取板块排名…")
    sector_raw = fetch_sector_ranking(10)
    sectors = []
    total_mkt_amount = total_turnover  # 从指数取的总成交
    for i, item in enumerate(sector_raw):
        name = item.get("f14", "") or ""
        chg = item.get("f3", 0) or 0
        amount = round((item.get("f6", 0) or 0) / 100000000, 1)
        share = f"{round(amount / total_mkt_amount * 100, 1)}%" if total_mkt_amount > 0 else "—"
        main_net = round((item.get("f66", 0) or 0) / 100000000, 1)
        limit_up = (item.get("f84", 0) or 0)
        up5 = (item.get("f102", 0) or 0)
        up_cnt = (item.get("f104", 0) or 0)
        down_cnt = (item.get("f105", 0) or 0)
        sectors.append({
            "rank": i + 1, "name": name, "chg": f"{chg:+.2f}%",
            "amount": str(int(amount)), "share": share,
            "mainNet": f"{main_net:+.1f}", "limitUp": limit_up,
            "up5": up5, "upCnt": up_cnt, "downCnt": down_cnt,
            "leader": "—", "leaderChg": "—",
            "fundLabel": "—", "fundPill": "p-gray",
        })
    print(f"    板块: {len(sectors)} 条")

    # 5. 构建 volume 对象
    turnover_5 = []  # 无法获取前5日均量, 默认填—
    volume = {
        "turnover": str(int(total_turnover)),
        "ratio5": "—", "ratio10": "—",
        "up": vol_data.get("up", 0), "down": vol_data.get("down", 0),
        "flat": vol_data.get("flat", 0), "ratio": f"{vol_data.get('ratio',0):.2f}",
        "mainNet": "—", "inCount": 0, "outCount": 0,
        "limitUp": limit_up_count, "limitDown": limit_down_count,
        "blast": blast_count, "sealRate": f"{seal_rate}%",
        "up5pct": vol_data.get("up5pct", 0),
        "down5pct": vol_data.get("down5pct", 0),
        "prevLimitUp": [limit_up_count],
    }

    # 6. 构建完整 review 对象
    review = {
        "date": today_dt.strftime("%Y-%m-%d"),
        "weekday": weekday_str,
        "isTradingDay": True,
        "conclusion": f"<b>{today_dt.strftime('%m/%d')} 收盘：</b>数据已自动生成（基础版）。AI 深度分析待接入。",
        "digest": f"{today_dt.strftime('%-m/%-d')} 收盘：涨停 {limit_up_count} 家，封板率 {seal_rate}%。AI 深度分析待接入。",
        "emotionStage": "数据驱动（待分析）",
        "position": "待分析",
        "summary": {
            "emotion": "数据驱动", "emotionSub": "基础数据已生成",
            "position": "待分析", "positionSub": "AI 分析待接入",
            "mainLine": "待分析", "mainLineSub": "—",
            "subLine": "—", "subLineSub": "—",
        },
        "concl_so": {"s1": ["基础数据已生成，深度分析待 AI 接入。"], "s2": [], "s3": [], "s4": ""},
        "indices": indices,
        "volume": volume,
        "sectors": sectors,
        "centers": [],
        "tiers": [],
        "boardLadder": board_ladder,
        "cycle": {"current": 0, "stages": []},
        "scenarios": [],
        "watch": [],
        "risks": [],
        "turnover": {"top50": "—", "top100": "—", "top300": "—", "top500": "—"},
        "disclaimer": f"本报告由 AI 系统基于公开市场数据自动生成，数据于 {today_dt.strftime('%Y年%-m月%-d日')} 15:00 收盘后通过东方财富公开接口实时抓取。基础数据版，AI 深度分析待接入。不构成任何形式的投资建议。",
    }

    # 7. 合并到 market-review.json
    print("[5] 合并到 market-review.json…")
    existing = {"reviews": []}
    if os.path.exists(REVIEW_FILE):
        try:
            with open(REVIEW_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    reviews = existing.get("reviews", [])
    # 去重：如果今天已有数据，替换
    reviews = [r for r in reviews if r.get("date") != review["date"]]
    reviews.insert(0, review)  # 最新在前
    reviews.sort(key=lambda x: x.get("date", ""), reverse=True)

    output = {
        "updatedAt": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "reviews": reviews,
    }

    with open(REVIEW_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(REVIEW_FILE) / 1024
    print(f"    保存: {len(reviews)} 条复盘 ({size_kb:.1f}KB)")
    print(f"═══ 完成 ═══")


if __name__ == "__main__":
    main()
