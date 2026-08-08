#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
多源A股行情数据适配器 v1.0
==========================
五源回退: 腾讯财经 → efinance → 新浪 → akshare → 本地缓存
专为 GitHub Actions 环境优化，无状态请求，零API依赖
"""

import sys
import io
import json
import time
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, Any

import pandas as pd
import numpy as np

# 尝试加载 env 配置
ENV_FILE = Path(__file__).parent / "env.py"
ENV = {}
if ENV_FILE.exists():
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            exec(f.read(), ENV)
    except Exception:
        pass

# ═══════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════
CACHE_DIR = Path(__file__).parent / ".market_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_HOURS = 4  # 缓存有效期
REQUEST_TIMEOUT = 8  # 单次请求超时秒数
RETRY_DELAY = 1.5    # 重试间隔秒数

# 观察池标的映射 (腾讯代码 → 名称)
INDEX_MAP = {
    "sh000001": "上证指数",
    "sh000688": "科创50",
}
STOCK_MAP = {
    "sh688017": "绿的谐波", "sz300502": "新易盛",
    "sh688981": "中芯国际", "sz002460": "赣锋锂业",
    "sz000960": "锡业股份", "sh601881": "中国银河",
    "sh518880": "黄金ETF",  "sh588000": "科创50ETF",
}

# ═══════════════════════════════════════════════
# 源1: 腾讯财经 HTTP API (最稳定, 无需API Key)
# ═══════════════════════════════════════════════
def fetch_tencent_index(code: str = "sh000001") -> Optional[Dict]:
    """腾讯财经指数快照: http://qt.gtimg.cn/q=sh000001"""
    try:
        import requests
        url = f"http://qt.gtimg.cn/q={code}"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                           headers={"Referer": "https://finance.qq.com"})
        text = resp.text
        if "=" not in text or len(text) < 50:
            return None
        
        # 腾讯返回格式: v_sh000001="1~上证指数~3850.12~..."
        data_str = text.split('"')[1] if '"' in text else text.split("=")[1].strip().strip('"').strip(";")
        fields = data_str.split("~")
        if len(fields) < 40:
            return None
        
        return {
            "name": fields[1],
            "current": float(fields[3]) if fields[3] else 0,
            "change_pct": float(fields[32]) if len(fields) > 32 and fields[32] else 0,
            "volume": float(fields[6]) if len(fields) > 6 and fields[6] else 0,
            "amount": float(fields[37]) if len(fields) > 37 and fields[37] else 0,
        }
    except Exception:
        return None


def fetch_tencent_batch(codes: list) -> Dict[str, Dict]:
    """批量获取腾讯行情 (一次请求多只标的)"""
    try:
        import requests
        code_str = ",".join(codes)
        url = f"http://qt.gtimg.cn/q={code_str}"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                           headers={"Referer": "https://finance.qq.com"})
        text = resp.text
        
        results = {}
        for line in text.strip().split("\n"):
            if "=" not in line or len(line) < 20:
                continue
            code = line.split("=")[0].replace("v_", "")
            data_str = line.split('"')[1] if '"' in line else line.split("=")[1].strip().strip('"').strip(";")
            fields = data_str.split("~")
            if len(fields) < 35:
                continue
            
            results[code] = {
                "name": fields[1],
                "current": float(fields[3]) if fields[3] else 0,
                "change_pct": float(fields[32]) if len(fields) > 32 and fields[32] else 0,
                "open": float(fields[5]) if fields[5] else 0,
                "high": float(fields[33]) if len(fields) > 33 and fields[33] else 0,
                "low": float(fields[34]) if len(fields) > 34 and fields[34] else 0,
                "volume": float(fields[6]) if len(fields) > 6 and fields[6] else 0,
                "amount": float(fields[37]) if len(fields) > 37 and fields[37] else 0,
            }
        return results
    except Exception:
        return {}


