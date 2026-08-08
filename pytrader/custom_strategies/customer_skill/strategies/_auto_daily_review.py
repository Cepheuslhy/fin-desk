#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
每日策略自动评估脚本 v1.0
==========================
专为 GitHub Actions 设计，无LLM依赖，规则引擎驱动

输出: daily_report.md (日报) + daily_report.json (结构化数据)
"""

import sys
import os
import io
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import Counter

import numpy as np
import pandas as pd

# 路径初始化
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# 查找 repo 根目录（向上找到 .git 或 news/ 目录）
REPO_ROOT = SCRIPT_DIR
for _ in range(5):
    if (REPO_ROOT / "news").exists() or (REPO_ROOT / ".git").exists():
        break
    REPO_ROOT = REPO_ROOT.parent

# ═══════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════
OUTPUT_MD = SCRIPT_DIR / "daily_report.md"
OUTPUT_JSON = SCRIPT_DIR / "daily_report.json"
NEWS_DIR = REPO_ROOT / "news"  # 使用 fin-desk 已有的华尔街见闻新闻抓取
WATCHLIST = {
    "sh688017": {"name": "绿的谐波", "industry": "机器人"},
    "sz300502": {"name": "新易盛", "industry": "光模块"},
    "sh688981": {"name": "中芯国际", "industry": "半导体"},
    "sz002460": {"name": "赣锋锂业", "industry": "锂矿"},
    "sz000960": {"name": "锡业股份", "industry": "锡"},
    "sh601881": {"name": "中国银河", "industry": "券商"},
    "sh518880": {"name": "黄金ETF", "industry": "黄金"},
    "sh588000": {"name": "科创50ETF", "industry": "科技"},
}

# ═══════════════════════════════════════════════
# 1. 行情获取
# ═══════════════════════════════════════════════
def get_market_data() -> Dict:
    """获取行情快照 (多源回退 + 网络校验)"""
    try:
        from multi_source_data import get_market_snapshot, get_turnover_billion, validate_against_web
        snap = get_market_snapshot()
        market = {
            "index": snap["index"],
            "index_change": snap.get("index_change", 0),
            "sci50": snap["sci50"],
            "sci50_change": snap.get("sci50_change", 0),
            "turnover": get_turnover_billion(snap),
            "stocks": snap.get("stocks", {}),
            "source": snap.get("source", "unknown"),
        }
        # 网络校验: 与东方财富公开行情交叉比对 (搜不到则维持拉取数据)
        validation, market = validate_against_web(market)
        market["validation"] = validation
        return market
    except Exception as e:
        print(f"[行情] 获取失败: {e}")
        return {
            "index": 0, "index_change": 0, "sci50": 0,
            "sci50_change": 0, "turnover": 0, "stocks": {},
            "source": "fallback",
            "validation": {"validated": False, "note": f"行情获取异常: {e}"},
        }


# ═══════════════════════════════════════════════
# 2. 新闻情感分析
# ═══════════════════════════════════════════════
BULL = {"暴涨": 0.9, "飙升": 0.8, "翻倍": 0.95, "超预期": 0.85, "突破": 0.7,
        "量产": 0.75, "创纪录": 0.8, "扭亏": 0.65, "新高": 0.65, "涨停": 0.85,
        "利好": 0.6, "大涨": 0.7, "创新高": 0.75, "超市场预期": 0.8}
BEAR = {"暴跌": -0.9, "崩盘": -0.95, "腰斩": -0.85, "暴雷": -0.9,
        "不及预期": -0.8, "亏损": -0.7, "泡沫": -0.7, "停产": -0.6,
        "裁员": -0.55, "跌停": -0.85, "大跌": -0.6, "下修": -0.5,
        "衰退": -0.7, "危机": -0.65}

KEYWORDS_BY_SYMBOL = {
    "sh688017": ["机器人", "谐波", "减速器", "电机", "人形", "宇树", "特斯拉"],
    "sz300502": ["光模块", "英伟达", "CPO", "800G", "GPU", "数据中心"],
    "sh688981": ["存储", "长鑫", "半导体", "芯", "晶圆", "三星", "DRAM"],
    "sz002460": ["锂", "赣锋", "电池", "新能源", "锂矿"],
    "sz000960": ["锡", "有色金属", "焊料", "电子"],
    "sh601881": ["券商", "银河", "经纪", "两融", "成交量", "证监会"],
    "sh518880": ["黄金", "避险", "央行购金", "地缘", "通胀"],
    "sh588000": ["科创", "半导体", "AI", "芯片", "科技"],
}


def get_three_day_sentiment() -> Tuple[Dict, Dict[str, List[float]]]:
    """合并最近三天新闻情感（读取 fin-desk 已有的华尔街见闻新闻）"""
    today = datetime.now()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]
    dates.sort()
    
    daily_sent = {}
    stock_hits = {code: [] for code in WATCHLIST}
    
    for d in dates:
        news_file = NEWS_DIR / f"{d}.json"
        if not news_file.exists():
            daily_sent[d] = {"avg_sentiment": 0, "articles": 0}
            continue
        
        try:
            with open(news_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", [])
        except Exception:
            daily_sent[d] = {"avg_sentiment": 0, "articles": 0}
            continue
        
        sent_total = 0
        sent_count = 0
        
        for item in items:
            # 新闻内容 + 频道标签作为上下文
            content = (item.get("c", "") or "")[:500]
            channel = item.get("ch", "") or ""
            text = content + channel
            
            # 情感计算
            for w, v in BULL.items():
                if w in text:
                    sent_total += v
                    sent_count += 1
            for w, v in BEAR.items():
                if w in text:
                    sent_total += v
                    sent_count += 1
            
            # 标的匹配（需 ≥2 个关键词同时命中）
            for code, kws in KEYWORDS_BY_SYMBOL.items():
                if sum(1 for kw in kws if kw in text) >= 2:
                    stock_hits[code].append({
                        "date": d, "title": content[:80],
                        "keywords": [kw for kw in kws if kw in text],
                    })
        
        avg = sent_total / max(sent_count, 1)
        daily_sent[d] = {"avg_sentiment": round(avg, 3), "articles": len(items)}
    
    return daily_sent, stock_hits


def get_sentiment_trend(daily_sent: Dict) -> str:
    """判断三日情感趋势"""
    dates = sorted(daily_sent.keys())
    if len(dates) < 3:
        return "数据不足"
    
    s = [daily_sent[d]["avg_sentiment"] for d in dates]
    
    if s[2] > s[1] > s[0]:
        return f"三连升 ↗ ({s[0]:+.3f}→{s[1]:+.3f}→{s[2]:+.3f})"
    elif s[2] < s[1] < s[0]:
        return f"三连降 ↘ ({s[0]:+.3f}→{s[1]:+.3f}→{s[2]:+.3f})"
    elif s[2] > s[1] and s[1] < s[0]:
        return f"V型修复 ({s[0]:+.3f}→{s[1]:+.3f}→{s[2]:+.3f})"
    elif s[2] < s[1] and s[1] > s[0]:
        return f"倒V回落 ({s[0]:+.3f}→{s[1]:+.3f}→{s[2]:+.3f})"
    else:
        return f"震荡 ({s[0]:+.3f}→{s[1]:+.3f}→{s[2]:+.3f})"


# ═══════════════════════════════════════════════
# 3. 周期定位 (简化版，不依赖策略内部类)
# ═══════════════════════════════════════════════
def detect_cycle_phase(market: Dict, sentiment: Dict) -> Tuple[str, str]:
    """简化版六阶段周期定位"""
    turnover = market.get("turnover", 0)
    sci50_chg = market.get("sci50_change", 0)
    dates = sorted(sentiment.keys())
    latest_sent = sentiment[dates[-1]]["avg_sentiment"] if dates else 0
    
    # 简化判断逻辑
    if sci50_chg > 2 and turnover > 30000 and latest_sent > 0.35:
        phase, advice = "高潮/进攻", "5-8成仓位，持股但警惕分化"
    elif sci50_chg > 0.5 and turnover > 25000 and latest_sent > 0.25:
        phase, advice = "启动/发酵", "5-7成仓位，积极操作"
    elif sci50_chg < -3 and latest_sent < 0.15:
        phase, advice = "冰点/退潮", "0-2成仓位，等待修复"
    elif -1 < sci50_chg < 1 and 0.15 < latest_sent < 0.3:
        phase, advice = "分歧", "3-5成仓位，等待方向"
    else:
        phase, advice = "震荡", "3-5成仓位，灵活应对"
    
    return phase, advice


# ═══════════════════════════════════════════════
# 4. 规则引擎: 生成操作建议
# ═══════════════════════════════════════════════
def generate_actions(market: Dict, sentiment: Dict,
                     stock_hits: Dict) -> List[Dict]:
    """规则引擎生成逐标的操作建议"""
    actions = []
    sci50_chg = market.get("sci50_change", 0)
    turnover = market.get("turnover", 0)
    stocks_data = market.get("stocks", {})
    dates = sorted(sentiment.keys())
    latest_sent = sentiment[dates[-1]]["avg_sentiment"] if dates else 0
    trend = get_sentiment_trend(sentiment)
    
    for code, info in WATCHLIST.items():
        name = info["name"]
        industry = info["industry"]
        hits = stock_hits.get(code, [])
        
        # 默认仓位
        position = "满仓"
        
        # ── 规则1: 黄金独立逻辑 ──
        if code == "sh518880":
            # 地缘+利率双驱动
            if latest_sent > 0.3 and trend.startswith("三连升"):
                position = "加至25%"
            elif latest_sent > 0.2:
                position = "维持20%"
            else:
                position = "维持15%"
        
        # ── 规则2: 科创50ETF ──
        elif code == "sh588000":
            if sci50_chg > 3 and turnover > 28000:
                position = "满仓"
            elif sci50_chg < -3:
                position = "减至50%"
            elif trend.startswith("三连升"):
                position = "满仓"
            else:
                position = "维持80%"
        
        # ── 规则3: 券商 (利率敏感) ──
        elif code == "sh601881":
            if turnover > 30000:
                position = "加至85%"
            elif turnover > 25000:
                position = "维持70%"
            else:
                position = "维持50%"
        
        # ── 规则4: 科技/AI链 ──
        elif industry in ("光模块", "半导体"):
            if trend.startswith("三连升") and latest_sent > 0.35:
                position = "满仓"
            elif trend.startswith("三连降") or latest_sent < 0.2:
                position = "减至60%"
            else:
                position = "维持85%"
        
        # ── 规则5: 周期股 ──
        elif industry in ("锂矿", "锡"):
            position = "满仓"  # 独立周期不受AI波动
        
        # ── 规则6: 机器人 ──
        elif industry == "机器人":
            position = "超配120%" if latest_sent > 0.25 else "满仓"
        
        # ── 新闻驱动微调 ──
        if hits:
            # 最近一天有≥3条匹配→加仓倾向
            recent_hits = [h for h in hits if h["date"] == dates[-1]]
            if len(recent_hits) >= 3 and "减" not in position:
                position = position.replace("维持", "加至")
        
        actions.append({
            "code": code, "name": name, "industry": industry,
            "position": position, "news_hits": len(hits),
            "price": stocks_data.get(code, {}).get("current", 0),
            "change_pct": stocks_data.get(code, {}).get("change_pct", 0),
        })
    
    return actions


# ═══════════════════════════════════════════════
# 5. 生成报告
# ═══════════════════════════════════════════════
def generate_report(market: Dict, sentiment: Dict,
                    stock_hits: Dict, actions: List[Dict],
                    phase: str, advice: str) -> str:
    """生成完整日报 Markdown"""
    dates = sorted(sentiment.keys())
    trend = get_sentiment_trend(sentiment)
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    lines = []
    lines.append(f"# 每日策略评估日报")
    lines.append(f"")
    lines.append(f"> 自动生成于 {today_str} | 数据源: {market['source']} | 规则引擎 v1.0")
    lines.append(f"")
    
    # ── 数据校验状态 ──
    v = market.get("validation", {})
    if v.get("validated"):
        lines.append(f"> ✅ **数据已校验**: {v.get('note', '')} (东方财富)")
    else:
        lines.append(f"> ⚠️ **数据未校验**: {v.get('note', '无校验信息')}")
    lines.append(f"")
    
    # ── 一、大盘定位 ──
    lines.append(f"## 一、大盘定位")
    lines.append(f"")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 上证指数 | {market['index']:.0f} ({market['index_change']:+.2f}%) |")
    lines.append(f"| 科创50 | {market['sci50']:.0f} ({market['sci50_change']:+.2f}%) |")
    lines.append(f"| 成交额 | {market['turnover']:.0f}亿 |")
    lines.append(f"| 周期阶段 | **{phase}** |")
    lines.append(f"| 仓位建议 | {advice} |")
    lines.append(f"")
    
    # ── 二、三日情感 ──
    lines.append(f"## 二、三日新闻情感")
    lines.append(f"")
    lines.append(f"**趋势**: {trend}")
    lines.append(f"")
    lines.append(f"| 日期 | 情感均值 | 文章数 |")
    lines.append(f"|------|:---:|:---:|")
    for d in dates:
        s = sentiment[d]
        bar = "🟢" * min(5, int(s["avg_sentiment"] * 15 + 1)) if s["avg_sentiment"] > 0 else "🔴"
        lines.append(f"| {d} | {bar} {s['avg_sentiment']:+.3f} | {s['articles']} |")
    lines.append(f"")
    
    # ── 三、操作建议 ──
    lines.append(f"## 三、观察池操作建议")
    lines.append(f"")
    lines.append(f"| 标的 | 行业 | 仓位 | 新闻命中 | 涨跌 |")
    lines.append(f"|------|------|:---:|:---:|:---:|")
    for a in actions:
        hits_str = f"🔥{a['news_hits']}" if a["news_hits"] >= 5 else str(a["news_hits"])
        chg = a.get("change_pct", 0)
        chg_str = f"{chg:+.1f}%" if chg != 0 else "-"
        lines.append(f"| {a['name']} | {a['industry']} | **{a['position']}** | {hits_str} | {chg_str} |")
    lines.append(f"")
    
    # ── 四、核心新闻 ──
    lines.append(f"## 四、今日核心新闻")
    lines.append(f"")
    today_hits = []
    for code, hits in stock_hits.items():
        for h in hits:
            if h["date"] == dates[-1]:
                today_hits.append(f"- [{WATCHLIST[code]['name']}] {h['title']}")
    
    if today_hits:
        for h in today_hits[:15]:
            lines.append(h)
    else:
        lines.append("(无匹配新闻，请手动查看)")
    lines.append(f"")
    
    # ── 五、风险提示 ──
    lines.append(f"## 五、风险提示")
    lines.append(f"")
    if phase in ("高潮/进攻",):
        lines.append(f"- ⚠️ 高潮阶段 → 警惕分化，做好止盈准备")
    if market["sci50_change"] < -3:
        lines.append(f"- 🚨 科创50跌超3% → 关注是否触发熔断")
    if market["turnover"] < 20000:
        lines.append(f"- ⚠️ 成交额偏低 → 流动性不足")
    if trend.startswith("三连降"):
        lines.append(f"- 🔴 情感三连降 → 降低仓位，等待修复")
    lines.append(f"- ⚡ 本报告由规则引擎自动生成，仅供参考，不构成投资建议")
    lines.append(f"")
    
    lines.append(f"---")
    lines.append(f"*Generated by Qbot Daily Review Engine v1.0*")
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════
def main():
    print("=" * 60)
    print("Qbot 每日策略评估 v1.0")
    print(f"时间: {datetime.now().isoformat()}")
    print("=" * 60)
    
    # Step 1: 行情
    print("\n[1/4] 获取行情...")
    market = get_market_data()
    v = market.get("validation", {})
    vflag = "✅已校验" if v.get("validated") else "⚠️未校验"
    print(f"  上证: {market['index']:.0f} | 科创50: {market['sci50']:.0f} | 成交: {market['turnover']:.0f}亿 | 源: {market['source']} | 校验: {vflag}")
    
    # Step 2: 新闻情感
    print("\n[2/4] 分析三日新闻情感...")
    sentiment, stock_hits = get_three_day_sentiment()
    for d, s in sorted(sentiment.items()):
        print(f"  {d}: {s['avg_sentiment']:+.3f} ({s['articles']}篇)")
    
    # Step 3: 周期定位
    print("\n[3/4] 周期定位...")
    phase, advice = detect_cycle_phase(market, sentiment)
    print(f"  阶段: {phase} | 建议: {advice}")
    
    # Step 4: 生成操作建议
    print("\n[4/4] 生成操作建议...")
    actions = generate_actions(market, sentiment, stock_hits)
    for a in actions:
        print(f"  {a['name']:<8} → {a['position']}")
    
    # Step 5: 输出报告
    report = generate_report(market, sentiment, stock_hits, actions, phase, advice)
    
    # 写入文件
    OUTPUT_MD.write_text(report, encoding="utf-8")
    print(f"\n✅ 日报已生成: {OUTPUT_MD}")
    
    # JSON 结构化输出
    json_data = {
        "timestamp": datetime.now().isoformat(),
        "market": market,
        "sentiment": {d: s for d, s in sorted(sentiment.items())},
        "phase": phase, "advice": advice,
        "actions": actions,
    }
    OUTPUT_JSON.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ JSON已生成: {OUTPUT_JSON}")
    
    print(f"\n{'='*60}")
    print(report)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