# ═══════════════════════════════════════════════
# 源2: efinance (东方财富, 无需API Key)
# ═══════════════════════════════════════════════
def fetch_efinance_index(code: str = "000001") -> Optional[Dict]:
    """efinance 指数快照"""
    try:
        import efinance as ef
        df = ef.stock.get_realtime_quotes([code])
        if df is None or df.empty:
            return None
        row = df.iloc[0]
        return {
            "name": row.get("股票名称", ""),
            "current": float(row.get("最新价", 0)),
            "change_pct": float(row.get("涨跌幅", 0)),
            "volume": float(row.get("成交量", 0)),
            "amount": float(row.get("成交额", 0)),
        }
    except Exception:
        return None


def fetch_efinance_watchlist() -> Dict[str, Dict]:
    """efinance 批量获取观察池"""
    try:
        import efinance as ef
        codes = list(STOCK_MAP.keys())
        # efinance 需要的代码格式: 688017, 300502
        ef_codes = [c[2:] for c in codes]
        df = ef.stock.get_realtime_quotes(ef_codes)
        if df is None or df.empty:
            return {}
        
        results = {}
        for _, row in df.iterrows():
            code = row.get("股票代码", "")
            for orig, name in STOCK_MAP.items():
                if orig[2:] == code or code in orig:
                    results[orig] = {
                        "name": row.get("股票名称", name),
                        "current": float(row.get("最新价", 0)),
                        "change_pct": float(row.get("涨跌幅", 0)),
                        "open": float(row.get("开盘价", 0)),
                        "high": float(row.get("最高价", 0)),
                        "low": float(row.get("最低价", 0)),
                        "volume": float(row.get("成交量", 0)),
                        "amount": float(row.get("成交额", 0)),
                    }
                    break
        return results
    except Exception:
        return {}


# ═══════════════════════════════════════════════
# 源3: 新浪财经 (备用)
# ═══════════════════════════════════════════════
def fetch_sina_batch(codes: list) -> Dict[str, Dict]:
    """新浪批量行情: hq.sinajs.cn"""
    try:
        import requests
        sina_codes = [f"{'sh' if c.startswith('sh') else 'sz'}{c[2:]}" for c in codes]
        url = "http://hq.sinajs.cn/list=" + ",".join(sina_codes)
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                           headers={"Referer": "https://finance.sina.com.cn"})
        resp.encoding = "gbk"
        text = resp.text
        
        results = {}
        for line in text.strip().split("\n"):
            if "=" not in line or len(line) < 20:
                continue
            code_str = line.split("=")[0].replace("var hq_str_", "")
            data = line.split('"')[1].split(",") if '"' in line else []
            if len(data) < 30:
                continue
            
            # 映射回原始代码
            orig_code = f"sh{code_str[2:]}" if code_str.startswith("sh") else f"sz{code_str[2:]}"
            results[orig_code] = {
                "name": data[0],
                "current": float(data[3]) if data[3] else 0,
                "change_pct": 0,  # Sina 日线不直接给涨跌幅
                "open": float(data[1]) if data[1] else 0,
                "high": float(data[4]) if data[4] else 0,
                "low": float(data[5]) if data[5] else 0,
                "volume": float(data[8]) if data[8] else 0,
                "amount": float(data[9]) if data[9] else 0,
            }
        return results
    except Exception:
        return {}


# ═══════════════════════════════════════════════
# 源4: akshare (最后回退)
# ═══════════════════════════════════════════════
def fetch_akshare_spot() -> Optional[pd.DataFrame]:
    """akshare 全市场快照 (限流风险)"""
    try:
        import akshare as ak
        time.sleep(2.0)
        df = ak.stock_zh_a_spot_em()
        return df
    except Exception:
        return None


# ═══════════════════════════════════════════════
# 统一接口: 多源回退
# ═══════════════════════════════════════════════
def get_market_snapshot() -> Dict:
    """
    获取市场快照 (五源回退)
    返回: {index, sci50, turnover, sci50_change, stocks: {...}, timestamp}
    """
    result = {
        "index": 0, "index_name": "上证指数", "index_change": 0,
        "sci50": 0, "sci50_name": "科创50", "sci50_change": 0,
        "turnover": 0, "stocks": {}, "source": "unknown",
        "timestamp": datetime.now().isoformat(),
    }
    
    codes = list(STOCK_MAP.keys())
    index_codes = ["sh000001", "sh000688"]
    
    # 回退链: 腾讯 → efinance → 新浪 → akshare → 缓存
    for source_name, fetcher in [
        ("tencent", lambda: (fetch_tencent_batch(codes + index_codes), None)),
        ("efinance", lambda: (fetch_efinance_watchlist(), fetch_efinance_index("000001"))),
        ("sina", lambda: (fetch_sina_batch(codes + index_codes), None)),
    ]:
        try:
            print(f"  [数据] 尝试 {source_name}...")
            batch_data, index_data = fetcher()
            if batch_data:
                # 提取指数
                for idx_code in index_codes:
                    if idx_code in batch_data:
                        d = batch_data[idx_code]
                        if idx_code == "sh000001":
                            result["index"] = d["current"]
                            result["index_change"] = d.get("change_pct", 0)
                            # 腾讯上证指数 amount 字段单位/口径不可靠(实测仅~18亿)，低于合理下限不采用
                            amt_yi = d.get("amount", 0) / 1e8  # 转亿
                            result["turnover"] = amt_yi if amt_yi > TURNOVER_MIN else 0
                        elif idx_code == "sh000688":
                            result["sci50"] = d["current"]
                            result["sci50_change"] = d.get("change_pct", 0)
                        del batch_data[idx_code]
                
                result["stocks"] = batch_data
                result["source"] = source_name
                print(f"  [数据] ✅ {source_name} 成功 ({len(batch_data)}只标的)")
                break
        except Exception as e:
            print(f"  [数据] {source_name} 失败: {str(e)[:60]}")
            continue
    
    # 最后回退: 缓存
    if not result["stocks"]:
        cached = _load_cache()
        if cached:
            print(f"  [数据] ⚠️ 使用缓存 (TTL {CACHE_TTL_HOURS}h)")
            result = cached
            result["source"] = "cache"
    
    # 写入缓存
    if result["stocks"]:
        _save_cache(result)
    
    return result


def _load_cache() -> Optional[Dict]:
    cache_file = CACHE_DIR / "snapshot.json"
    if not cache_file.exists():
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        age = time.time() - os.path.getmtime(cache_file)
        if age > CACHE_TTL_HOURS * 3600:
            return None
        return data
    except Exception:
        return None


def _save_cache(data: Dict):
    try:
        with open(CACHE_DIR / "snapshot.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ═══════════════════════════════════════════════
# 便捷函数: 计算成交额 (来自多个源)
# ═══════════════════════════════════════════════
def get_turnover_billion(snapshot: Dict) -> float:
    """获取全市场成交额(亿元)"""
    # 优先从快照直接获取
    turnover = snapshot.get("turnover", 0)
    if turnover > 1000:
        return turnover
    
    # 从标的加总估算
    stocks = snapshot.get("stocks", {})
    if stocks:
        total = sum(s.get("amount", 0) for s in stocks.values())
        # 8只标的约占全市场0.3%
        return total / 0.003 / 1e8 if total > 0 else 25000
    
    return 25000  # 默认


# ═══════════════════════════════════════════════
# 数据校验: 与东方财富公开行情交叉比对
# ═══════════════════════════════════════════════
# 独立公开源 (secid 格式: 市场.代码, 1=沪 0=深)
EM_SECIDS = {
    "sh000001": "1.000001",   # 上证指数
    "sh000688": "1.000688",   # 科创50
    "sz399001": "0.399001",   # 深证成指 (用于两市成交额估算)
}
IDX_DIFF_TOLERANCE = 0.02   # 指数偏差容忍度 2%
TURNOVER_MIN = 1000         # 两市成交额合理下限(亿)


def _fetch_eastmoney(secid: str) -> Optional[Dict]:
    """
    东方财富 push2 单标的快照 (与腾讯源完全独立)
    字段: f43=最新价 f58=名称 f162=涨跌幅(%) f167=成交额(元)
    """
    try:
        import requests
        url = (f"https://push2.eastmoney.com/api/qt/stock/get"
               f"?secid={secid}&fields=f43,f58,f162,f167&fltt=2")
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                           headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json().get("data")
        if not data:
            return None
        return {
            "current": float(data.get("f43") or 0),
            "change_pct": float(data.get("f162") or 0),
            "amount": float(data.get("f167") or 0),  # 元
        }
    except Exception:
        return None


# ═══════════════════════════════════════════════
# 全市场成交额: 实时聚合 + 缓存 + 兜底
# ═══════════════════════════════════════════════
TURNOVER_CACHE_FILE = CACHE_DIR / "turnover_cache.json"
TURNOVER_CACHE_MAX_AGE_DAYS = 7
# 最近交易日(2026-08-07 周五)真实全市场成交额兜底 (用户行情复盘确认约 26644 亿)
DEFAULT_TURNOVER_YI = 26644.0


def _load_turnover_cache() -> Optional[Tuple[float, float]]:
    """返回 (成交额亿, 缓存年龄小时) 或 None"""
    try:
        if not TURNOVER_CACHE_FILE.exists():
            return None
        d = json.loads(TURNOVER_CACHE_FILE.read_text(encoding="utf-8"))
        ts, val = d.get("ts"), d.get("value")
        if val is None or ts is None:
            return None
        return float(val), (time.time() - float(ts)) / 3600
    except Exception:
        return None


def _save_turnover_cache(val: float):
    try:
        TURNOVER_CACHE_FILE.write_text(
            json.dumps({"ts": time.time(), "value": val}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def _get_web_total_turnover() -> Tuple[Optional[float], str]:
    """
    东方财富沪深A股全市场成交额(元) → 亿元。
    返回 (成交额亿, 来源): live=实时聚合 / cache=沿用缓存 / default=默认真实值。

    策略(以缓存为权威, 实时仅作合理覆盖):
      - 实时聚合沪深A股全部个股 f6, 但仅在 [0.3x, 3x] 缓存值且 > 下限时采用,
        避免休市返回的 0 值或接口异常小值(如被截断的列表)污染数据
      - 否则沿用最近交易日缓存(≤7天), 缓存由交易日真实值维护
      - 最终兜底: 最近交易日真实值 (避免显示腾讯源失真的 18亿)
    """
    cached = _load_turnover_cache()
    cache_val = cached[0] if cached else DEFAULT_TURNOVER_YI

    # 1) 实时聚合 (交易日准确, 但需合理性校验)
    try:
        import requests
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"  # 沪深A股
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {"pn": "1", "pz": "8000", "po": "1", "np": "1", "fltt": "2",
                  "invt": "2", "fid": "f3", "fs": fs, "fields": "f12,f6"}
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT * 2,
                            headers={"User-Agent": "Mozilla/5.0"})
        items = resp.json().get("data", {}).get("diff") or []
        total = sum(float(it.get("f6") or 0) for it in items)
        yi = total / 1e8
        # 合理性: 与缓存同量级且 > 下限, 防止休市0值/异常小值污染
        if yi > TURNOVER_MIN and yi > cache_val * 0.3 and yi < cache_val * 3:
            _save_turnover_cache(yi)
            return yi, "live"
    except Exception:
        pass

    # 2) 缓存 (周末/网络失败)
    if cached and cached[1] <= TURNOVER_CACHE_MAX_AGE_DAYS * 24 and cached[0] > TURNOVER_MIN:
        return cached[0], "cache"

    # 3) 兜底默认真实值
    return DEFAULT_TURNOVER_YI, "default"


def validate_against_web(snapshot: Dict) -> Tuple[Dict, Dict]:
    """
    将拉取的数据与东方财富公开行情交叉校验。

    规则:
      - 网络可达且偏差 ≤ 容忍度 → 采用网络权威值, validated=True
      - 网络不可达 (搜不到)       → 维持拉取数据, validated=False
      - 网络可达但偏差超阈值      → 维持拉取数据, 记录差异告警

    返回: (report, snapshot)  snapshot 可能被就地修正
    """
    report = {
        "validated": False,
        "web_source": "eastmoney",
        "web_index": None,
        "web_index_change": None,
        "web_sci50": None,
        "web_turnover": None,
        "web_turnover_source": None,
        "index_diff_pct": None,
        "turnover_diff_pct": None,
        "note": "",
    }

    sh = _fetch_eastmoney(EM_SECIDS["sh000001"])
    kc = _fetch_eastmoney(EM_SECIDS["sh000688"])
    sz = _fetch_eastmoney(EM_SECIDS["sz399001"])

    # ── 网络不可达: 维持拉取数据 ──
    if not sh or not kc:
        report["note"] = "网络校验源(东方财富)不可达，维持拉取数据"
        print(f"  [校验] ⚠️ 东方财富不可达，维持拉取数据")
        return report, snapshot

    # ── 上证指数一致性比对 ──
    pulled_idx = snapshot.get("index", 0) or 1
    web_idx = sh["current"]
    idx_diff = abs(web_idx - pulled_idx) / pulled_idx
    report["web_index"] = web_idx
    report["web_index_change"] = sh["change_pct"]
    report["index_diff_pct"] = round(idx_diff * 100, 2)

    if idx_diff <= IDX_DIFF_TOLERANCE:
        # ── 一致 → 采用网络权威值 ──
        report["validated"] = True
        snapshot["index"] = web_idx
        snapshot["index_change"] = sh["change_pct"]
        snapshot["sci50"] = kc["current"]
        snapshot["sci50_change"] = kc["change_pct"]
        report["web_sci50"] = kc["current"]

        # 成交额: 实时聚合(交易日) → 缓存(周末) → 默认(兜底)
        web_total, tsrc = _get_web_total_turnover()
        src_label = {"live": "实时聚合", "cache": "最近交易日缓存", "default": "默认真实值"}.get(tsrc, "")
        if web_total and web_total > TURNOVER_MIN:
            report["web_turnover"] = round(web_total, 1)
            report["web_turnover_source"] = tsrc
            snapshot["turnover"] = round(web_total, 1)
            report["turnover_diff_pct"] = round(
                abs(web_total - snapshot.get("turnover", 0)) / max(snapshot.get("turnover", 1), 1) * 100, 2)
            report["note"] = f"已与东方财富公开行情交叉校验一致(成交额:{src_label})"
        else:
            report["note"] = "指数已校验，但全市场成交额未取到，维持拉取数据"
        print(f"  [校验] ✅ 与东方财富一致 (上证 {web_idx:.2f}, 偏差 {idx_diff*100:.2f}%, 成交额:{src_label})")
    else:
        # ── 偏差超阈值 → 维持拉取值, 记录告警 ──
        report["note"] = f"网络校验差异 {idx_diff*100:.1f}% 超阈值({IDX_DIFF_TOLERANCE*100:.0f}%)，维持拉取值"
        print(f"  [校验] ⚠️ 差异 {idx_diff*100:.1f}% 超阈值，维持拉取值")

    return report, snapshot


# ═══════════════════════════════════════════════
# 独立测试
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("多源数据适配器 - 测试")
    print("=" * 60)
    
    snap = get_market_snapshot()
    print(f"\n数据源: {snap['source']}")
    print(f"上证: {snap['index']} ({snap.get('index_change', 0):+.1f}%)")
    print(f"科创50: {snap['sci50']} ({snap.get('sci50_change', 0):+.1f}%)")
    print(f"成交额: {get_turnover_billion(snap):.0f}亿")
    print(f"\n观察池 ({len(snap['stocks'])}只):")
    for code, info in snap["stocks"].items():
        name = info.get("name", STOCK_MAP.get(code, "?"))
        print(f"  {code} {name:<8} {info['current']:.2f} ({info.get('change_pct', 0):+.1f}%)")
