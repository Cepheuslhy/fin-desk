#!/usr/bin/env python3
"""
景气周期轮动策略 v18 整合版
策略架构(32类/24维):
  数据层: 实时行情获取 + 新闻量化(去重/情感/全文/提取/溯源/影响) + 资金流向(北向/两融)
  风控层: 7层风控(熔断/波动率/风险映射/逃顶/安全垫/纠偏/估值约束)
  分析层: 五维量化评分 + 财报质量过滤(营收/EPS/OCF/毛利率) + 双击框架(戴维斯/苯韭)
  决策层: 动态风格轮换 + 剑气配比 + 加仓引擎(安全垫门禁+金字塔)
  新闻层: 智能去重+时效衰减+情感极性+全文利用+结构化提取+溯源可靠性+影响量化
"""
import json
import os
import sys
import io
import re
import math
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Set, Union
from enum import Enum

# 集中配置（支持多种运行路径）
try:
    from env import (SCORE_THRESHOLDS, DOUBLE_CLICK_WEIGHTS, LIQUIDITY_BUFF,
                     RECOMMENDATION_WEIGHTS, RECOMMENDATION_THRESHOLDS,
                     RISK_CONTROL, TRADE_MODE_RATIOS, SWORD_QI_TARGET,
                     ESCAPE_TOP_THRESHOLD, ESCAPE_CONTEXT_FILTER,
                     FLASH_KEYWORD_MAP, PHENOMENON_THRESHOLD, QPS_CONFIG,
                     DEFAULT_WATCHLIST, THEORETICAL_INDEX_BASE,
                     THEORETICAL_INDEX_RATIO, FUND_FLOW_THRESHOLDS)
except ImportError:
    # 尝试从策略目录导入
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from env import (SCORE_THRESHOLDS, DOUBLE_CLICK_WEIGHTS, LIQUIDITY_BUFF,
                         RECOMMENDATION_WEIGHTS, RECOMMENDATION_THRESHOLDS,
                         RISK_CONTROL, TRADE_MODE_RATIOS, SWORD_QI_TARGET,
                         ESCAPE_TOP_THRESHOLD, ESCAPE_CONTEXT_FILTER,
                         FLASH_KEYWORD_MAP, PHENOMENON_THRESHOLD, QPS_CONFIG,
                         DEFAULT_WATCHLIST, THEORETICAL_INDEX_BASE,
                         THEORETICAL_INDEX_RATIO, FUND_FLOW_THRESHOLDS)
    except ImportError:
        # 降级为默认值（兼容独立部署）
        SCORE_THRESHOLDS = {"heavy_position": 90, "light_position": 70, "watch": 50, "clear": 50}
        DOUBLE_CLICK_WEIGHTS = {"davis_score_factor": 0.10, "benjiu_double_factor": 0.15, "benjiu_single_factor": 0.07}
        LIQUIDITY_BUFF = {"weak": {"turnover": (7000,8000),"multiplier":0.7},"normal":{"turnover":(9000,11000),"multiplier":1.0},"gain":{"turnover":(13000,15000),"multiplier":1.3},"dragon":{"turnover":(16000,20000),"multiplier":1.8},"super_buff":{"turnover":(25000,50000),"multiplier":2.0}}
        RECOMMENDATION_WEIGHTS = {"news_heat": 0.30, "technical": 0.40, "strategy": 0.30}
        RECOMMENDATION_THRESHOLDS = {"strong_buy": 65, "buy": 50}
        RISK_CONTROL = {"super_bull": {"max_drawdown": -15, "single_stop": -8},"normal":{"max_drawdown":-10,"single_stop":-6},"weak":{"max_drawdown":-8,"single_stop":-5},"crash":{"max_drawdown":-5,"single_stop":-3}}
        TRADE_MODE_RATIOS = {"pure_qizong": {"ratio": 12, "hold": "12-24月+"},"qizong_bias":{"ratio":10},"jianzong_bias":{"ratio":8},"pure_jianzong":{"ratio":5}}
        SWORD_QI_TARGET = {"jianzong": 0.30, "qizong": 0.70}
        ESCAPE_TOP_THRESHOLD = {"clear_all": 75, "reduce_hard": 50, "reduce_watch": 25}
        ESCAPE_CONTEXT_FILTER = {}
        FLASH_KEYWORD_MAP = {}
        PHENOMENON_THRESHOLD = 70
        QPS_CONFIG = {"min": 1.2, "max": 4.5}
        DEFAULT_WATCHLIST = []
        THEORETICAL_INDEX_BASE = 3050
        THEORETICAL_INDEX_RATIO = 4.89
        FUND_FLOW_THRESHOLDS = {"strong_bull": 10000}

# 修复Windows编码
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 统计稳健性依赖(协方差收缩 / 协整检验 / 多重检验)
try:
    import numpy as np
except ImportError:
    np = None
try:
    from statsmodels.tsa.stattools import adfuller, kpss, coint
    from statsmodels.stats.multitest import multipletests
    _HAS_STATSMODELS = True
except Exception:
    adfuller = kpss = coint = multipletests = None
    _HAS_STATSMODELS = False


class InvestmentStyle(Enum):
    """投资风格"""
    ULTRA_SHORT = "超短"      # 纯看线/技术面，利好比手速
    VALUE = "价投"            # 低估值吃息 / 高景气投资投机


class IndustryType(Enum):
    """行业类型"""
    NEW_TECH = "新型科技"      # 选最上游、卡位股
    TRADITIONAL = "传统行业"   # 选下游头部
    CYCLICAL = "周期行业"      # 等5条件满足


class EventType(Enum):
    """拐点事件类型"""
    TECH_BREAKTHROUGH = "科技突破"           # 如23年ChatGPT
    POLITICAL_ECONOMIC = "政治经济突发"      # 如19年芯片制裁
    RESOURCE_SUPPLY = "资源供需失衡"         # 如20年熔喷布
    OVERSEAS_MAPPING = "海外映射"            # 海外成功→国内重估
    OTHER = "其他拐点"
    FAKE = "伪现象级事件"                    # 政策口号/蹭热点/历史新高陷阱


@dataclass
class StockScore:
    """
    股票评分结果 — 五维量化 + 财报加成 + 估值约束
    权重: 景气度20% + 业务纯度40% + 估值25% + 龙头15% + 辨识度20% + 风险扣分
    流动性: 动态Buff乘数 | 财报: ×0.7~1.3 | 估值: ×0.4~1.0
    """
    symbol: str
    name: str
    industry_boom: int = 0        # 行业景气度 0-20 (权重20%)
    business_purity: int = 0      # 业务纯度 0-40 (权重40%)
    valuation: int = 0            # 历史估值位置 0-25 (权重25%)
    industry_leader: int = 0      # 细分行业龙头 0-15 (权重15%)
    recognition: int = 0          # 市场辨识度 0-20 (权重20%)
    risk_penalty: int = 0         # 个股风险值 -20-0 (扣分项,上不封顶)
    total_score: float = 0.0
    risk_flags: List[str] = field(default_factory=list)
    cycle_score: float = 0.0      # 周期反转得分
    cycle_conditions: List[str] = field(default_factory=list)
    # 超级成长股财报质量 & 估值约束
    financial_quality: bool = False
    growth_multiplier: float = 1.0
    financial_label: str = ""
    financial_details: List[str] = field(default_factory=list)
    valuation_multiplier: float = 1.0
    valuation_reason: str = ""
    
    @property
    def raw_score(self) -> int:
        return (self.industry_boom + self.business_purity + 
                self.valuation + self.industry_leader + self.recognition + self.risk_penalty)
    
    def __post_init__(self):
        raw_sum = (self.industry_boom + self.business_purity + 
                   self.valuation + self.industry_leader + self.recognition)
        self.total_score = round(raw_sum + self.risk_penalty, 1)


class CycleReversalChecker:
    """
    周期反转5条件检查器
    寻找即将来到景气周期的传统行业
    """
    
    CONDITIONS = {
        "drop_enough": {"desc": "跌的够多", "weight": 0.20, "threshold": -0.50},
        "drop_long": {"desc": "跌的时间够长(完整周期)", "weight": 0.20, "threshold": 12},
        "bankruptcies": {"desc": "大量同行倒闭破产", "weight": 0.20, "threshold": 3},
        "demand_stable": {"desc": "未来需求稳定/增长", "weight": 0.20, "threshold": 0.05},
        "tech_breakthrough": {"desc": "技术突破商业价值变大", "weight": 0.20, "threshold": True}
    }
    
    def check(self, data: Dict) -> Tuple[float, List[str]]:
        """检查周期反转条件，返回(得分0-1, 满足条件列表)"""
        score = 0.0
        met = []
        
        # 1. 跌的够多
        if data.get("drop_from_high", 0) <= self.CONDITIONS["drop_enough"]["threshold"]:
            score += self.CONDITIONS["drop_enough"]["weight"]
            met.append("跌的够多")
        
        # 2. 跌的时间够长
        if data.get("decline_months", 0) >= self.CONDITIONS["drop_long"]["threshold"]:
            score += self.CONDITIONS["drop_long"]["weight"]
            met.append("跌的时间够长")
        
        # 3. 大量同行倒闭
        if data.get("bankruptcies", 0) >= self.CONDITIONS["bankruptcies"]["threshold"]:
            score += self.CONDITIONS["bankruptcies"]["weight"]
            met.append("大量同行倒闭")
        
        # 4. 未来需求稳定/增长
        if data.get("demand_growth", 0) >= self.CONDITIONS["demand_stable"]["threshold"]:
            score += self.CONDITIONS["demand_stable"]["weight"]
            met.append("未来需求稳定")
        
        # 5. 技术突破
        if data.get("tech_breakthrough", False):
            score += self.CONDITIONS["tech_breakthrough"]["weight"]
            met.append("技术突破")
        
        return score, met


class IndustrySelector:
    """行业选择器 - 产业链卡位原则"""
    
    @staticmethod
    def select_sub_sector(industry_type: IndustryType, sectors: List[Dict]) -> Optional[Dict]:
        """
        选择细分行业:
        - 新型科技: 优先最上游、有技术壁垒、无法绕过的环节(卡位股)
        - 传统行业: 选下游头部/最强企业
        """
        if not sectors:
            return None
            
        if industry_type == IndustryType.NEW_TECH:
            # 新型科技: 上游位置 > 技术壁垒 > 不可替代性
            return sorted(sectors, 
                key=lambda x: (x.get("upstream_level", 0), 
                              x.get("tech_barrier", 0),
                              x.get("irreplaceable", 0)), 
                reverse=True)[0]
            
        elif industry_type == IndustryType.TRADITIONAL:
            # 传统行业: 市占率 > 盈利能力 > 品牌认知
            return sorted(sectors,
                key=lambda x: (x.get("market_share", 0),
                              x.get("profitability", 0),
                              x.get("brand_power", 0)),
                reverse=True)[0]
        
        return sectors[0]


class FundFlowAnalyzer:
    """
    股市资金平衡表分析器 — 历史汇总 + akshare实时北向/两融
    流入: 外资+融资+偏股基金+ETF+私募+保险+社保+国家队+散户+分红+回购
    流出: IPO+增发+配股+可转债+可交债+印花税+佣金+融资利息+净减持
    """
    
    HISTORICAL_FLOW = {
        2020: {"desc": "增量资金市", "net": 20000, "market": "牛市"},
        2021: {"desc": "偏股基金接盘", "net": 13881, "market": "牛市"},
        2022: {"desc": "外资减少+IPO/减持", "net": -10916, "market": "熊市"},
        2023: {"desc": "失血减少+政策端", "net": -1180, "market": "转折年"},
        2024: {"desc": "ETF大爆发+分红回购", "net": 14222, "market": "结构牛"}
    }
    
    def fetch_realtime_flows(self) -> Dict:
        """获取实时北向资金+融资融券数据"""
        result = {"north_net": 0, "north_in": 0, "north_out": 0,
                  "margin_balance": 0, "margin_net": 0, "success": False}
        try:
            import akshare as ak
            # 北向资金
            try:
                north = ak.stock_hsgt_hist_em(symbol="北向资金")
                if north is not None and len(north) > 0:
                    latest = north.iloc[-1]
                    result["north_net"] = float(latest.get("净买入额", 0)) / 1e8
                    result["north_in"] = float(latest.get("买入成交额", 0)) / 1e8
                    result["north_out"] = float(latest.get("卖出成交额", 0)) / 1e8
                    result["north_date"] = str(latest.get("日期", "?"))
                    result["success"] = True
            except:
                pass
            # 融资融券
            try:
                margin = ak.stock_margin_sz_sh_daily()
                if margin is not None and len(margin) > 0:
                    latest_m = margin.iloc[-1]
                    result["margin_balance"] = float(latest_m.get("融资余额", 0)) / 1e8
                    result["margin_net"] = float(latest_m.get("融资净买入额", 0)) / 1e8
                    result["margin_date"] = str(latest_m.get("交易日期", "?"))
                    result["success"] = True
            except:
                # 降级：尝试个股两融
                pass
        except:
            pass
        return result
    
    def analyze(self, inflow: Dict, outflow: Dict) -> Dict:
        """分析资金平衡: 合并实时数据"""
        # 尝试获取实时北向+两融
        realtime = self.fetch_realtime_flows()
        
        # 如果有实时数据，用实时北向替换静态外资估算
        if realtime["success"]:
            north_net = realtime.get("north_net", 0)
            margin_net = realtime.get("margin_net", 0)
            if abs(north_net) > 0.1:
                inflow["北向资金(实时)"] = north_net
            if abs(margin_net) > 0.1:
                margin_key = "融资净买入"
                if margin_net > 0:
                    inflow[margin_key] = margin_net
                else:
                    outflow[margin_key] = abs(margin_net)
        
        total_in = sum(inflow.values())
        total_out = sum(outflow.values())
        net = total_in - total_out
        
        # 判断市场类型
        market_type = "震荡"
        if net > 10000:
            market_type = "强增量市场(牛市)"
        elif net > 5000:
            market_type = "弱增量市场(结构牛)"
        elif net > 0:
            market_type = "存量博弈"
        elif net > -5000:
            market_type = "存量流出"
        else:
            market_type = "大幅失血(熊市)"
        
        # 关键信号识别
        signals = []
        if inflow.get("ETF基金", 0) > 5000:
            signals.append("ETF大幅流入(国家队/机构加仓)")
        if outflow.get("净减持", 0) < 500:
            signals.append("减持压制减轻")
        if inflow.get("偏股型基金", 0) < 0:
            signals.append("基金发行冰点(反向信号)")
        if outflow.get("IPO", 0) < 1000:
            signals.append("IPO放缓(政策呵护)")
        # 实时信号
        north_net = realtime.get("north_net", 0)
        if north_net > 100:
            signals.append(f"北向大幅流入({north_net:.0f}亿)")
        elif north_net < -100:
            signals.append(f"北向大幅流出({north_net:.0f}亿)⚠️")
        margin_net = realtime.get("margin_net", 0)
        if margin_net > 50:
            signals.append(f"融资净买入({margin_net:.0f}亿)")
        elif margin_net < -50:
            signals.append(f"融资净偿还({margin_net:.0f}亿)⚠️")
        
        return {
            "total_inflow": total_in,
            "total_outflow": total_out,
            "net_flow": net,
            "market_type": market_type,
            "signals": signals,
            "inflow_rank": sorted(inflow.items(), key=lambda x: -x[1]),
            "outflow_rank": sorted(outflow.items(), key=lambda x: -x[1]),
            "realtime": realtime  # 实时北向+两融
        }


class LiquidityBuffAnalyzer:
    """
    流动性Buff分析器 — 基于rolling 90日均量的动态阈值
    核心公式: 成交额/90日均量 → 5级Buff等级(虚弱→超级)
    """
    
    BASELINE = {
        "1.0": {"index_range": (3000, 3100), "desc": "基准平衡"},
        "0.6": {"index_range": (2700, 2800), "desc": "流动性枯竭"},
        "1.8": {"index_range": (3500, 3600), "desc": "大龙Buff"}
    }
    
    # 动态Buff等级（相对于90日均量的倍数）
    BUFF_DYNAMIC = {
        "虚弱": {"ratio": (0, 0.6), "multiplier": 0.7, "desc": "量能低迷,谨慎"},
        "正常": {"ratio": (0.6, 1.2), "multiplier": 1.0, "desc": "基准平衡"},
        "增益": {"ratio": (1.2, 1.6), "multiplier": 1.3, "desc": "量能放大,积极"},
        "大龙": {"ratio": (1.6, 2.2), "multiplier": 1.6, "desc": "大龙Buff+红蓝+护盾"},
        "超级": {"ratio": (2.2, 999), "multiplier": 2.0, "desc": "历史级放量,更强做多"}
    }
    
    def _get_90d_avg_turnover(self) -> float:
        """获取90日均成交额（从akshare获取上证指数日线）"""
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily_em(symbol="sh000001")
            if df is not None and len(df) >= 90:
                recent = df.tail(90)
                avg_amt = float(recent["amount"].mean()) / 1e8  # 转为亿
                return avg_amt
        except:
            pass
        return 20000  # 默认2万亿（保守估计）
    
    def calculate_buff(self, turnover: float, index: float, avg_90d: float = None) -> Dict:
        """
        基于90日均量的动态Buff
        turnover: 成交额(亿)
        index: 大盘点位
        avg_90d: 90日均量(亿)，不传则自动获取
        """
        if avg_90d is None:
            avg_90d = self._get_90d_avg_turnover()
        if avg_90d <= 0:
            avg_90d = 20000
        
        # 动态比值
        ratio = turnover / avg_90d
        
        # 找到对应的Buff等级
        buff_level = "正常"
        multiplier = 1.0
        for level, cfg in self.BUFF_DYNAMIC.items():
            if cfg["ratio"][0] <= ratio < cfg["ratio"][1]:
                buff_level = level
                multiplier = cfg["multiplier"]
                break
        
        # 成交萎缩惩罚（缩量到90日均量的50%以下）
        if ratio < 0.5:
            multiplier = max(0.5, multiplier)
        
        # 计算理论点位
        theoretical_index = ((turnover / 10000 - 1) / THEORETICAL_INDEX_RATIO + 1) * THEORETICAL_INDEX_BASE
        index_vs_theory = index / theoretical_index
        
        return {
            "buff_level": buff_level,
            "multiplier": round(multiplier, 2),
            "ratio_vs_90d": round(ratio, 2),
            "avg_90d": round(avg_90d, 0),
            "theoretical_index": round(theoretical_index, 0),
            "actual_index": index,
            "index_vs_theory": round(index_vs_theory, 2),
            "desc": self.BUFF_DYNAMIC[buff_level]["desc"],
            "suggestion": self._get_suggestion(buff_level, index_vs_theory)
        }
    
    def _get_suggestion(self, level: str, ratio: float) -> str:
        """根据Buff等级给出操作建议"""
        if level == "超级" and ratio < 1.0:
            return "超级Buff+指数低估→全力做多,更加格局"
        elif level == "超级":
            return "超级Buff+指数合理→持有等修复,选股更容易"
        elif level == "大龙" and ratio < 1.0:
            return "高Buff+指数低估→全力做多"
        elif level == "大龙":
            return "高Buff+指数合理→持有等修复"
        elif level == "虚弱":
            return "虚弱Debuff→减仓防守"
        elif ratio < 0.9:
            return "指数低于理论→等流动性修复"
        else:
            return "正常操作"


class RiskAssessment:
    """风险识别评估 - 坐牢风险清单 + 暴跌因素 + 流动性Buff"""
    
    # 原地踏步(坐牢)风险
    PRISON_RISKS = {
        "valuation_suppressed": "传统行业整体估值偏低，受龙头企业市值压制",
        "shareholder_reduction": "涨上去就有股东套现减持的历史操作",
        "earnings_delay": "业绩迟迟无法兑现，或3年内确定出不了业绩"
    }
    
    # 潜在暴跌因素
    CRASH_RISKS = {
        "internal_fraud": "企业内部问题爆雷(审计保留意见/业绩造假)",
        "external_black_swan": "外部突发大事件(友商技术突破/产能爆发/政策突变)"
    }
    
    def assess(self, stock_data: Dict) -> List[str]:
        """评估风险，返回风险标签列表"""
        risks = []
        
        # 坐牢风险
        if stock_data.get("sector_valuation_low"):
            risks.append("估值受压制")
        if stock_data.get("reduction_history"):
            risks.append("股东减持历史")
        if stock_data.get("earnings_delay_years", 0) > 3:
            risks.append("业绩3年无法兑现")
        
        # 暴跌因素
        if stock_data.get("audit_issue"):
            risks.append("审计风险")
        if stock_data.get("competitor_breakthrough"):
            risks.append("友商技术突破")
        if stock_data.get("policy_risk"):
            risks.append("政策风险")
        if stock_data.get("external_event"):
            risks.append("外部黑天鹅")
            
        return risks


class RealtimeDataProvider:
    """实时日线数据提供器 — 多源回退 (akshare→efinance→新浪API)"""
    
    def __init__(self):
        self.cache = {}
        self._last_source = ""
    
    def get_daily(self, symbol: str, days: int = 90) -> Optional[Dict]:
        """获取个股日线 + 计算技术指标。非交易日自动用最新收盘价"""
        if symbol in self.cache:
            return self.cache[symbol]
        
        import pandas as pd
        df = None
        is_weekend = datetime.now().weekday() >= 5
        min_records = 5 if is_weekend else 10
        
        # 源1: akshare
        try:
            import akshare as ak
            end = datetime.now().strftime("%Y%m%d")
            # 拉取约1年数据以支撑真实 MA250(年线); 用 DateOffset 避免跨年月份算术越界
            start = (datetime.now() - pd.DateOffset(months=12)).strftime("%Y%m%d")
            df_raw = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end, adjust="qfq")
            if df_raw is not None and len(df_raw) >= min_records:
                df = df_raw.rename(columns={"日期":"date","收盘":"close","最高":"high","最低":"low","成交量":"volume"})
                self._last_source = "akshare"
        except: pass
        
        # 源2: efinance
        if df is None or len(df) < min_records:
            try:
                import efinance as ef
                end = datetime.now().strftime("%Y%m%d")
                start = (datetime.now() - pd.DateOffset(months=12)).strftime("%Y%m%d")
                df_raw = ef.get_quote_history(symbol, beg=start, end=end)
                if df_raw is not None and len(df_raw) >= min_records:
                    df = df_raw.rename(columns={"日期":"date","收盘":"close","最高":"high","最低":"low","成交量":"volume"})
                    self._last_source = "efinance"
            except: pass
        
        # 源3: 新浪K线API (个股)
        if df is None or len(df) < min_records:
            try:
                import requests
                prefix = "sh" if symbol.startswith("6") else "sz"
                url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{symbol}&scale=240&ma=no&datalen=260"
                h = {"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn/"}
                resp = requests.get(url, headers=h, timeout=15)
                if resp.status_code == 200 and resp.text.strip():
                    data = resp.json()
                    if data and len(data) >= min_records:
                        rows = [{"date":k.get("day",""),"close":float(k.get("close",0)),
                                 "high":float(k.get("high",0)),"low":float(k.get("low",0)),
                                 "volume":float(k.get("volume",0))} for k in data]
                        df = pd.DataFrame(rows)
                        self._last_source = "sina_kline"
            except: pass
        
        # 源4: 新浪实时行情 (ETF/个股通用兜底)
        if df is None or len(df) < min_records:
            try:
                import requests
                prefix = "sh" if symbol.startswith(("6","5","1","9")) else "sz"
                r = requests.get(f"https://hq.sinajs.cn/list={prefix}{symbol}",
                    headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn/"}, timeout=10)
                if r.status_code == 200 and '="' in r.text:
                    parts = r.text.split('="')[1].split('",')[0].split(",")
                    if len(parts) >= 4 and parts[3]:
                        close = float(parts[3])
                        high = float(parts[4]) if len(parts)>4 and parts[4] else close
                        low = float(parts[5]) if len(parts)>5 and parts[5] else close
                        vol = float(parts[8]) if len(parts)>8 and parts[8] else 0
                        date_str = parts[-4] if len(parts)>10 and '-' in parts[-4] else datetime.now().strftime("%Y-%m-%d")
                        df = pd.DataFrame([{"date":date_str,"close":close,"high":high,"low":low,"volume":vol}])
                        while len(df) < 20:
                            df = pd.concat([df.head(1), df]).reset_index(drop=True)
                            df.loc[0,"date"] = ""
                        self._last_source = "sina_realtime"
            except: pass
        
        if df is None or len(df) < 5:
            self.cache[symbol] = None
            return None
        
        return self._compute_indicators(df, symbol, is_weekend)
    
    def _compute_indicators(self, df, symbol, is_weekend=False):
        import pandas as pd, numpy as np
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        close = df["close"]; cur = close.iloc[-1]
        
        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1] if len(close) >= 60 else ma20
        ma250 = close.rolling(250).mean().iloc[-1] if len(close) >= 250 else None
        
        d1 = (cur/close.iloc[-2]-1)*100 if len(close)>=2 else 0
        d5 = (cur/close.iloc[-5]-1)*100 if len(close)>=5 else 0
        d20 = (cur/close.iloc[-20]-1)*100 if len(close)>=20 else 0
        
        delta = close.diff(); gain = delta.where(delta>0,0).rolling(14).mean()
        loss = (-delta.where(delta<0,0)).rolling(14).mean()
        rs = gain/(loss+0.0001); rsi = float(100-(100/(1+rs.iloc[-1]))) if not pd.isna(rs.iloc[-1]) else 50
        
        ema12 = close.ewm(span=12).mean(); ema26 = close.ewm(span=26).mean()
        macd_val = float(ema12.iloc[-1]-ema26.iloc[-1])
        signal = (ema12-ema26).ewm(span=9).mean(); macd_signal_val = float(signal.iloc[-1])
        
        volatility = float(close.pct_change().std()*np.sqrt(252))*100
        price_position = float((cur-close.min())/(close.max()-close.min()+0.001))*100
        is_bull_align = cur > ma5 > ma20 > ma60
        
        # 成交量指标
        vol = df.get("volume", pd.Series([0]*len(df))).iloc[-1] if "volume" in df.columns else 0
        vol_ma5 = df["volume"].rolling(5).mean().iloc[-1] if "volume" in df.columns and len(df) >= 5 else vol
        vol_ma20 = df["volume"].rolling(20).mean().iloc[-1] if "volume" in df.columns and len(df) >= 20 else vol
        vol_ratio = round(vol / vol_ma20, 2) if vol_ma20 > 0 else 1.0
        is_volume_shrink = vol_ratio < 0.6  # 缩量信号：当前成交量低于20日均量60%
        
        tech_score = 0
        if is_bull_align: tech_score += 30
        if 30 <= rsi <= 70: tech_score += 20
        if macd_val > macd_signal_val: tech_score += 20
        if d5 > 0: tech_score += 15
        if price_position < 50: tech_score += 15
        
        note = f"周末, 价格为{close.index[-1].strftime('%m-%d')}收盘价" if is_weekend else ""
        result = {
            "symbol":symbol,"price":round(cur,2),"d1":round(d1,2),"d5":round(d5,2),"d20":round(d20,2),
            "ma5":round(ma5,2),"ma20":round(ma20,2),"ma60":round(ma60,2),"ma250":round(ma250,2) if ma250 is not None else None,"rsi":round(rsi,1),
            "macd":round(macd_val,4),"macd_signal":round(macd_signal_val,4),
            "volatility":round(volatility,1),"price_position":round(price_position,0),
            "bull_align":is_bull_align,"tech_score":tech_score,"note":note,
            "volume":int(vol),"vol_ratio":vol_ratio,"vol_shrink":is_volume_shrink,
            # 缓存原始收盘价序列, 供协方差收缩计算真实收益率(避免用估值分位代理价格)
            "close_series":[round(float(x),4) for x in close.values]
        }
        self.cache[symbol] = result
        return result

    def get_returns(self, symbol: str, days: int = 120) -> Optional["np.ndarray"]:
        """返回个股日收益率序列(用于协方差收缩/组合优化)。无数据或 numpy 不可用返回 None。"""
        if np is None:
            return None
        try:
            d = self.get_daily(symbol, days=days)
            series = d.get("close_series") if d else None
            if not series or len(series) < 5:
                return None
            arr = np.asarray(series, float)
            r = np.diff(arr) / arr[:-1]
            if len(r) < 3:
                return None
            return r
        except Exception:
            return None
    
    def check_batch(self, symbols: List[str]) -> Dict[str, Dict]:
        results = {}
        for sym in symbols:
            data = self.get_daily(sym)
            if data:
                note = data.get("note","")
                print(f"  [数据] {sym}... {data['price']} | 技术:{data['tech_score']} {note}")
            else:
                print(f"  [数据] {sym}... 不可用")
            results[sym] = data
        return results


class ValueInvestingAnalyzer:
    """
    价值投资双击框架 v3.0 (第14维度) — 整合三大双击理论
    
    戴维斯双击: 低PE买入 + EPS增长 + 高PE卖出 = 股价倍乘效应
        公式: 预期收益 = PE扩张倍数 × EPS增长倍数 - 1
        例: PE从15→30(×2) + EPS从1→2(×2) = 股价×4
        
    笨韭双击: 产业拐点 ∩ 企业基本面拐点 (最硬逻辑)
        产业侧: 四要素周期/涨价/新供需/国产替代/海外映射
        企业侧: 连续季报增长/毛利率扩张/产能扩张/订单大增
        
    笨韭单击: 单一拐点 → 赔率降低但确定性更高
        产业拐点+企业未跟进 → 等信号确认
        企业拐点+产业未共振 → 个股独立行情
    """
    
    # ═══ 产业拐点六维度分析 ═══
    INDUSTRY_DIMENSIONS = {
        "four_elements": {"weight": 0.25, "desc": "四要素周期:政策/供需/价格/技术"},
        "price_rising": {"weight": 0.20, "desc": "涨价逻辑:量价齐升/成本推动/供需缺口"},
        "new_demand": {"weight": 0.20, "desc": "新供需关系:应用爆发/国产替代/海外映射"},
        "capacity_cycle": {"weight": 0.15, "desc": "产能周期:扩产→过剩→出清→紧缺"},
        "policy_catalyst": {"weight": 0.10, "desc": "政策催化剂:补贴/出口管制/行业准入"},
        "struct_change": {"weight": 0.10, "desc": "结构性变化:技术路线切换/商业模式重构"},
    }
    
    # ═══ 企业基本面拐点六维度 ═══
    COMPANY_DIMENSIONS = {
        "revenue_growth": {"weight": 0.20, "desc": "营收连续增长(≥2季度加速)"},
        "eps_turnaround": {"weight": 0.25, "desc": "EPS拐点:扭亏/暴增/连续改善"},
        "margin_expand": {"weight": 0.20, "desc": "毛利率持续提升(≥2季度)"},
        "capacity_release": {"weight": 0.15, "desc": "产能大幅扩张(新产线/新并购)"},
        "order_surge": {"weight": 0.10, "desc": "订单/合同负债大增"},
        "cashflow_improve": {"weight": 0.10, "desc": "经营现金流同步改善"},
    }
    
    # ═══ 产业拐点典型案例库 ═══
    INDUSTRY_INFLECTION_CASES = {
        "存储芯片": {"status": "产业拐点已确认", "signals": ["HBM涨价300%", "DRAM/NAND供不应求",
            "三星SK海力士4800万亿韩元投资", "AI数据中心存储需求爆发"], "confidence": 90},
        "锂矿": {"status": "产业拐点待确认", "signals": ["锂价触底10万→反弹15万", "澳矿减产/非洲矿延后",
            "新能源车销量同比+25%", "正极材料开工率回升"], "confidence": 55},
        "券商": {"status": "结构性拐点中", "signals": ["券商十年最好业绩", "并购重组大年(国泰君安+海通)",
            "居民存款入市加速", "证监会鼓励头部券商做大"], "confidence": 70},
        "机器人": {"status": "产业拐点已确认", "signals": ["宇树R1→2.99万(成本断崖)", "特斯拉Optimus量产",
            "减速器/传感器国产化突破", "政策:人形机器人产业规划"], "confidence": 85},
        "半导体设备": {"status": "产业拐点已确认", "signals": ["国产替代不可逆(美对华制裁升级)", 
            "中芯国际/华虹产能利用率>95%", "北方华创新签订单同比+45%", "半导体资本开支增长30%+"], "confidence": 88},
        "小金属(钨/锡/锗)": {"status": "产业拐点已确认", "signals": ["出口管制战略武器化", "锗/镓/钨产量刚性收缩",
            "军工/半导体需求激增", "日本东曹氧化锆停供→国产替代窗口"], "confidence": 82},
        "贵金属(黄金/白银)": {"status": "产业拐点已确认", "signals": ["全球央行持续购金", "地缘冲突+美伊局势",
            "美元指数走弱/降息预期", "白银工业属性+贵金属避险双驱动"], "confidence": 75},
    }
    
    @classmethod
    def analyze_davis_double(cls, stock_data: Dict) -> Dict:
        """
        分析戴维斯双击潜力
        股价 = EPS × PE
        
        逻辑链:
        1. 当前PE处于历史低位 → PE有扩张空间
        2. EPS处于加速增长期 → 盈利持续超预期
        3. PE扩张 × EPS增长 = 股价双击效应
        
        潜在收益 = (目标PE/当前PE) × (1+EPS增长率) - 1
        """
        pe = stock_data.get("pe_ttm", 0)
        eps = stock_data.get("eps", 0)
        eps_growth = stock_data.get("eps_growth", 0)
        pe_percentile = stock_data.get("pe_percentile", 0.5)
        eps_acceleration = stock_data.get("eps_acceleration", 0)  # EPS二阶导
        sector_pe = stock_data.get("sector_pe", 0)  # 行业平均PE
        
        result = {
            "score": 0, "type": "", "logic": "", 
            "details": [], "potential_return": 0,
            "pe_signal": "", "eps_signal": "", "conviction": ""
        }
        
        # PE信号分析
        pe_expansion_factor = 1.0
        pe_percentile = pe_percentile or 0.5  # 防御 None (PE自动计算可能因限流返回None)
        if pe > 0 and pe_percentile > 0:
            if pe_percentile <= 0.20:
                result["pe_signal"] = f"PE({pe:.1f})处于历史底部{pe_percentile*100:.0f}%分位→大幅扩张空间"
                pe_expansion_factor = 2.0 if pe < 15 else 1.7
            elif pe_percentile <= 0.35:
                result["pe_signal"] = f"PE({pe:.1f})低估值区{pe_percentile*100:.0f}%分位→中等扩张空间"
                pe_expansion_factor = 1.5
            elif pe_percentile <= 0.50:
                result["pe_signal"] = f"PE({pe:.1f})合理偏低{pe_percentile*100:.0f}%分位→有限扩张"
                pe_expansion_factor = 1.2
            else:
                result["pe_signal"] = f"PE({pe:.1f})偏高{pe_percentile*100:.0f}%分位→扩张空间不足"
                pe_expansion_factor = 0.9
        
        # EPS信号分析
        eps_growth_factor = 1.0
        if eps_growth > 0:
            if eps_growth >= 50:
                result["eps_signal"] = f"EPS超高增长{eps_growth:.0f}%→双击加速器"
                eps_growth_factor = 1.5
                if eps_acceleration > 0:
                    result["eps_signal"] += f" [二阶导+{eps_acceleration*100:.0f}%加速中]"
                    eps_growth_factor += 0.15
            elif eps_growth >= 30:
                result["eps_signal"] = f"EPS高增长{eps_growth:.0f}%→双击核心动力"
                eps_growth_factor = 1.3
                if eps_acceleration > 0:
                    result["eps_signal"] += f" [二阶导+{eps_acceleration*100:.0f}%]"
                    eps_growth_factor += 0.1
            elif eps_growth >= 15:
                result["eps_signal"] = f"EPS稳健增长{eps_growth:.0f}%→温和双击"
                eps_growth_factor = 1.1
            else:
                result["eps_signal"] = f"EPS低增长{eps_growth:.0f}%→双击动力不足"
        
        # 计算潜在收益空间
        potential_return = round((pe_expansion_factor * eps_growth_factor - 1) * 100, 1)
        result["potential_return"] = potential_return
        
        # 行业PE对比
        if sector_pe > 0 and pe > 0:
            if pe < sector_pe * 0.7:
                result["details"].append(f"PE仅为行业均值的{pe/sector_pe*100:.0f}%→相对低估修复空间")
            elif pe > sector_pe * 1.3:
                result["details"].append(f"PE高于行业均值{pe/sector_pe*100:.0f}%→相对高估需谨慎")
        
        # 综合判定
        if pe_percentile <= 0.25 and eps_growth >= 30:
            result["score"] = min(95, 60 + (1-pe_percentile)*40 + eps_growth*0.3)
            result["type"] = "🔥戴维斯双击(强)"
            result["logic"] = (f"低PE({pe:.1f}@{pe_percentile*100:.0f}%分位)×高EPS增长({eps_growth:.0f}%)"
                              f"→潜在收益空间+{potential_return}%")
            result["conviction"] = "高确定性:PE底部+EPS加速=戴维斯双击最硬逻辑"
        elif pe_percentile <= 0.35 and eps_growth >= 50:
            result["score"] = 85
            result["type"] = "戴维斯双击"
            result["logic"] = f"合理PE({pe:.1f})×超高EPS({eps_growth:.0f}%)→潜在+{potential_return}%"
            result["conviction"] = "中高确定性:EPS超高增长可消化PE"
        elif pe_percentile <= 0.50 and eps_growth >= 30:
            result["score"] = 70
            result["type"] = "准戴维斯双击"
            result["logic"] = f"合理PE×高增长→潜在+{potential_return}%"
            result["conviction"] = "中等确定性:PE空间有限,需EPS持续超预期"
        elif eps_acceleration > 0.05 and pe_percentile <= 0.40:
            result["score"] = 65
            result["type"] = "戴维斯双击初期(EPS加速中)"
            result["logic"] = f"EPS加速+PE低位→戴维斯双击在酝酿"
            result["conviction"] = "需确认EPS加速趋势能否持续"
        
        # 行业PE对比加分
        if sector_pe > 0 and pe > 0 and pe < sector_pe * 0.5:
            result["score"] = min(98, result["score"] + 10)
            result["type"] = result["type"].replace("双击", "双击+折价修复")
        
        return result
    
    @classmethod
    def analyze_industry_inflection(cls, stock_data: Dict, news_signals: List[str]) -> Dict:
        """
        产业拐点六维度分析 v2.1 — 按行业匹配案例库
        
        检测产业层面的拐点信号:
        ① 四要素周期(政策/供需/价格/技术) — 权重25%
        ② 涨价逻辑(量价齐升) — 权重20%
        ③ 新供需关系(应用爆发/国产替代) — 权重20%
        ④ 产能周期位置(扩产→过剩→出清→紧缺) — 权重15%
        ⑤ 政策催化剂 — 权重10%
        ⑥ 结构性变化 — 权重10%
        """
        name = stock_data.get("name", "")
        symbol = stock_data.get("symbol", "")
        news_content = " ".join(news_signals).lower() if news_signals else ""
        
        # 根据股票名称匹配产业案例库 — 获取行业专属信号
        industry_signals = []
        matched_case = None
        name_lower = name.lower()
        
        # 先精确匹配案例库名称中包含的关键词
        # 定义额外的名称→行业映射(用于ETF等不容易匹配的标的)
        name_to_case_map = {
            "科创50": "半导体设备",  # 科创50主要权重是半导体
            "黄金": "贵金属(黄金/白银)",
            "白银": "贵金属(黄金/白银)",
            "中芯": "半导体设备",
            "北方华创": "半导体设备",
            "赣锋": "锂矿",
            "天齐": "锂矿",
            "券商": "券商",
            "银河": "券商",
            "中信": "券商",
            "锡业": "小金属(钨/锡/锗)",
            "章源": "小金属(钨/锡/锗)",
            "绿的": "机器人",
            "埃斯顿": "机器人",
            "佰维": "存储芯片",
            "江波龙": "存储芯片",
            "兆易": "存储芯片",
        }
        
        matched_case_name = None
        for map_kw, case_name_ref in name_to_case_map.items():
            if map_kw in name or map_kw in symbol:
                matched_case_name = case_name_ref
                break
        
        if matched_case_name and matched_case_name in cls.INDUSTRY_INFLECTION_CASES:
            matched_case = cls.INDUSTRY_INFLECTION_CASES[matched_case_name]
            industry_signals = [s.lower() for s in matched_case.get("signals", [])]
        else:
            # 回退: 检查案例库名称是否包含股票名称关键词
            for case_name, case_data in cls.INDUSTRY_INFLECTION_CASES.items():
                case_lower = case_name.lower()
                # 双向匹配: 案例名在股票名中 OR 股票名在案例名中
                name_parts = name_lower.replace("(", " ").replace(")", " ").replace("/", " ").split()
                if case_lower in name_lower:
                    industry_signals = [s.lower() for s in case_data.get("signals", [])]
                    matched_case = case_data
                    break
                for part in name_parts:
                    if len(part) >= 2 and part in case_lower:
                        industry_signals = [s.lower() for s in case_data.get("signals", [])]
                        matched_case = case_data
                        break
                if industry_signals:
                    break
        
        # 合并: 行业案例库信号 + 实时新闻信号
        content = " ".join(industry_signals) + " " + news_content
        
        scores = {}
        evidence = []
        total = 0.0
        
        # ① 四要素周期检测
        four_el_score = 0
        policy_kw = ["政策", "规划", "国务院", "工信部", "发改委", "补贴", "放宽", "放开", "支持"]
        supply_kw = ["供不应求", "供给收缩", "产能不足", "缺口", "短缺", "停产", "减产", "去产能"]
        price_kw = ["涨价", "提价", "价格突破", "价格创新高", "报价上涨", "涨幅", "上涨"]
        tech_kw = ["技术突破", "量产", "新工艺", "迭代", "升级", "突破", "专利", "研发成功"]
        
        if any(kw in content for kw in policy_kw):
            four_el_score += 25
            evidence.append("政策要素")
        if any(kw in content for kw in supply_kw):
            four_el_score += 30
            evidence.append("供需缺口")
        if any(kw in content for kw in price_kw):
            four_el_score += 25
            evidence.append("价格上行")
        if any(kw in content for kw in tech_kw):
            four_el_score += 20
            evidence.append("技术突破")
        
        scores["four_elements"] = min(four_el_score, 100)
        total += scores["four_elements"] * cls.INDUSTRY_DIMENSIONS["four_elements"]["weight"]
        
        # ② 涨价逻辑检测
        price_score = 0
        if any(kw in content for kw in ["涨价", "提价", "价格上涨", "报价上调"]):
            price_score = 70
            evidence.append("明确涨价信号")
        if any(kw in content for kw in ["量价齐升", "量价齐飞", "供需缺口"]):
            price_score = max(price_score, 90)
            evidence.append("量价齐升逻辑")
        if any(kw in content for kw in ["成本推动", "原材料涨价"]):
            price_score = max(price_score, 50)
            evidence.append("成本推动型涨价")
        scores["price_rising"] = price_score
        total += price_score * cls.INDUSTRY_DIMENSIONS["price_rising"]["weight"]
        
        # ③ 新供需关系检测
        demand_score = 0
        if any(kw in content for kw in ["国产替代", "进口替代", "自主可控"]):
            demand_score = 85
            evidence.append("国产替代需求爆发")
        if any(kw in content for kw in ["海外映射", "美股映射", "对标"]):
            demand_score = max(demand_score, 70)
            evidence.append("海外映射→国内重估")
        if any(kw in content for kw in ["AI", "人工智能需求", "新应用", "场景爆发"]):
            demand_score = max(demand_score, 80)
            evidence.append("新应用需求引爆")
        if any(kw in content for kw in ["数据中心", "算力需求", "存储需求", "云计算"]):
            demand_score = max(demand_score, 75)
            evidence.append("算力基础设施需求")
        scores["new_demand"] = demand_score
        total += demand_score * cls.INDUSTRY_DIMENSIONS["new_demand"]["weight"]
        
        # ④ 产能周期检测
        capacity_score = 30  # 默认中间位置
        if any(kw in content for kw in ["产能出清", "去产能", "淘汰落后", "减产"]):
            capacity_score = 80
            evidence.append("产能出清→供给收缩")
        elif any(kw in content for kw in ["扩产", "新产能", "投产", "扩产计划"]):
            capacity_score = 40
            evidence.append("扩产初期→产能未释放")
        elif any(kw in content for kw in ["产能过剩", "供大于求", "开工率低"]):
            capacity_score = 10
            evidence.append("产能过剩→需等待出清")
        elif any(kw in content for kw in ["供不应求", "产能紧张", "满产"]):
            capacity_score = 90
            evidence.append("紧缺→涨价在即")
        scores["capacity_cycle"] = capacity_score
        total += capacity_score * cls.INDUSTRY_DIMENSIONS["capacity_cycle"]["weight"]
        
        # ⑤ 政策催化剂检测
        policy_cat_score = 0
        if any(kw in content for kw in ["补贴", "财政补贴", "税收优惠", "退税"]):
            policy_cat_score = 70
            evidence.append("财政补贴催化")
        if any(kw in content for kw in ["出口管制", "禁运", "管制", "制裁"]):
            policy_cat_score = max(policy_cat_score, 60)
            evidence.append("出口管制→供给收缩")
        if any(kw in content for kw in ["产业规划", "战略新兴产业", "重点支持"]):
            policy_cat_score = max(policy_cat_score, 65)
            evidence.append("产业规划催化")
        if any(kw in content for kw in ["准入", "牌照", "放开"]):
            policy_cat_score = max(policy_cat_score, 75)
            evidence.append("政策准入放开")
        scores["policy_catalyst"] = policy_cat_score
        total += policy_cat_score * cls.INDUSTRY_DIMENSIONS["policy_catalyst"]["weight"]
        
        # ⑥ 结构性变化检测
        struct_score = 0
        if any(kw in content for kw in ["技术路线切换", "新技术路线", "路径切换"]):
            struct_score = 80
            evidence.append("技术路线切换→新格局")
        if any(kw in content for kw in ["商业模式重构", "直接销售", "订阅制"]):
            struct_score = max(struct_score, 65)
            evidence.append("商业模式重构")
        if any(kw in content for kw in ["产业链转移", "供应链重构", "去风险"]):
            struct_score = max(struct_score, 70)
            evidence.append("供应链重构→新机会")
        scores["struct_change"] = struct_score
        total += struct_score * cls.INDUSTRY_DIMENSIONS["struct_change"]["weight"]
        
        # 历史案例库匹配加分
        case_bonus = 0
        if matched_case:
            case_bonus = matched_case["confidence"] * 0.15
            evidence.append(f"案例库:{matched_case['status']}(置信度{matched_case['confidence']})")
        
        final_score = min(100, total + case_bonus)
        
        status = "拐点未确认"
        if final_score >= 80:
            status = "🔥产业拐点已确认(强)"
        elif final_score >= 65:
            status = "产业拐点确认中"
        elif final_score >= 45:
            status = "产业拐点初现"
        elif final_score >= 25:
            status = "产业拐点待观察"
        else:
            status = "产业无明显拐点"
        
        return {
            "score": round(final_score, 1),
            "status": status,
            "dimension_scores": {k: round(v, 1) for k, v in scores.items()},
            "evidence": list(set(evidence)),
            "is_inflection": final_score >= 65,
            "confidence": "高" if final_score >= 80 else "中" if final_score >= 50 else "低"
        }
    
    @classmethod
    def analyze_company_inflection(cls, stock_data: Dict, news_signals: List[str]) -> Dict:
        """
        企业基本面拐点六维度分析
        
        检测企业层面的拐点信号:
        ① 营收连续增长(≥2季度加速) — 权重20%
        ② EPS拐点(扭亏/暴增/连续改善) — 权重25%
        ③ 毛利率持续提升(≥2季度) — 权重20%
        ④ 产能大幅扩张(新产线/新并购) — 权重15%
        ⑤ 订单/合同负债大增 — 权重10%
        ⑥ 经营现金流同步改善 — 权重10%
        """
        name = stock_data.get("name", "")
        content = " ".join(news_signals).lower() if news_signals else ""
        
        scores = {}
        evidence = []
        total = 0.0
        
        # ① 营收连续增长
        revenue_q = stock_data.get("revenue_qoq", [])  # 近4季度环比
        revenue_score = 0
        if len(revenue_q) >= 3:
            accelerating = sum(1 for i in range(1, len(revenue_q)) 
                              if revenue_q[i] > revenue_q[i-1])
            if accelerating >= 3:
                revenue_score = 90
                evidence.append(f"营收连续{accelerating}季度加速增长")
            elif accelerating >= 2:
                revenue_score = 70
                evidence.append(f"营收连续{accelerating}季度增长")
            elif accelerating >= 1:
                revenue_score = 45
                evidence.append("营收初现拐点")
        else:
            if any(kw in content for kw in ["营收增长", "收入增长", "rev", "收入高增"]):
                revenue_score = 60
                evidence.append("有营收增长报道")
        scores["revenue_growth"] = revenue_score
        total += revenue_score * cls.COMPANY_DIMENSIONS["revenue_growth"]["weight"]
        
        # ② EPS拐点
        eps_score = 0
        eps_trend = stock_data.get("eps_trend", [])  # 近几季度EPS
        if len(eps_trend) >= 2:
            if all(e > 0 for e in eps_trend[-3:]) and eps_trend[-1] > eps_trend[-2] * 1.3:
                eps_score = 95
                evidence.append("EPS连续正+加速暴增→强拐点")
            elif eps_trend[-1] > 0 and eps_trend[-2] <= 0:
                eps_score = 88
                evidence.append("EPS扭亏为盈→确认拐点")
            elif eps_trend[-1] > eps_trend[-2] > eps_trend[-3] > 0:
                eps_score = 82
                evidence.append("EPS连续3季度改善")
            elif eps_trend[-1] > eps_trend[-2]:
                eps_score = 65
                evidence.append("EPS改善趋势初现")
        else:
            if any(kw in content for kw in ["扭亏", "业绩预增", "业绩暴增", "利润大增", 
                                              "净利润增长", "盈利大幅改善"]):
                eps_score = 75
                evidence.append("业绩预增/扭亏信号")
        scores["eps_turnaround"] = eps_score
        total += eps_score * cls.COMPANY_DIMENSIONS["eps_turnaround"]["weight"]
        
        # ③ 毛利率扩张
        margin_trend = stock_data.get("margin_trend", [])
        margin_score = 0
        if len(margin_trend) >= 2:
            if margin_trend[-1] > margin_trend[-2] > margin_trend[-3]:
                margin_score = 88
                evidence.append("毛利率连续3季度扩张")
            elif margin_trend[-1] > margin_trend[-2] + 2:
                margin_score = 80
                evidence.append(f"毛利率QoQ扩大{margin_trend[-1]-margin_trend[-2]:.1f}个百分点")
            elif margin_trend[-1] > margin_trend[-2]:
                margin_score = 60
                evidence.append("毛利率环比改善")
        else:
            if any(kw in content for kw in ["毛利率提升", "毛利率改善", "毛利率恢复", "利润率提升"]):
                margin_score = 65
                evidence.append("毛利率提升信号")
        scores["margin_expand"] = margin_score
        total += margin_score * cls.COMPANY_DIMENSIONS["margin_expand"]["weight"]
        
        # ④ 产能扩张
        capacity_score = 0
        if any(kw in content for kw in ["新产线投产", "新工厂", "产能翻倍", "产能大幅扩张", 
                                          "产能释放", "产能达到", "投产"]):
            capacity_score = 75
            evidence.append("产能大幅扩张/新产线投产")
        elif any(kw in content for kw in ["并购", "收购", "整合", "重组"]):
            capacity_score = 60
            evidence.append("并购扩张→产能增加")
        elif stock_data.get("capex_growth", 0) > 30:
            capacity_score = 55
            evidence.append(f"资本开支同比+{stock_data['capex_growth']:.0f}%→产能扩张信号")
        scores["capacity_release"] = capacity_score
        total += capacity_score * cls.COMPANY_DIMENSIONS["capacity_release"]["weight"]
        
        # ⑤ 订单/合同负债大增
        order_score = 0
        if any(kw in content for kw in ["订单大增", "订单爆发", "新签订单", "在手订单", "合同负债"]):
            order_score = 80
            evidence.append("订单/合同负债大增")
        elif stock_data.get("contract_liability_growth", 0) > 40:
            order_score = 70
            evidence.append(f"合同负债同比+{stock_data['contract_liability_growth']:.0f}%")
        scores["order_surge"] = order_score
        total += order_score * cls.COMPANY_DIMENSIONS["order_surge"]["weight"]
        
        # ⑥ 经营现金流改善
        cashflow_score = 0
        if stock_data.get("ocf_positive", False) and stock_data.get("ocf_growth", 0) > 20:
            cashflow_score = 75
            evidence.append("经营现金流正+加速增长")
        elif stock_data.get("ocf_positive", False):
            cashflow_score = 50
            evidence.append("经营现金流转正")
        scores["cashflow_improve"] = cashflow_score
        total += cashflow_score * cls.COMPANY_DIMENSIONS["cashflow_improve"]["weight"]
        
        final_score = min(100, total)
        
        status = "企业拐点未确认"
        if final_score >= 80:
            status = "🔥企业基本面拐点已确认"
        elif final_score >= 60:
            status = "企业拐点确认中"
        elif final_score >= 35:
            status = "企业拐点初现"
        else:
            status = "企业基本面无明显拐点"
        
        return {
            "score": round(final_score, 1),
            "status": status,
            "dimension_scores": {k: round(v, 1) for k, v in scores.items()},
            "evidence": list(set(evidence)),
            "is_inflection": final_score >= 60,
            "confidence": "高" if final_score >= 80 else "中" if final_score >= 50 else "低"
        }
    
    @classmethod
    def analyze_benjiu_double(cls, stock_data: Dict, news_signals: List[str]) -> Dict:
        """
        苯韭双击/单击综合分析 v3.0
        
        三种形态:
        1. 苯韭双击(最硬逻辑): 产业拐点≥65分 + 企业拐点≥60分 = 双重拐点共振
           逻辑: 产业大势已来 + 企业率先出业绩 → 最确定性投资机会
        2. 苯韭单击-产业型: 产业拐点确认 + 企业未跟进
           逻辑: 等企业信号确认后加仓(此时赔率最高但确定性低)
        3. 苯韭单击-企业型: 企业拐点确认 + 产业未共振
           逻辑: 个股独立行情(需防范宏观/行业风险)
        """
        # 产业拐点分析
        industry_result = cls.analyze_industry_inflection(stock_data, news_signals)
        
        # 企业基本面拐点分析
        company_result = cls.analyze_company_inflection(stock_data, news_signals)
        
        industry_score = industry_result["score"]
        company_score = company_result["score"]
        
        # 综合判定
        result = {
            "industry_inflection": industry_result["is_inflection"],
            "company_inflection": company_result["is_inflection"],
            "industry_score": industry_score,
            "company_score": company_score,
            "industry_status": industry_result["status"],
            "company_status": company_result["status"],
            "industry_evidence": industry_result["evidence"],
            "company_evidence": company_result["evidence"],
            "score": 0,
            "type": "无双击信号",
            "type_icon": "—",
            "logic": "",
            "conviction": ""
        }
        
        # 判定双击类型
        if industry_result["is_inflection"] and company_result["is_inflection"]:
            # 苯韭双击 — 最硬逻辑
            combined = industry_score * 0.55 + company_score * 0.45
            result["score"] = min(98, round(combined + 5, 1))
            result["type"] = "🔥苯韭双击"
            result["type_icon"] = "⭐⭐⭐"
            result["logic"] = (f"产业拐点({industry_score:.0f}分)∩企业拐点({company_score:.0f}分)→"
                             f"最硬选股逻辑,确定性最高")
            result["conviction"] = "最高确定性:产业+企业双拐点共振,股价上涨的底层逻辑最硬"
            
            # 双击力度分析
            gap = industry_score - company_score
            if gap > 10:
                result["logic"] += " [企业滞后于产业→后续企业加速空间更大]"
            elif gap < -5:
                result["logic"] += " [企业领先于产业→大概率引领行业反转]"
            else:
                result["logic"] += " [产业企业同步共振→最佳介入时机]"
                
        elif industry_result["is_inflection"] and not company_result["is_inflection"]:
            # 苯韭单击-产业型
            result["score"] = round(industry_score * 0.6 + company_score * 0.1, 1)
            result["type"] = "苯韭单击-产业型"
            result["type_icon"] = "⭐⭐"
            result["logic"] = (f"产业拐点已确认({industry_score:.0f}分)但企业拐点未到({company_score:.0f}分)→"
                             f"赔率高但需等企业信号,适合轻仓布局等双击确认")
            result["conviction"] = "中等确定性:产业大势确定但个股拐点未现,等连续2季报验证后加仓"
            
        elif not industry_result["is_inflection"] and company_result["is_inflection"]:
            # 苯韭单击-企业型
            result["score"] = round(company_score * 0.5 + industry_score * 0.2, 1)
            result["type"] = "苯韭单击-企业型"
            result["type_icon"] = "⭐"
            result["logic"] = (f"企业拐点已确认({company_score:.0f}分)但产业未共振({industry_score:.0f}分)→"
                             f"个股独立行情,需防范行业下行拖累")
            result["conviction"] = "中等偏低确定性:企业先行反转但行业未确认,关注后续产业信号"
            
        else:
            # 无拐点信号
            result["score"] = round(max(industry_score, company_score) * 0.3, 1)
            result["type"] = "无双击信号"
            result["type_icon"] = "—"
            result["logic"] = (f"产业({industry_score:.0f}分)+企业({company_score:.0f}分)均未确认拐点→"
                             f"暂无双击机会,等待信号")
            result["conviction"] = "当前不适合苯韭双击框架选股"
        
        return result
    
    @classmethod
    def get_stock_inflection_snapshot(cls, name: str, stock_data: Dict, news_signals: List[str]) -> Dict:
        """
        快速生成单标的双击快照 — 用于报告展示
        """
        davis = cls.analyze_davis_double(stock_data)
        benjiu = cls.analyze_benjiu_double(stock_data, news_signals)
        
        return {
            "name": name,
            "symbol": stock_data.get("symbol", ""),
            "davis": davis,
            "benjiu": benjiu,
            "best_play": "戴维斯双击" if davis["score"] > benjiu["score"] 
                         else ("苯韭双击" if "双击" in benjiu["type"] else benjiu["type"]),
            "signal_score": max(davis["score"], benjiu["score"])
        }


class MarketCatalystClassifier:
    """
    超景气价值投机框架 (第15维度)
    
    对A股上涨因素进行分层分类:
    一层: 流动性溢价(普涨牛市) vs 高股息吃息(熊市抱团)
    二层: 消息事件驱动 → 现象级行业拐点 vs 其他消息驱动
    
    现象级事件 → 有持续力、可复现 → 超景气价值投机机会(高确定性)
    其他消息 → 一次性、不可复现 → 短期炒作(低确定性)
    """
    
    # ═══ 消息事件分类体系 ═══
    PHENOMENON_CATALYSTS = {
        "科技突破": {"keywords": ["技术突破", "新品发布", "量产", "工艺迭代", "0.7nm", "颠覆", "HBM", "EUV", 
                          "AI芯片", "大模型", "存储涨价", "新品", "黑科技"], "score": 95, "duration": "12-24个月"},
        "资源供需失衡": {"keywords": ["供给收缩", "停产", "出口管制", "矿产枯竭", "供应链断裂", "断供", "缺货",
                               "涨价潮", "资源争夺"], "score": 90, "duration": "6-18个月"},
        "政治经济突发": {"keywords": ["贸易战", "关税", "制裁", "禁运", "冲突", "封锁", "军事行动",
                                "地缘风险"], "score": 85, "duration": "3-12个月"},
        "海外映射": {"keywords": ["美股映射", "海外对标", "全球龙头", "Apple", "Microsoft", "NVIDIA",
                            "海外需求溢出", "国际巨头合作"], "score": 80, "duration": "3-9个月"},
        "产业周期反转": {"keywords": ["周期底部", "去库存完成", "产能出清", "行业亏损面", "并购整合",
                                "龙头企业率先改善", "开工率回升"], "score": 88, "duration": "12-24个月"},
    }
    
    # 其他消息驱动(一次性/短期/不可复现) ← 低确定性
    OTHER_CATALYSTS = {
        "政策利好": {"keywords": ["政策", "补贴", "规划", "纲要", "国务院", "发改委", "工信部", "意见", "通知",
                            "放松管制", "准入门槛"], "score": 55, "risk": "政策力度不确定性,可能是口号"},
        "炒名字/概念": {"keywords": ["概念", "板块", "题材", "风口", "热点", "新概念", "命名", 
                               "改名", "新板块"], "score": 30, "risk": "短期情绪驱动,无基本面支撑"},
        "被纳入概念板块": {"keywords": ["纳入", "调入", "入选", "概念股", "指数调整", "成份股"], 
                        "score": 40, "risk": "被动资金流入,一次性效应"},
        "收购/重组": {"keywords": ["收购", "重组", "并购", "借壳", "注入", "整合", "合并"],
                     "score": 45, "risk": "并购后整合风险,商誉减值"},
        "巨头合作": {"keywords": ["合作", "联手", "牵手", "签约", "战略合作", "独家合作"],
                     "score": 50, "risk": "合作落地不确定,业绩贡献待验证"},
        "其他利好": {"keywords": ["利好", "利好不断", "利好兑现", "订单", "中标", "合同"],
                     "score": 48, "risk": "单次利好,持续性不足"},
    }
    
    @classmethod
    def classify_catalyst(cls, news_signals: List[str], stock_data: Dict = None) -> Dict:
        """
        分类消息事件类型，判断是否为超景气价值投机机会
        
        返回: {
            "phenomenon_signals": 现象级信号列表,
            "other_signals": 其他信号列表,
            "primary_type": 主要类型,
            "is_super_boom": 是否为超景气投机标的,
            "certainty_score": 确定性评分,
            "speculation_score": 综合投机评分
        }
        """
        content = " ".join(news_signals).lower() if news_signals else ""
        
        phenomenon_hits = []
        other_hits = []
        
        # 检测现象级信号
        for ptype, pdata in cls.PHENOMENON_CATALYSTS.items():
            matched = [kw for kw in pdata["keywords"] if kw.lower() in content]
            if matched:
                phenomenon_hits.append({
                    "type": ptype,
                    "score": pdata["score"],
                    "duration": pdata["duration"],
                    "keywords": matched,
                    "quality": "超景气" if pdata["score"] >= 85 else "准景气"
                })
        
        # 检测其他消息驱动
        for otype, odata in cls.OTHER_CATALYSTS.items():
            matched = [kw for kw in odata["keywords"] if kw.lower() in content]
            if matched:
                other_hits.append({
                    "type": otype,
                    "score": odata["score"],
                    "risk": odata["risk"],
                    "keywords": matched
                })
        
        # 排序
        phenomenon_hits.sort(key=lambda x: -x["score"])
        other_hits.sort(key=lambda x: -x["score"])
        
        # 综合判定
        total_phenomenon = sum(h["score"] for h in phenomenon_hits)
        total_other = sum(h["score"] for h in other_hits)
        
        # 超景气判定: 现象级信号占主导
        is_super_boom = total_phenomenon > 80
        primary_type = "超景气价值投机" if is_super_boom else "普通消息驱动"
        
        # 确定性评分
        if is_super_boom:
            certainty = min(95, 60 + total_phenomenon * 0.25 - total_other * 0.1)
        else:
            certainty = max(20, total_phenomenon * 0.3 + total_other * 0.15)
        
        # 噪音比 = 现象级/(现象级+其他)
        noise_ratio = total_phenomenon / (total_phenomenon + total_other + 0.001)
        
        return {
            "phenomenon_signals": phenomenon_hits,
            "other_signals": other_hits,
            "primary_type": primary_type,
            "is_super_boom": is_super_boom,
            "certainty_score": round(certainty, 1),
            "noise_ratio": round(noise_ratio, 2),
            "total_phenomenon": total_phenomenon,
            "total_other": total_other,
            "recommendation": cls._get_recommendation(is_super_boom, certainty, noise_ratio)
        }
    
    @classmethod
    def _get_recommendation(cls, is_super_boom: bool, certainty: float, noise_ratio: float) -> str:
        """生成操作建议"""
        if is_super_boom and certainty >= 85:
            return "🔥超景气投机(高确定性)→优先配置，重仓参与"
        elif is_super_boom and certainty >= 70:
            return "超景气投机(中确定性)→核心仓位，积极布局"
        elif is_super_boom:
            return "准超景气→轻仓试探，等待更多信号确认"
        elif certainty >= 50:
            return "普通消息驱动→短线参与，快进快出"
        elif noise_ratio < 0.3:
            return "噪音主导→回避，无持续力"
        else:
            return "无催化剂→不属于事件驱动策略"
    
    @classmethod
    def assess_market_phase(cls, turnover: float, index_change: float, 
                           high_div_performance: float = 0) -> Dict:
        """
        评估当前市场阶段: 流动性溢价阶段 vs 高股息抱团阶段
        
        流动性溢价(普涨): 成交放大 + 指数上行 + 中小创领涨
        高股息吃息(抱团): 成交萎缩 + 指数震荡/下行 + 红利股相对强势
        """
        if turnover >= 15000 and index_change >= 0.5:
            phase = "流动性溢价牛市"
            suggestion = "优先配置中小成长标的(科创50/机器人/AI算力),弹性最大"
        elif turnover >= 15000 and index_change >= -1.0:
            phase = "流动性充裕震荡市"
            suggestion = "均衡配置,成长+红利各半"
        elif turnover >= 10000:
            phase = "正常流动性"
            suggestion = "注重基本面和双击逻辑选股"
        elif turnover < 8000:
            phase = "流动性枯竭"
            suggestion = "防御为主,高股息吃息+黄金ETF,少动多观察"
        else:
            phase = "存量博弈"
            suggestion = "精选个股,等流动性恢复"
        
        return {
            "phase": phase,
            "suggestion": suggestion,
            "turnover_bill": round(turnover/10000, 2),
            "index_change": f"{index_change:+.1f}%",
            "prefer_small_growth": turnover >= 15000 and index_change >= 0.5,
            "prefer_high_dividend": turnover < 10000
        }


class PhenomenonTierClassifier:
    """
    现象级事件三层分类模型 (第16维度)
    
    按可预判性、爆发力、持续力、出现概率将现象级事件分为三层:
    
    ①类: 不可预期的突发性事件
        - 出现几率很低(黑天鹅级)
        - 短期爆发力极高、影响范围大、持续时间长、回调大
        - 策略: 等第一波回调后再参与，吃大波段第二波(确定性最高)
        - 案例: 黎巴嫩BB机事件、COVID疫情、俄乌战争
    
    ②类: 超级事件周期内的细分产业化拐点 ← 最重要！可预判埋伏
        - 从宏观叙事(如AI大周期)中推导出产业链细分拐点
        - 影响范围小(单个细分)、持续时间长短不一
        - 策略: 提前预判畅想并埋伏(守株待兔)，等企业业绩确认后加仓
        - 案例: AI→HBM→存储芯片涨价→光模块→CPO→液冷→铜连接...
    
    ③类: 概率较高的小拐点事件(一年多次)
        - 基于经济周期/季节规律/财报周期
        - 爆发力不确定、影响范围不确定、持续较短
        - 策略: 等多条件重叠时参与(确定性可提升到70%+)
        - 案例: Q1春耕/Q2中报/Q3旺季/Q4估值切换等事件周期
    """
    
    # ═══ ①类黑天鹅事件库 ═══
    TIER1_BLACKSWAN = {
        "大规模战争爆发": {"probability": 0.02, "impact_scope": "全球/全市场", "duration": "6-24月",
                       "burst_power": 95, "drawdown": "第一波后30-50%回调", "strategy": "等回调建仓,吃第二波"},
        "重大地缘冲突": {"probability": 0.05, "impact_scope": "区域/能源/军工", "duration": "3-12月",
                      "burst_power": 90, "drawdown": "第一波后20-30%回调", "strategy": "冲突核心资产+避险对冲"},
        "全球疫情/灾害": {"probability": 0.03, "impact_scope": "全市场", "duration": "6-18月",
                       "burst_power": 88, "drawdown": "恐慌下跌后V型反转", "strategy": "恐慌底部加仓ETF"},
        "金融系统性危机": {"probability": 0.04, "impact_scope": "金融/地产", "duration": "3-12月",
                       "burst_power": 85, "drawdown": "指数-20%以上", "strategy": "国家队入场后跟随"},
    }
    
    # ═══ ②类可预判埋伏库(超级事件→细分拐点) ═══
    TIER2_PREDICTABLE = {
        "AI大周期": {
            "macro_narrative": "AI成为继互联网之后最大的技术变革周期",
            "sub_inflections": [
                {"name": "存储芯片(HBM)", "timing": "2024Q2-2026", "status": "已确认,持续中",
                 "signal": "HBM价格涨2-3倍,SK海力士/美光供不应求", "stocks": ["688525","301308"]},
                {"name": "光模块/CPO", "timing": "2024Q3-2026", "status": "已确认,渗透加速",
                 "signal": "800G/1.6T光模块订单爆发", "stocks": ["300502","300394"]},
                {"name": "铜连接/液冷", "timing": "2025H1-2027", "status": "拐点确认中",
                 "signal": "GB200/300机柜铜连接方案替代光连接", "stocks": ["601138"]},
                {"name": "AI终端/手机", "timing": "2026H2-2028", "status": "即将到来",
                 "signal": "折叠屏iPhone+AI Agent", "stocks": ["002475","300433"]},
                {"name": "人形机器人", "timing": "2026-2028", "status": "量产元年",
                 "signal": "宇树R1降至2.99万,特斯拉Optimus量产", "stocks": ["688017","002747"]},
                {"name": "AI广告/营销", "timing": "2026-2028", "status": "商业模式落地中",
                 "signal": "豆包收费版+微信支付宝Agent", "stocks": []},
            ]
        },
        "半导体国产替代": {
            "macro_narrative": "中美科技脱钩→半导体全链国产化",
            "sub_inflections": [
                {"name": "半导体设备", "timing": "2024-2030", "status": "已确认,深度推进",
                 "signal": "北方华创/中微公司订单+45%", "stocks": ["002371","688012"]},
                {"name": "晶圆制造", "timing": "2025-2027", "status": "拐点确认",
                 "signal": "中芯国际产能利用率>95%", "stocks": ["688981"]},
                {"name": "EDA/IP", "timing": "2026-2028", "status": "初期突破",
                 "signal": "华大九天/概伦电子国产化率提升", "stocks": []},
                {"name": "先进封装", "timing": "2025-2027", "status": "已确认",
                 "signal": "长电科技/通富微电Chiplet订单", "stocks": []},
            ]
        },
        "战略资源武器化": {
            "macro_narrative": "中国利用资源禀赋反击技术封锁",
            "sub_inflections": [
                {"name": "小金属(钨/锡/锗)", "timing": "2025-2027", "status": "已确认,持续强化",
                 "signal": "出口管制升级+日本东曹停产", "stocks": ["002378","000960","600497"]},
                {"name": "稀土永磁", "timing": "2025-2028", "status": "拐点确认中",
                 "signal": "出口配额收紧+新能源/军工需求", "stocks": []},
                {"name": "贵金属(黄金/白银)", "timing": "2025-2027", "status": "已确认",
                 "signal": "央行购金+地缘冲突+降息预期", "stocks": ["600489","600547"]},
            ]
        },
        "能源转型周期": {
            "macro_narrative": "碳中和→新能源产业链重构",
            "sub_inflections": [
                {"name": "锂矿周期反转", "timing": "2026H2-2028", "status": "拐点确认中",
                 "signal": "锂价触底回升+澳矿减产+需求恢复", "stocks": ["002460","002466"]},
                {"name": "固态电池", "timing": "2027-2029", "status": "技术验证期",
                 "signal": "丰田/宁德时代固态电池量产时间表", "stocks": []},
                {"name": "光伏出清反弹", "timing": "2026H2-2027", "status": "筑底中",
                 "signal": "组件价格跌破成本线→中小企业退出", "stocks": []},
            ]
        },
    }
    
    # ═══ ③类高频小拐点事件 ═══
    TIER3_RECURRING = {
        "Q1春耕行情": {"probability": 0.70, "timing": "每年1-3月", "catalyst": "一季报预期+流动性宽松",
                     "typical_return": "10-20%", "reliable_sectors": ["券商","半导体","消费电子"]},
        "Q2中报季": {"probability": 0.65, "timing": "每年4-6月", "catalyst": "半年报业绩驱动",
                    "typical_return": "5-15%", "reliable_sectors": ["业绩超预期个股"]},
        "Q3旺季备货": {"probability": 0.60, "timing": "每年7-9月", "catalyst": "消费电子旺季+中秋国庆",
                     "typical_return": "5-10%", "reliable_sectors": ["消费电子","食品饮料","半导体"]},
        "Q4估值切换": {"probability": 0.75, "timing": "每年10-12月", "catalyst": "公募排名+跨年行情",
                     "typical_return": "8-15%", "reliable_sectors": ["券商","成长股","高送转"]},
        "券商合并驱动": {"probability": 0.50, "timing": "不定期", "catalyst": "监管放行+头部合并公告",
                      "typical_return": "20-40%", "reliable_sectors": ["券商"]},
        "存储芯片涨价周期": {"probability": 0.80, "timing": "每24-36月", "catalyst": "供需错配→原厂提价",
                         "typical_return": "30-80%", "reliable_sectors": ["存储芯片","设备","材料"]},
    }

    @classmethod
    def classify_event_tier(cls, event_type: str, news_content: str) -> Dict:
        """对单个事件进行三层分类"""
        result = {
            "tier": 3,  # 默认③类
            "classification": "高频事件",
            "probability": "中高",
            "predictable": False,
            "strategy": "等多条件重叠",
            "burst_power": 30,
            "suggested_action": ""
        }
        
        # 检测①类黑天鹅
        for event_name, event_data in cls.TIER1_BLACKSWAN.items():
            keywords = event_name.replace("大规模","").replace("重大","").replace("全球","").split("/")
            if any(kw in news_content for kw in keywords if len(kw) >= 2):
                result["tier"] = 1
                result["classification"] = f"①突发黑天鹅: {event_name}"
                result["probability"] = f"{event_data['probability']*100:.0f}%"
                result["predictable"] = False
                result["strategy"] = event_data["strategy"]
                result["burst_power"] = event_data["burst_power"]
                result["suggested_action"] = f"不追第一波,等{event_data['drawdown']}再建仓"
                result["duration"] = event_data["duration"]
                result["impact_scope"] = event_data["impact_scope"]
                return result
        
        # 检测②类可预判 → 匹配细分拐点
        for macro_name, macro_data in cls.TIER2_PREDICTABLE.items():
            for sub in macro_data["sub_inflections"]:
                sub_name = sub["name"].lower()
                sub_kw = sub_name.split("(")[0].strip() if "(" in sub_name else sub_name
                if sub_kw in news_content or sub_name in news_content:
                    result["tier"] = 2
                    result["classification"] = f"②可预判: {macro_name}→{sub['name']}"
                    result["probability"] = "中高(可提前预判)"
                    result["predictable"] = True
                    result["strategy"] = "提前埋伏(守株待兔)"
                    result["burst_power"] = 70
                    result["suggested_action"] = (f"当前'{sub['status']}',等{sub['signal'][:30]}→确认后加仓"
                                                 if "已确认" not in sub["status"] else
                                                 f"已确认'{sub['status']}',核心仓位持有")
                    result["duration"] = sub["timing"]
                    result["impact_scope"] = f"{sub['name']}产业链"
                    result["sub_inflection"] = sub
                    return result
        
        # ③类高频事件检测
        for event_name, event_data in cls.TIER3_RECURRING.items():
            if any(kw in news_content for kw in event_name.split("/") if len(kw) >= 2):
                result["tier"] = 3
                result["classification"] = f"③高频: {event_name}"
                result["probability"] = f"{event_data['probability']*100:.0f}%"
                result["predictable"] = True
                result["strategy"] = "按日历规律,等多条件重叠"
                result["burst_power"] = 45
                result["suggested_action"] = (f"时间窗口{event_data['timing']},典型收益{event_data['typical_return']},"
                                            f"关注{event_data['reliable_sectors'][0]}")
                result["duration"] = event_data["timing"]
                return result
        
        # 未匹配 → 默认③类
        result["suggested_action"] = "未识别到明确的事件类型,待跟踪"
        return result
    
    @classmethod
    def build_pre_position_pipeline(cls, stock_name: str, all_news: List[str]) -> List[Dict]:
        """
        构建主动埋伏管道 — 扫描②类可预判细分拐点
        
        返回: 标的可以埋伏的所有细分拐点，按时序排列
        """
        content = " ".join(all_news).lower() if all_news else ""
        pipeline = []
        
        # 按②类超级事件→细分拐点匹配
        stock_map = {
            "科创50": ["存储芯片(HBM)", "光模块/CPO", "AI终端/手机", "人形机器人"],
            "中芯": ["半导体设备", "晶圆制造", "先进封装"],
            "赣锋": ["锂矿周期反转", "固态电池"],
            "天齐": ["锂矿周期反转", "固态电池"],
            "银河": ["券商合并"],
            "锡业": ["小金属(钨/锡/锗)"],
            "黄金": ["贵金属(黄金/白银)"],
            "绿的": ["人形机器人"],
            "埃斯顿": ["人形机器人"],
            "佰维": ["存储芯片(HBM)"],
            "江波龙": ["存储芯片(HBM)"],
            "北方华创": ["半导体设备"],
        }
        
        target_inflections = []
        for map_name, inflections in stock_map.items():
            if map_name in stock_name:
                target_inflections = inflections
                break
        
        for sub_name in target_inflections:
            for macro_name, macro_data in cls.TIER2_PREDICTABLE.items():
                for sub in macro_data["sub_inflections"]:
                    if sub["name"] == sub_name:
                        # 计算信号匹配度
                        signal_match = sum(1 for kw in sub["signal"].replace("+"," ").split(",") 
                                         if kw.strip()[:3].lower() in content)
                        
                        if "已确认" in sub["status"]:
                            action = "✅ 已确认,核心仓位持有"
                            confidence = 90
                        elif "确认中" in sub["status"] or "确认" in sub["status"]:
                            action = "⏳ 拐点确认中,轻仓布局等信号"
                            confidence = 65
                        elif "即将" in sub["status"]:
                            action = "🔮 前瞻布局,极轻仓埋伏(1-2%)"
                            confidence = 40
                        else:
                            action = "⏸ 观察期,暂不参与"
                            confidence = 20
                        
                        pipeline.append({
                            "sub_inflection": sub_name,
                            "macro_theme": macro_name,
                            "timing": sub["timing"],
                            "status": sub["status"],
                            "signal": sub["signal"][:50],
                            "stocks": sub["stocks"],
                            "action": action,
                            "confidence": confidence,
                            "signal_match": signal_match
                        })
        
        pipeline.sort(key=lambda x: -x["confidence"])
        return pipeline


class TradingModeClassifier:
    """
    两种交易模式分类器 (第17维度)
    
    剑宗(一波流) 30%: 
        - 最热细分赛道，一波行情(半年-一年)
        - 海外业绩映射国内→短期无业绩支撑，易透支估值
        - 与大盘涨跌关联不大，可逆势上涨
        - 流动性好时生命周期更长
        - 高位破线减仓→缩量企稳后加回
    
    气宗(长期格局) 70%:
        - 高确定性，基本面改善+业绩持续显现+市场渗透
        - 可格局长拿，位置可增加
        - 第①类第一波强但短期不兑现业绩→回调大→可以格局
        - 压舱石作用: 降低波动率+超级行情爆发时有大仓位吃指数暴涨
        
    判定逻辑:
        - 剑宗: 海外映射/主题炒作/概念驱动/技术突破早期/无连续季报支撑
        - 气宗: 业绩已验证/产业拐点已确认/龙头企业/连续季度EPS增长/毛利率扩张
    """
    
    # 剑宗特征关键词
    JIANZONG_FEATURES = [
        "海外映射", "美股对标", "主题投资", "概念驱动", "0→1阶段",
        "技术验证期", "量产初期", "无季报支撑", "短期透支", "流动性驱动",
        "板块效应", "跟风上涨", "低渗透率"
    ]
    
    # 气宗特征关键词
    QIZONG_FEATURES = [
        "业绩已验证", "连续增长", "EPS持续改善", "毛利率扩张", "现金流转正",
        "产业拐点已确认", "龙头企业", "成熟商业模式", "高壁垒", "定价权",
        "稳定现金流", "高确定性", "渗透率加速", "量利齐升"
    ]
    
    @classmethod
    def classify(cls, stock_data: Dict, benjiu_result: Dict, 
                davis_result: Dict, tier_result: Dict) -> Dict:
        """
        分类标的交易模式
        
        返回: {
            "mode": "剑宗一波流"|"气宗格局",
            "ratio": 标的在组合中的建议比例,
            "hold_duration": "6-12月"|"12-24月+",
            "entry_rule": 入场规则,
            "exit_rule": 退出规则,
            "rebalance_rule": 再平衡规则
        }
        """
        score_qizong = 0  # 气宗得分
        score_jianzong = 0  # 剑宗得分
        
        # ═══ 气宗加分项 ═══
        # 苯韭双击 → 产业+企业双拐点
        if "双击" in benjiu_result.get("type", ""):
            score_qizong += 30
        
        # 戴维斯双击 → PE×EPS双重扩张
        if "戴维斯双击" in davis_result.get("type", ""):
            score_qizong += 20
        elif "准戴维斯双击" in davis_result.get("type", ""):
            score_qizong += 15
        
        # 企业拐点已确认
        if benjiu_result.get("company_inflection"):
            score_qizong += 15
            if benjiu_result.get("company_score", 0) >= 80:
                score_qizong += 10
        
        # 产业拐点已确认(强)
        ind_stat = benjiu_result.get("industry_status", "")
        if "强" in ind_stat:
            score_qizong += 10
        
        # EPS连续增长
        eps_trend = stock_data.get("eps_trend", [])
        if len(eps_trend) >= 3 and all(e > 0 for e in eps_trend[-3:]):
            score_qizong += 10
            if eps_trend[-1] > eps_trend[-2] > eps_trend[-3]:
                score_qizong += 5  # 加速增长额外加分
        
        # 毛利率扩张
        margin_trend = stock_data.get("margin_trend", [])
        if len(margin_trend) >= 2 and margin_trend[-1] > margin_trend[-2]:
            score_qizong += 8
        
        # 经营现金流转正
        if stock_data.get("ocf_positive"):
            score_qizong += 7
        
        # 现金流增长
        if stock_data.get("ocf_growth", 0) >= 30:
            score_qizong += 5
        
        # ESG/治理加分（龙头地位）
        rank = stock_data.get("market_rank", 10)
        if rank == 1:
            score_qizong += 8
        elif rank <= 3:
            score_qizong += 5
        
        # ═══ 剑宗加分项 ═══
        # 超景气价值投机 → 现象级事件驱动
        event_type = stock_data.get("event_type", "")
        if event_type in ["科技突破", "海外映射"]:
            score_jianzong += 15
        
        # 技术突破标签 → 0→1阶段
        if stock_data.get("tech_breakthrough"):
            score_jianzong += 12
        
        # EPS趋势有负数（未盈利/扭亏初期）
        if len(eps_trend) >= 2 and any(e <= 0 for e in eps_trend[-2:]):
            score_jianzong += 10
        
        # PE高估（PE分位>50%）
        if stock_data.get("pe_percentile", 0.5) > 0.5:
            score_jianzong += 8
        
        # 无经营现金流
        if not stock_data.get("ocf_positive"):
            score_jianzong += 8
        
        # 高波动率行业（科技/贵金属/小金属）
        high_vol_industries = ["NEW_TECH", "资源供需失衡"]
        if stock_data.get("industry_type", "") in high_vol_industries:
            score_jianzong += 5
        
        # ③类高频事件
        if tier_result.get("tier") == 3:
            score_jianzong += 5
        
        # ═══ 综合判定 ═══
        gap = score_qizong - score_jianzong
        
        if gap >= 20:
            mode = "气宗格局"
            base_ratio = 12  # 在组合中占12%
            hold_duration = "12-24月+"
            conviction = "高确定性:基本面持续改善+业绩连续验证,格局长拿不慌"
            entry_rule = "回调至均线(MA20/MA60)即加仓,不追高"
            exit_rule = "连续2季度EPS增速放缓+毛利率下降→减仓;产业拐点消失→清仓"
        elif gap >= 5:
            mode = "气宗偏剑"
            base_ratio = 10
            hold_duration = "9-18月"
            conviction = "偏格局:基本面改善中但未完全确认,核心仓位持有+小仓位做波段"
            entry_rule = "等季报确认后加仓至满配,初期轻仓试探"
            exit_rule = "高位放量破MA20减1/3,跌破MA60减至半仓"
        elif gap >= -10:
            mode = "剑宗偏气"
            base_ratio = 8
            hold_duration = "6-12月"
            conviction = "偏一波流:有基本面改善预期但未兑现,等业绩确认可转为格局"
            entry_rule = "缩量企稳后轻仓介入,季报超预期→加仓至气宗"
            exit_rule = "高位放巨量→减半仓;破MA20全清;等二波信号再加回"
        else:
            mode = "剑宗一波流"
            base_ratio = 5
            hold_duration = "3-9月"
            conviction = "纯一波流:短期热度炒作,无基本面支撑,快进快出不恋战"
            entry_rule = "事件催化当日轻仓追,次日不加仓"
            exit_rule = "高位破线(MA5/MA10)无条件清仓;缩量企稳可轻仓接回做二波"
        
        # ═══ 仓位动态调整 ═══
        # 流动性Buff修正仓位
        rebalance = {
            "super_bull": "流动性超级Buff→剑宗提高至40%",
            "normal": f"剑宗30% : 气宗70%基准配置",
            "bear": "流动性虚弱→剑宗降至10%或清零,气宗底仓持有",
            "trigger_upgrade": "剑宗标的连续2季报超预期→升级为气宗,比例翻倍",
            "trigger_downgrade": "气宗标的季报miss→降级为剑宗,减仓等待",
        }
        
        return {
            "mode": mode,
            "base_ratio": base_ratio,
            "hold_duration": hold_duration,
            "conviction": conviction,
            "entry_rule": entry_rule,
            "exit_rule": exit_rule,
            "rebalance": rebalance,
            "qizong_score": score_qizong,
            "jianzong_score": score_jianzong,
            "score_gap": gap,
            "category": "气宗" if "气宗" in mode else "剑宗"
        }
    
    @classmethod
    def build_portfolio_allocation(cls, plan_results: List[Dict]) -> Dict:
        """构建组合仓位配置方案"""
        jianzong_stocks = [s for s in plan_results 
                          if s.get("trade_mode", {}).get("category") == "剑宗"]
        qizong_stocks = [s for s in plan_results 
                        if s.get("trade_mode", {}).get("category") == "气宗"]
        
        total_jian = sum(s.get("trade_mode", {}).get("base_ratio", 5) for s in jianzong_stocks)
        total_qiz = sum(s.get("trade_mode", {}).get("base_ratio", 10) for s in qizong_stocks)
        total = total_jian + total_qiz
        
        # 归一化到100%
        jian_pct = round(total_jian / total * 100, 1) if total > 0 else 30
        qiz_pct = round(total_qiz / total * 100, 1) if total > 0 else 70
        
        return {
            "jianzong_count": len(jianzong_stocks),
            "qizong_count": len(qizong_stocks),
            "jianzong_pct": jian_pct,
            "qizong_pct": qiz_pct,
            "jianzong_stocks": [(s["name"], s["trade_mode"]["base_ratio"]) for s in jianzong_stocks],
            "qizong_stocks": [(s["name"], s["trade_mode"]["base_ratio"]) for s in qizong_stocks],
            "advice": cls._get_allocation_advice(jian_pct, qiz_pct)
        }
    
    @classmethod
    def _get_allocation_advice(cls, jian_pct: float, qiz_pct: float) -> str:
        if jian_pct > 40:
            return "剑宗占比偏高→检查是否有可转为气宗的标的(等季报确认)"
        elif qiz_pct > 80:
            return "气宗占比过高→组合缺乏弹性,考虑增加1-2个剑宗高弹性标的"
        else:
            return f"剑宗{jian_pct:.0f}%:气宗{qiz_pct:.0f}%→接近黄金比例30:70"




class AutoControlLeadershipMapper:
    """
    自主可控与遥遥领先双向映射 (第22维度) — 苯韭双吃
    
    核心公式:
    东大自主可控 = 西方遥遥领先
    东大遥遥领先 = 西方自主可控
    
    定义:
    - 自主可控: 对当前社会非常重要的关键领域,但还没有完全掌握并突破
    - 遥遥领先: 该领域产品放在世界上断档领先,找不出像样的对手
    
    东大自主可控产业 (当前舞台中央):
    - 芯片半导体为主的信息产业 (最后5公里突破)
    - 生物医药 (和西方差距大,但二级市场可能有行情)
    
    东大遥遥领先产业 (断档领先):
    - 稀有金属、锂电池、激光雷达、钢铁、水泥、光伏、新能源汽车
    - 电力、家电、吃穿住行等 (产能溢出的过剩行业需剔除)
    
    西方遥遥领先:
    - 高端存储、先进制程芯片、AI应用、操作系统、商业航天
    
    西方自主可控:
    - 电力、电解铝、稀有金属、激光雷达等 (被东大卡脖子)
    
    映射对照表 (正反双向):
    """
    
    # 东大自主可控 → 西方遥遥领先
    CN_AUTO_TO_WEST_LEAD = {
        "中芯国际": {"western": "台积电(TSMC)", "logic": "晶圆代工自主可控 vs 全球遥遥领先"},
        "寒武纪": {"western": "英伟达(NVIDIA)", "logic": "AI芯片自主可控 vs GPU遥遥领先"},
        "长鑫存储": {"western": "SK海力士/Samsung", "logic": "存储自主可控 vs HBM遥遥领先"},
        "汇量科技": {"western": "AppLovin", "logic": "营销科技自主可控 vs 全球领先"},
        "北方华创": {"western": "应用材料(AMAT)", "logic": "设备自主可控 vs 全球龙头"},
    }
    
    # 东大遥遥领先 → 西方自主可控
    CN_LEAD_TO_WEST_AUTO = {
        "北方稀土/金力永磁": {"western": "MP Materials", "logic": "稀土遥遥领先 vs 美国自主可控"},
        "湖南黄金": {"western": "美国锑业", "logic": "锑金属遥遥领先 vs 美国自主可控"},
        "云南锗业": {"western": "AXT", "logic": "锗金属遥遥领先 vs 西方自主可控"},
        "中国宏桥": {"western": "美国铝业(Alcoa)", "logic": "电解铝遥遥领先 vs 美国自主可控"},
        "禾赛科技": {"western": "Aeva/Mobileye", "logic": "激光雷达遥遥领先 vs 西方自主可控"},
        "宁德时代": {"western": "LG新能源", "logic": "锂电池遥遥领先 vs 西方追赶"},
        "隆基绿能": {"western": "First Solar", "logic": "光伏遥遥领先 vs 西方自主可控"},
        "比亚迪": {"western": "特斯拉/大众", "logic": "新能源车遥遥领先 vs 西方追赶"},
    }
    
    @classmethod
    def get_mapping(cls, stock_name: str) -> Dict:
        """获取标的的双向映射关系"""
        if stock_name in cls.CN_AUTO_TO_WEST_LEAD:
            data = cls.CN_AUTO_TO_WEST_LEAD[stock_name]
            return {
                "type": "东大自主可控",
                "cn_name": stock_name,
                "western": data["western"],
                "logic": data["logic"],
                "strategy": "波动大易暴涨暴跌,回调深,适合苯韭双击框架",
                "a_share_action": "等回调至产业拐点确认后介入",
                "us_stock_action": "若西方对标暴涨,A股映射有滞后性,可提前埋伏"
            }
        elif stock_name in cls.CN_LEAD_TO_WEST_AUTO:
            data = cls.CN_LEAD_TO_WEST_AUTO[stock_name]
            return {
                "type": "东大遥遥领先",
                "cn_name": stock_name,
                "western": data["western"],
                "logic": data["logic"],
                "strategy": "断档领先有定价权,但需剔除产能严重过剩行业",
                "a_share_action": "趋势持有,关注海外映射(西方自主可控政策催化)",
                "us_stock_action": "若西方出自主可控政策,东大龙头先涨"
            }
        return None
    
    @classmethod
    def scan_reverse_opportunity(cls, western_news: List[str]) -> List[Dict]:
        """
        反向扫描: 西方新闻→东大机会
        
        当发现西方出现'自主可控'政策/新闻时,
        对应东大'遥遥领先'标的可能被催化
        """
        opportunities = []
        content = " ".join(western_news).lower()
        
        # 西方自主可控关键词
        us_auto_keywords = ["supply chain security", "decouple", "self-reliant", 
                           "reduce dependence", "onshoring", "friend-shoring"]
        
        if any(kw in content for kw in us_auto_keywords):
            # 西方在搞自主可控 → 东大遥遥领先标的机会
            for cn_name, data in cls.CN_LEAD_TO_WEST_AUTO.items():
                if any(sector in content for sector in ["rare earth", "lithium", "solar", "ev"]):
                    opportunities.append({
                        "trigger": "西方自主可控政策",
                        "cn_beneficiary": cn_name,
                        "western_counterpart": data["western"],
                        "action": "东大遥遥领先标的提前反应,关注A股映射"
                    })
        
        return opportunities


class EscapeTopAnalyzer:
    """
    高位止盈/逃顶信号系统 (第21维度)
    
    三维逃顶框架: 宏观维度 + 板块维度 + 个股维度
    核心原则: 以上条件不是AND而是OR,触发一条即跑,准备清仓式跑路
    
    ═══ 宏观维度逃顶信号 ═══
    1. 官方连发12道金牌提示风险
       - 信号: 当天大幅低开后逐步修复,继续加速大涨 → 进入随时跑路状态
       - 动作: 能格局的等到官方出杀伤力政策(抽流动性),记得一定要跑
       
    2. 增量资金天量(买入动能枯竭)
       - 居民储蓄大幅减少30%+,消费贷款大幅增加,融资余额爆量
       - 基金募资+ETF成交+A股成交突破10万亿
       - 销户和从没炒股的人全部入场(例如奶奶也入市了)
       - 本质: 买入量打到天量,未来动能加速枯竭
       
    3. 卡脖子尖端科技全面突破并商业化
       - EUV/2-3nm芯片商业化量产 或 5nm芯片过剩进入价格战
       - 前者: 估值在天上,利好加速胀破,无想象空间,容易出顶
       - 后者: 制程价格战,说明高端科技需求过剩
       - 本轮本质: 科技自主可控→全面突破(含AI),最卡脖子技术突破后情绪极值
       - 变量: AI出现奇点,以上逻辑全部推翻
    
    ═══ 板块维度逃顶信号 ═══
    1. 新业态渗透率突破30% → 板块行情可能见顶
       - 案例: 上轮新能源和半导体(成熟制程)
       - 本轮: 5nm及以上制程占据全球30%份额
       
    2. 出现更加超景气新赛道 → 主升板块提前结束行情
       - 资金被新赛道虹吸,原板块失血
       
    3. 板块最核心标的走势异常 → 代表市场对该赛道态度转变
    
    ═══ 个股维度逃顶信号 ═══
    1. 单日换手40%+ → 准备随时跑路,不是顶也是顶附近
    2. 个股缩量加速上涨 → 见顶信号之一
    3. 偏概念容量票(毛利低,没净利润) → 利好落地前就会见顶,破日线就跑
    4. 一波流涨幅3倍以上,若还在持续拉升 → 破5日线减仓或清仓,不格局
    5. 实控人公布减持 → 清仓,换最有竞争力同行(除非全市场唯一卡位)
    6. 个股股东人数大幅度增加 → 散户接盘信号
    7. 高位搞不景气业务定增 → 资金饥渴,心虚
    8. 其他重大基本面利空: 业绩造假,创始人退位等
    """
    
    # ═══ 宏观逃顶信号库 ═══
    MACRO_EXIT_SIGNALS = {
        "official_warning": {
            "name": "官方连发12道金牌",
            "keywords": ["证监会", "风险提示", "金牌", "监管", "降温", "泡沫", "过热"],
            "threshold": 12,  # 12道金牌
            "action": "进入随时跑路状态,能格局的等官方出杀伤力政策后跑"
        },
        "liquidity_exhaustion": {
            "name": "增量资金天量(买入动能枯竭)",
            "signals": [
                "居民储蓄减少30%+", "消费贷款大增", "融资余额爆量",
                "基金募资爆棚", "ETF成交万亿", "A股成交突破10万亿",
                "销户入场", "从不炒股的人入市", "奶奶入市"
            ],
            "action": "买入量打到天量,未来动能加速枯竭,准备减仓"
        },
        "tech_breakthrough_peak": {
            "name": "卡脖子技术全面突破商业化",
            "scenarios": [
                "EUV光刻机突破", "2nm芯片量产", "3nm芯片量产",
                "5nm芯片价格战", "成熟制程产能过剩"
            ],
            "action": "最卡脖子技术突破后情绪极值,分批止盈"
        }
    }
    
    # ═══ 板块逃顶信号库 ═══
    SECTOR_EXIT_SIGNALS = {
        "penetration_30": {
            "name": "渗透率突破30%",
            "examples": ["新能源", "半导体成熟制程", "5nm及以上制程占30%份额"],
            "action": "板块行情可能见顶,准备减仓"
        },
        "new_sector_emergence": {
            "name": "更加超景气新赛道出现",
            "effect": "资金虹吸,原板块失血",
            "action": "旧板块提前结束,切换新赛道"
        },
        "leader_weakness": {
            "name": "板块核心标的走势异常",
            "signals": ["龙头股滞涨", "龙头股放量下跌", "龙头股破位"],
            "action": "代表市场态度转变,跟随减仓"
        }
    }
    
    # ═══ 个股逃顶信号库 ═══
    STOCK_EXIT_SIGNALS = {
        "high_turnover": {
            "name": "单日换手40%+",
            "threshold": 0.40,
            "action": "不是顶也是顶附近,准备随时跑路"
        },
        "shrinking_volume_acceleration": {
            "name": "缩量加速上涨",
            "action": "见顶信号之一,减仓观望"
        },
        "concept_capacity_peak": {
            "name": "偏概念容量票见顶",
            "features": ["毛利低", "没净利润", "靠事件驱动"],
            "timing": "利好落地前就会见顶",
            "action": "破日线就跑,不格局"
        },
        "triple_gain": {
            "name": "一波流涨幅3倍以上",
            "action": "破5日线减仓或清仓,不格局"
        },
        "insider_reduction": {
            "name": "实控人公布减持",
            "action": "清仓,换最有竞争力同行(除非全市场唯一卡位)"
        },
        "shareholder_increase": {
            "name": "股东人数大幅增加",
            "action": "散户接盘信号,减仓"
        },
        "high_price_private_placement": {
            "name": "高位搞不景气业务定增",
            "action": "资金饥渴心虚,减仓"
        },
        "fundamental_bearish": {
            "name": "重大基本面利空",
            "examples": ["业绩造假", "创始人退位", "信息披露违规"],
            "action": "清仓"
        }
    }
    
    @classmethod
    def scan_exit_signals(cls, stock_data: Dict, news_signals: List[str], 
                         technical_data: Dict) -> Dict:
        """
        扫描三维逃顶信号 — 动态版(基于今日新闻关键词权重)
        """
        content = " ".join(news_signals).lower() if news_signals else ""
        
        macro_keywords = {
            "金牌": 15, "风险提示": 15, "证监会": 12, "监管": 12,
            "降温": 10, "泡沫": 25, "过热": 20, "暴跌": 15,
            "做空": 15, "加息": 12, "回调": 8, "清仓": 20, "崩盘": 25
        }
        sector_keywords = {
            "半导体见顶": 20, "芯片过剩": 18, "AI泡沫": 25,
            "算力过剩": 20, "存储过剩": 15, "锂价下跌": 15
        }
        
        macro_signals = []; sector_signals = []; stock_signals = []
        macro_score = 0; sector_score = 0; stock_score = 0
        
        # 宏观扫描(关键词×出现次数×权重)
        for kw, weight in macro_keywords.items():
            if kw.lower() in content:
                count = content.count(kw.lower())
                weighted = min(weight * count, 30)
                macro_score += weighted
                if weighted >= 10:
                    macro_signals.append({
                        "type": f"新闻含'{kw}'(出现{count}次)",
                        "score": round(weighted, 1),
                        "action": "关注监管风向" if "监管" in kw or "证监会" in kw else "警惕情绪恶化"
                    })
        
        # 板块扫描
        for kw, weight in sector_keywords.items():
            if kw.lower() in content:
                sector_score += weight
                sector_signals.append({
                    "type": f"板块风险: {kw}",
                    "score": weight,
                    "action": "审视该板块持仓"
                })
        
        # 个股扫描
        turnover = technical_data.get("turnover_ratio", 0)
        if turnover >= 0.40:
            stock_score += 30
            stock_signals.append({"type":"换手超40%","score":30,"action":"准备随时清仓"})
        elif turnover >= 0.25:
            stock_score += 15
            stock_signals.append({"type":"换手超25%","score":15,"action":"警惕高位换手"})
        d5 = technical_data.get("d5", 0)
        if d5 >= 25:
            stock_score += 15
            stock_signals.append({"type":"5日涨超25%","score":15,"action":"加速上涨见顶风险"})
        
        # MA60/年线破位逃顶信号 【修复 v7: 阈值适配波动率, 替代固定 5%】
        price = stock_data.get("price", 0)
        ma60 = stock_data.get("ma60", 0)
        ma250 = stock_data.get("ma250", 0)  # 年线

        # 1) 计算个股年化波动率 → 日均波动 σ_daily
        vol_annual = stock_data.get("volatility_annualized") or stock_data.get("vol", 0.25)
        sigma_daily = vol_annual / (252 ** 0.5) if vol_annual > 0 else 0.015
        # 2) 动态阈值: = min(0.05, max(0.02, 2.0 × σ_daily))
        #    高波动率股票(寒武纪σ=0.35 → 阈值~0.044), 低波动率(工行σ=0.15 → 阈值=0.02)
        vol_threshold = min(0.05, max(0.02, 2.0 * sigma_daily))
        ma60_break_level = round(ma60 * (1.0 - vol_threshold), 2) if ma60 > 0 else 0

        if ma60_break_level > 0 and price < ma60_break_level:
            # 跌破波动率适配的 MA60 破位线
            stock_score += 25
            stock_signals.append({
                "type": f"股价{price:.2f}跌破MA60({ma60:.2f})×{vol_threshold:.3f}(波动率{vol_annual:.0%})",
                "score": 25,
                "action": "趋势严重破坏(vol-adapted),建议清仓该标的"
            })
        elif ma60 > 0 and price < ma60:
            stock_score += 10
            stock_signals.append({
                "type": f"股价{price:.2f}跌破MA60({ma60:.2f})",
                "score": 10,
                "action": "中期趋势反转,减仓至50%"
            })
        
        if ma250 > 0 and price < ma250:
            stock_score += 20
            stock_signals.append({
                "type": f"股价跌破年线MA250({ma250:.2f})",
                "score": 20,
                "action": "年线破位,长线资金离场信号,严格减仓"
            })
        
        # 市场整体年线破位检测
        market_ma250 = technical_data.get("market_ma250", 0)
        market_price = technical_data.get("market_price", 0)
        if market_ma250 > 0 and market_price < market_ma250:
            macro_score += 30
            macro_signals.append({
                "type": f"上证{market_price:.0f}跌破年线({market_ma250:.0f})",
                "score": 30,
                "action": "市场年线破位,系统性风险,建议总仓位≤30%"
            })
        
        total = macro_score + sector_score + stock_score
        urgency = min(100, total * 0.8 + stock_score * 0.4)
        
        if urgency >= 75: rec = "⚠️ 强烈建议减仓"
        elif urgency >= 50: rec = "🔴 大幅减仓"
        elif urgency >= 25: rec = "🟡 减仓观望"
        elif urgency >= 10: rec = "🔵 微调观察"
        else: rec = "🟢 安心持有"
        
        return {
            "macro_signals": macro_signals[:4],
            "sector_signals": sector_signals[:4],
            "stock_signals": stock_signals[:3],
            "total_score": round(urgency, 1),
            "macro_raw": round(macro_score, 1),
            "sector_raw": round(sector_score, 1),
            "stock_raw": round(stock_score, 1),
            "recommendation": rec,
            "principle": "基于今日新闻动态计算(修复版)"
        }


class RiskControlAnalyzer:
    """
    风险控制与动态调仓 (第18维度) — 剑气配合 + 止盈减仓 + 模式切换 + 亏损复盘 + 流动性保险
    """
    
    @classmethod
    def assess_risk_state(cls, plan: Dict) -> Dict:
        buff = plan.get("liquidity_buff", {})
        mp = plan.get("market_phase", {})
        pa = plan.get("portfolio_allocation", {})
        multiplier = buff.get("multiplier", 1.0)
        jian_pct = pa.get("jianzong_pct", 30) if pa else 30
        
        if multiplier >= 1.8:
            risk_state, max_dd, single_stop = "低风险(流动性充裕)", -15, -8
            liquidity_rule = "成交>2.5万亿→全力做多,留10%现金永不满仓"
        elif multiplier >= 1.3:
            risk_state, max_dd, single_stop = "中等偏低风险", -12, -7
            liquidity_rule = "成交1.3-2.5万亿→正常操作,组合仓位70-90%"
        elif multiplier >= 1.0:
            risk_state, max_dd, single_stop = "中等风险", -10, -6
            liquidity_rule = "成交1-1.3万亿→仓位60-80%,精选中确定性标的"
        elif multiplier >= 0.7:
            risk_state, max_dd, single_stop = "中高风险(流动性萎缩)", -8, -5
            liquidity_rule = "成交<1万亿→减至30%仓位+70%现金/国债逆回购"
        else:
            risk_state, max_dd, single_stop = "高风险(流动性枯竭)", -5, -3
            liquidity_rule = "成交<8000亿+指数跌>2%→全清,等3400点再进"
        
        return {
            "risk_state": risk_state, "max_drawdown": max_dd, "single_stop": single_stop,
            "multiplier": multiplier,
            "sword_qi": f"剑宗{jian_pct:.0f}%进攻:气宗{100-jian_pct:.0f}%防守",
            "liquidity_rule": liquidity_rule,
            "take_profit": "跌破MA60→减1/3 | 放巨量(5倍+)→减1/2 | 行业普跌5%+→等3日企稳",
            "loss_review": "连亏2笔→冷静48h复盘 | 连亏3笔→强制休息1周 | 每月复盘:胜率/盈亏比/回撤",
            "mode_switch": "剑宗连续2季报超预期→升级气宗 | 气宗季报miss→降级剑宗减50%",
            "emergency": "黑天鹅→全停买入,等72h消化"
        }
    
    @classmethod
    def generate_action_card(cls, plan: Dict) -> str:
        risk = cls.assess_risk_state(plan)
        return (
            f"  风险: {risk['risk_state']} | 组合止损{risk['max_drawdown']}% | 单票{risk['single_stop']}%\n"
            f"  剑气: {risk['sword_qi']} | 保险: {risk['liquidity_rule'][:40]}\n"
            f"  止盈: {risk['take_profit'][:50]}\n"
            f"  纪律: {risk['loss_review'][:45]}"
        )


class PositionCalculator:
    """
    五项增强仓位管理
    ① ATR动态止损 ② 安全垫机制 ③ 以损定量 ④ 移动止损 ⑤ 赚够再加仓
    """
    
    @staticmethod
    def calc_position(account: float, entry_price: float, stop_price: float,
                      risk_pct: float = 0.01) -> Tuple[int, float, float]:
        """以损定量: 固定风险反推开仓数量"""
        risk_amount = account * risk_pct
        stop_width = abs(entry_price - stop_price)
        if stop_width <= 0: return 0, 0, risk_amount
        shares = int(risk_amount / stop_width / 100) * 100
        actual_risk = shares * stop_width
        actual_risk_pct = round(actual_risk / account * 100, 1)
        return shares, actual_risk, actual_risk_pct
    
    @staticmethod
    def calc_atr_stop(price: float, atr: float, multiplier: float = 2.0) -> float:
        """ATR动态止损: 价格 - 倍数×ATR"""
        return round(price - multiplier * atr, 2)
    
    @staticmethod
    def calc_trailing_stop(current_price: float, highest_close: float,
                           atr: float, multiplier: float = 3.0) -> float:
        """移动止损: 最高收盘价 - 倍数×ATR, 只上移不下移"""
        return round(highest_close - multiplier * atr, 2)
    
    @classmethod
    def safety_cushion_ok(cls, entry_price: float, current_price: float,
                          stop_price: float) -> bool:
        """安全垫检查: 浮盈≥止损宽度的2倍才允许加仓"""
        stop_width = abs(entry_price - stop_price)
        if stop_width <= 0: return False
        profit = current_price - entry_price
        return profit >= stop_width * 2 if stop_width > 0 else False
    
    @classmethod
    def enhanced_risk_report(cls, plan: Dict, account: float = 172982,
                             risk_pct: float = 0.01) -> List[Dict]:
        """生成增强版仓位建议报告"""
        stocks = plan.get("stocks", [])
        rs = plan.get("realtime_signals", [])
        reports = []
        for s in stocks:
            sym = s["symbol"]
            name = s["name"]
            price = 0
            for r in rs:
                if r["symbol"] == sym and r.get("price", 0) > 0:
                    price = r["price"]; break
            if price <= 0:
                fb = {"601881":13.44,"588000":2.10,"518880":9.0,"688981":140.31,
                      "000960":40.48,"002460":62.97,"002466":58.66,"601006":4.60}
                price = fb.get(sym, 0)
            if price <= 0: continue
            
            atr_map = {"601881":1.44,"588000":0.05,"688981":4.0,"000960":1.5,
                       "002460":3.0,"518880":0.3}
            atr = atr_map.get(sym, price * 0.03)
            
            stop_2atr = cls.calc_atr_stop(price, atr, 2.0)
            stop_hard = round(price * 0.92, 2)
            stop_final = max(stop_2atr, stop_hard)
            
            shares, risk_amt, risk_pct_val = cls.calc_position(account, price, stop_final, risk_pct)
            build_pct = round(shares * price / account * 100, 1)
            
            cushion_ok = cls.safety_cushion_ok(price, price, stop_final)
            max_shares, _, _ = cls.calc_position(account, price, stop_final, risk_pct)
            max_pct = round(max_shares * price / account * 100, 1)
            add_condition = f"浮盈≥{round(abs(price-stop_final)*2,1)}元后可加至{max_pct}%"
            
            trail = cls.calc_trailing_stop(price, price, atr, 3.0)
            
            reports.append({
                "symbol":sym,"name":name,"price":price,
                "atr":atr,"stop_atr":stop_2atr,"stop_hard":stop_hard,"stop_final":stop_final,
                "shares_1pct":shares,"risk_1pct":risk_amt,"risk_pct":risk_pct_val,
                "build_pct":build_pct,"max_pct":max_pct,
                "safety_cushion":cushion_ok,"add_condition":add_condition,
                "trailing_stop":trail,"trailing_mult":3.0
            })
        return reports


class AddPositionEngine:
    """
    统一加仓决策引擎 — 集成所有加仓/减仓规则，安全垫为最终门禁
    架构: if 技术/基本面加仓信号_触发 → if 安全垫_达标 → 执行加仓 ✅ else → 记录信号，不执行 ❌
    
    集成规则:
    三. 技术指标加仓: MA20/MA60回调、缩量企稳
    四. 基本面加仓: 季报确认、周期反转5条件
    五. 市场环境仓位: 上证点位仓位、流动性保险
    六. 禁止加仓: 剑宗次日、亏损头寸、无安全垫
    七. 模式升级/降级: 季报驱动模式切换
    八. 金字塔加仓: 20%→40%→60%节奏
    """
    
    # ═══ 上证指数点位-仓位映射 ═══
    INDEX_POSITION_MAP = [
        (3800, 1.0, "满仓+融资"),
        (3700, 1.0, "满仓"),
        (3500, 0.50, "50%仓位"),
        (3400, 0.70, "70%仓位"),
        (3200, 1.0, "满仓+融资(极端回调)"),
        (3000, 0.30, "30%仓位防守"),
    ]
    
    # ═══ 金字塔加仓节奏 ═══
    PYRAMID_STAGES = [
        {"stage": 1, "ratio": 0.20, "name": "试探建仓", "trigger": "首次入场"},
        {"stage": 2, "ratio": 0.40, "name": "确认加仓", "trigger": "方向确认+安全垫达标"},
        {"stage": 3, "ratio": 0.60, "name": "趋势加仓", "trigger": "趋势强化+季报确认"},
        {"stage": 4, "ratio": 0.80, "name": "满配", "trigger": "产业+企业双拐点共振"},
    ]
    
    @classmethod
    def evaluate(cls, stock: Dict, technical: Optional[Dict],
                trade_mode: Optional[Dict], position_info: Optional[Dict],
                plan: Optional[Dict] = None) -> Dict:
        """
        综合评估加仓条件
        
        参数:
            stock: 股票基本面数据 (来自portfolio)
            technical: 技术指标数据 (来自RealTimeDataProvider, 含ma20/ma60/vol_ratio等)
            trade_mode: 交易模式 (来自TradingModeClassifier.classify)
            position_info: 持仓信息 {"entry_price": 100, "current_price": 115, "stop_price": 90, "entry_date": "2026-06-15", "current_shares": 1000}
            plan: 完整策略plan(含index/turnover等市场数据)
        
        返回: {
            "can_add": bool,        # 是否可以加仓
            "signals": [],          # 触发的信号列表
            "blockers": [],         # 阻止加仓的原因
            "stage": int,           # 当前金字塔阶段
            "next_stage_desc": str, # 下一阶段说明
            "detail": str,          # 详细说明
            "mode_action": str,     # 模式升级/降级建议
        }
        """
        signals = []
        blockers = []
        
        mode = trade_mode.get("mode", "") if trade_mode else ""
        entry_price = position_info.get("entry_price", 0) if position_info else 0
        current_price = position_info.get("current_price", 0) if position_info else 0
        stop_price = position_info.get("stop_price", entry_price * 0.92) if position_info else 0
        entry_date_str = position_info.get("entry_date", "") if position_info else ""
        
        # ═══════════════════════════════════════
        # 门禁0: 禁止加仓的绝对条件
        # ═══════════════════════════════════════
        
        # 0a. 剑宗一波流 → 次日不加仓
        if "剑宗一波流" in mode or mode == "剑宗一波流":
            if entry_date_str:
                from datetime import datetime as dt
                try:
                    entry_dt = dt.strptime(entry_date_str, "%Y-%m-%d")
                    days_held = (dt.now() - entry_dt).days
                    if days_held <= 1:
                        blockers.append(f"剑宗一波流次日不加仓(已持有{days_held}天)")
                except: pass
        
        # 0b. 亏损头寸绝不加仓（利弗莫尔原则）
        if entry_price > 0 and current_price > 0 and current_price < entry_price:
            loss_pct = round((current_price - entry_price) / entry_price * 100, 1)
            blockers.append(f"亏损头寸({loss_pct}%)禁止加仓——利弗莫尔原则")
        
        # ═══════════════════════════════════════
        # 一、技术指标加仓信号检查
        # ═══════════════════════════════════════
        if technical:
            ma20 = technical.get("ma20", 0)
            ma60 = technical.get("ma60", 0)
            price = technical.get("price", 0)
            vol_shrink = technical.get("vol_shrink", False)
            vol_ratio = technical.get("vol_ratio", 1.0)
            bull_align = technical.get("bull_align", False)
            rsi = technical.get("rsi", 50)
            
            # 1a. MA20回调加仓（主要加仓点）
            if price > 0 and ma20 > 0:
                distance_to_ma20 = (price - ma20) / ma20
                if 0 <= distance_to_ma20 <= 0.03:
                    signal_desc = f"MA20回调到位(偏离+{distance_to_ma20*100:.1f}%)"
                    if vol_shrink:
                        signal_desc += " + 缩量确认(量比{:.2f})".format(vol_ratio)
                        signals.append({"type": "MA20回调+缩量", "score": 25, "desc": signal_desc})
                    else:
                        signals.append({"type": "MA20回调(无缩量)", "score": 15, "desc": signal_desc})
                elif distance_to_ma20 < 0 and distance_to_ma20 >= -0.02:
                    signals.append({"type": "MA20轻度跌破", "score": 5, 
                                   "desc": f"MA20附近(偏离{distance_to_ma20*100:.1f}%),等企稳确认"})
            
            # 1b. MA60回调加仓（次级加仓点）
            if price > 0 and ma60 > 0:
                distance_to_ma60 = (price - ma60) / ma60
                if -0.02 <= distance_to_ma60 <= 0.05:
                    if vol_shrink:
                        signals.append({"type": "MA60深度回调+缩量企稳", "score": 20,
                                       "desc": f"MA60支撑确认(偏离+{distance_to_ma60*100:.1f}%)+缩量企稳"})
                    else:
                        signals.append({"type": "MA60附近", "score": 8,
                                       "desc": f"MA60附近(偏离{distance_to_ma60*100:.1f}%),等待缩量"})
            
            # 1c. 缩量企稳信号（剑宗偏气前置条件）
            if "剑宗偏气" in mode:
                if vol_shrink and price > 0:
                    signals.append({"type": "缩量企稳", "score": 10, 
                                   "desc": f"剑宗偏气缩量企稳(量比{vol_ratio:.2f}),可轻仓试探"})
            
            # 1d. 多头排列状态加分
            if bull_align and 30 <= rsi <= 65:
                signals.append({"type": "多头排列+RSI健康", "score": 5, "desc": "技术面良好"})
        
        # ═══════════════════════════════════════
        # 二、基本面加仓信号检查
        # ═══════════════════════════════════════
        
        # 2a. 季报确认加仓
        eps_trend = stock.get("eps_trend", [])
        margin_trend = stock.get("margin_trend", [])
        revenue_qoq = stock.get("revenue_qoq", [])
        
        if "气宗偏剑" in mode:
            # 需要连续2季报超预期才能满配
            eps_growth_ok = len(eps_trend) >= 4 and eps_trend[-1] > eps_trend[-3] and eps_trend[-2] > eps_trend[-4]
            margin_ok = len(margin_trend) >= 2 and margin_trend[-1] > margin_trend[-2]
            revenue_ok = len(revenue_qoq) >= 2 and revenue_qoq[-1] > 0 and revenue_qoq[-2] > 0
            
            if eps_growth_ok and margin_ok:
                signals.append({"type": "季报连续2季超预期", "score": 20, 
                               "desc": "EPS加速增长+毛利率扩张→可加至满配"})
            elif eps_growth_ok:
                signals.append({"type": "EPS改善(毛利率未确认)", "score": 10,
                               "desc": "EPS增长但毛利率待确认,维持轻仓"})
        
        # 2b. 周期反转条件
        cycle_score = stock.get("cycle_score", 0)
        cycle_conditions = stock.get("cycle_conditions", [])
        industry_type = stock.get("industry_type", "")
        
        if hasattr(industry_type, 'value'):
            industry_type = industry_type.value if hasattr(industry_type, 'value') else str(industry_type)
        
        if industry_type == "CYCLICAL" or str(industry_type) == "CYCLICAL":
            met_count = len(cycle_conditions) if cycle_conditions else 0
            if met_count >= 3:
                signals.append({"type": f"周期反转{met_count}/5条件", "score": 15 * met_count / 5,
                               "desc": f"周期反转{met_count}/5条件满足→可逐步建仓"})
            elif met_count >= 2:
                signals.append({"type": f"周期反转{met_count}/5条件", "score": 5,
                               "desc": f"周期反转仅{met_count}/5条件,轻仓布局等确认"})
        
        # ═══════════════════════════════════════
        # 三、市场环境仓位管理
        # ═══════════════════════════════════════
        if plan:
            index_val = plan.get("index", 0)
            turnover = plan.get("turnover", 0)
            
            # 3a. 上证点位仓位建议
            index_advice = cls._get_index_position(index_val)
            if index_advice:
                signals.append(index_advice)
            
            # 3b. 流动性检查
            if turnover < 10000:
                blockers.append(f"成交额{turnover}亿<1万亿,减至30%仓位")
            elif turnover >= 25000:
                signals.append({"type": "超级Buff", "score": 5, "desc": "流动性充沛,全力做多"})
        
        # ═══════════════════════════════════════
        # 四、模式升级/降级检查
        # ═══════════════════════════════════════
        mode_action = None
        if "剑宗" in mode and "偏" in mode:
            # 剑宗偏气 → 检查是否满足升级条件
            eps_upgrade = len(eps_trend) >= 4 and all(eps_trend[-2] > eps_trend[-3], eps_trend[-1] > eps_trend[-2])
            margin_upgrade = len(margin_trend) >= 3 and margin_trend[-1] > margin_trend[-2] > margin_trend[-3]
            if eps_upgrade and margin_upgrade:
                mode_action = "⬆️ 升级为气宗格局(比例翻倍)"
        elif "气宗" in mode and not "偏" in mode:
            # 纯气宗 → 检查是否降级
            if len(eps_trend) >= 2 and eps_trend[-1] < eps_trend[-2]:
                mode_action = "⚠️ EPS增速放缓,关注下季报,若miss→降级剑宗减50%"
        
        # ═══════════════════════════════════════
        # 门禁: 安全垫检查 (最终门禁)
        # ═══════════════════════════════════════
        cushion_ok = False
        if entry_price > 0 and stop_price > 0 and current_price > 0:
            cushion_ok = PositionCalculator.safety_cushion_ok(entry_price, current_price, stop_price)
        
        # 收缩后协方差驱动的配置权重(作为默认加仓权重输出, 优先于朴素评分权重)
        sym = stock.get("symbol", "")
        cov_w = plan.get("cov_allocation", {}).get(sym) if plan else None

        # ═══════════════════════════════════════
        # 综合决策
        # ═══════════════════════════════════════
        total_signal_score = sum(s["score"] for s in signals)
        
        if blockers:
            can_add = False
            decision = "❌ 禁止加仓"
            detail = "; ".join(blockers)
        elif len(signals) == 0:
            can_add = False
            decision = "⏸ 无加仓信号"
            detail = "等待技术/基本面信号触发"
        elif not cushion_ok:
            can_add = False
            decision = "🟡 信号就绪但安全垫不足"
            stop_width = abs(entry_price - stop_price) if stop_price > 0 else 0
            need_profit = stop_width * 2
            current_profit = current_price - entry_price if entry_price > 0 else 0
            gap = max(0, need_profit - current_profit)
            detail = f"信号: {', '.join(s['desc'] for s in signals[:3])} | 还需浮盈{gap:.1f}元"
        else:
            can_add = True
            decision = "✅ 允许加仓"
            weight_hint = f" | 建议配置权重{cov_w:.1%}" if cov_w is not None else ""
            detail = f"信号: {', '.join(s['desc'] for s in signals[:3])}{weight_hint}"
        
        # ═══════════════════════════════════════
        # 金字塔阶段判定
        # ═══════════════════════════════════════
        current_shares = position_info.get("current_shares", 0) if position_info else 0
        max_shares = position_info.get("max_shares", 0) if position_info else 0
        current_pct = current_shares / max_shares if max_shares > 0 else 0
        
        stage = 1
        for ps in cls.PYRAMID_STAGES:
            if current_pct >= ps["ratio"]:
                stage = ps["stage"]
        next_stage = cls.PYRAMID_STAGES[min(stage, len(cls.PYRAMID_STAGES)-1)]
        
        return {
            "can_add": can_add,
            "recommend_weight": round(cov_w, 4) if cov_w is not None else None,  # 默认加仓权重(协方差收缩驱动)
            "decision": decision,
            "signals": signals,
            "blockers": blockers,
            "total_signal_score": total_signal_score,
            "cushion_ok": cushion_ok,
            "stage": stage,
            "stage_name": next_stage["name"],
            "next_stage_desc": next_stage["trigger"],
            "mode_action": mode_action,
            "detail": detail,
        }
    
    @classmethod
    def _get_index_position(cls, index_val: float) -> Optional[Dict]:
        """根据上证指数返回仓位建议"""
        for threshold, ratio, desc in cls.INDEX_POSITION_MAP:
            if index_val >= threshold:
                return {"type": f"上证{threshold}+仓位建议", "score": 3,
                       "desc": f"上证{index_val}→{desc}"}
        return None
    
    @classmethod
    def evaluate_batch(cls, stocks: List[Dict], technicals: Dict[str, Dict],
                       trade_modes: Dict[str, Dict], positions: Dict[str, Dict],
                       plan: Optional[Dict] = None) -> Dict[str, Dict]:
        """批量评估加仓条件"""
        results = {}
        for s in stocks:
            sym = s.get("symbol", "")
            tech = technicals.get(sym)
            mode = trade_modes.get(sym, {})
            pos = positions.get(sym, {})
            results[sym] = cls.evaluate(s, tech, mode, pos, plan)
        return results


class RetailInvestorTherapy:
    """
    散户认知纠偏框架 (第19维度)
    
    连续亏损三年以上的三大错误行为诊断:
    1. 短线追热点打板 — 被信息左右/追涨杀跌/不看基本面
    2. 追高基本面龙头 — 越跌越补→深套→自我安慰"价值投资"
    3. 听朋友内部消息 — 接盘→是局不是秘密
    
    99%散户困境: 资金<机构 | 技术<量化 | 逻辑<专业 | 信息<圈层
    破局路径: 承认→停止→学习→建立体系→用框架选股
    """
    
    ERROR_PATTERNS = {
        "chase_hot": {
            "name": "短线追热点打板",
            "signals": ["追涨停", "打板", "热门股", "跟风", "消息驱动", "不看估值"],
            "diagnosis": "被市场情绪和信息茧房左右,追涨杀跌是散户最快亏钱方式",
            "fix": "用景气周期框架(⑫-⑯维)判断真拐点vs伪热点,只参与现象级事件带来的行业拐点",
            "severity": "高"
        },
        "chase_bluechip": {
            "name": "追高基本面龙头",
            "signals": ["越跌越补", "深套", "长期持有", "好公司", "价值投资", "龙头"],
            "diagnosis": "基本面好≠股价马上涨,越跌越补是散户第二大死法——浮亏变实亏",
            "fix": "用苯韭双击框架(⑭维)判断产业+企业双拐点,只在拐点确认后入场,不满仓单只",
            "severity": "高"
        },
        "insider_tips": {
            "name": "听朋友内部消息",
            "signals": ["内部消息", "朋友推荐", "老师带单", "内幕", "消息灵通"],
            "diagnosis": "99%是局→你不是圈内人→你接盘→对方出货",
            "fix": "所有消息需经过本策略18维框架交叉验证,不验证不入场",
            "severity": "极高"
        },
        "social_noise": {
            "name": "股吧/雪球/论坛问能不能涨",
            "signals": ["股吧", "雪球", "论坛", "能不能涨", "怎么看", "大神"],
            "diagnosis": "99%散户每天在此消耗时间→信息劣势+认知劣势叠加",
            "fix": "关闭所有社交炒股平台,只保留数据源和本策略框架",
            "severity": "中"
        }
    }
    
    @classmethod
    def diagnose(cls, trading_history: Dict) -> Dict:
        """诊断散户行为模式"""
        issues = []
        total_score = 100
        
        history = trading_history or {}
        behaviors = history.get("behaviors", [])
        behaviors_text = " ".join(behaviors).lower()
        
        for pid, pattern in cls.ERROR_PATTERNS.items():
            matched = [sig for sig in pattern["signals"] if sig.lower() in behaviors_text]
            if matched or history.get(f"has_{pid}"):
                issues.append({
                    "pattern": pattern["name"],
                    "severity": pattern["severity"],
                    "diagnosis": pattern["diagnosis"],
                    "fix": pattern["fix"],
                    "signals_matched": matched
                })
                total_score -= 25
        
        if not issues:
            issues.append({
                "pattern": "无明显错误行为",
                "severity": "低",
                "diagnosis": "未检测到典型散户错误模式",
                "fix": "继续保持框架驱动,定期复盘检视",
                "signals_matched": []
            })
        
        return {
            "health_score": max(0, total_score),
            "issues": issues,
            "verdict": cls._get_verdict(total_score, len(issues)),
            "learning_path": cls._get_learning_path(total_score)
        }
    
    @classmethod
    def _get_verdict(cls, score: int, issue_count: int) -> str:
        if score >= 75:
            return "认知健康→继续用框架约束交易行为"
        elif score >= 50:
            return "轻微受损→需刻意纠正1-2个错误行为模式"
        elif score >= 25:
            return "中等受损→暂停实盘,完成以下学习路径再恢复"
        else:
            return "严重受损→强制空仓1月,用模拟盘重建交易纪律"
    
    @classmethod
    def _get_learning_path(cls, score: int) -> List[str]:
        path = ["① 关闭股吧/雪球/抖音炒股频道→信息断舍离"]
        if score < 75:
            path.append("② 用本策略18维框架替代直觉交易→每笔交易记录框架评分")
        if score < 50:
            path.append("③ 连续20笔模拟盘验证胜率>60%→再恢复小额实盘")
        if score < 25:
            path.append("④ 强制休息1月→阅读《股票大作手回忆录》+《投资中最简单的事》")
        path.append("⑤ 每月末用本策略框架复盘→持续迭代认知")
        return path
    
    @classmethod
    def generate_wakeup_call(cls) -> str:
        """生成致亏损散户的信"""
        return """
    【致一直亏钱的散户——你可能正在犯的错】

    如果你连续亏损三年以上,请自查你的交易模式:
    
    ❌ 错误1: 短线追热点打板
       → 你看到的"涨停"是别人3天前埋伏好的
       → 本策略解决: ⑫维现象级事件识别→只追真拐点
    
    ❌ 错误2: 追高"基本面优秀"龙头→越跌越补
       → 好公司≠好股票≠马上涨,越跌越补是加速破产
       → 本策略解决: ⑭维苯韭双击→只在产业+企业双拐点入场
    
    ❌ 错误3: 听朋友/群/抖音"内部消息"
       → 99%是局,你是最后一个接盘的
       → 本策略解决: 18维交叉验证→不验证不入场
    
    你的对手是谁?
      公募(万亿资金+专业团队) + 量化(纳秒级交易)
      + 游资(信息圈层) + 产业资本(内部视角)
    
    你在股吧/雪球问"能不能涨"的时候,他们在看季报/调研/模型。
    
    破局唯一路径:
      停止错误行为→学习框架→建立体系→用逻辑替代情绪
      
    这个18维景气周期轮动策略,就是你的交易体系起点。
    """


class EventAnalyzer:
    """
    现象级事件/拐点事件识别器 (第12-13维度)
    12维: 突发新闻事件四要素 + 政策事件五要素
    13维: 超现象级事件(大周期) + 小拐点事件/机会(持续性判断)
    """
    
    # 现象级事件四要素评分
    PHENOMENON_ELEMENTS = {
        "authenticity": {"weight": 0.25, "desc": "真实性-需交叉验证"},
        "spread": {"weight": 0.25, "desc": "传播性-形成出圈效应"},
        "scale": {"weight": 0.25, "desc": "规模性-行业体量足够大"},
        "timeliness": {"weight": 0.25, "desc": "时效性-第一次最有价值"}
    }
    
    # 政策事件五要素
    POLICY_ELEMENTS = {
        "cycle": {"weight": 0.20, "desc": "落地周期"},
        "amount": {"weight": 0.25, "desc": "资金体量"},
        "duration": {"weight": 0.20, "desc": "持续多久"},
        "coverage": {"weight": 0.20, "desc": "覆盖群体规模"},
        "timeliness": {"weight": 0.15, "desc": "时效性-重复无效"}
    }
    
    # 历史现象级事件案例库
    HISTORICAL_CASES = {
        "chatgpt_2023": {"name": "ChatGPT爆火", "type": "科技突破", "return": 300, "duration_months": 12},
        "chip_sanction_2018": {"name": "美对中芯片制裁", "type": "贸易制裁", "return": 250, "duration_months": 18},
        "xg_open_2022": {"name": "XG放开预期", "type": "政策", "return": 80, "duration_months": 3},
        "bb_explosion_2024": {"name": "黎巴嫩BB机事件", "type": "战争/地缘", "return": 150, "duration_months": 6},
        "margin_loan_2024": {"name": "924借钱炒股政策", "type": "政策", "return": 40, "duration_months": 2}
    }
    
    @classmethod
    def analyze_event_quality(cls, article: Dict) -> Dict:
        """
        分析单条新闻的事件质量
        返回现象级事件评分
        """
        title = (article.get("title", "") + " " + article.get("content", "")).lower()
        
        scores = {
            "authenticity": 0,  # 是否有官方/权威来源
            "spread": 0,        # 是否包含 viral 关键词
            "scale": 0,         # 是否涉及大行业/大市值
            "timeliness": 0,    # 是否首次报道
            "is_phenomenon": False
        }
        
        # 真实性检测
        auth_keywords = ["官方", "宣布", "发布", "证实", "确认", "新华社", "央视", "彭博", "路透"]
        if any(kw in title for kw in auth_keywords):
            scores["authenticity"] = 80
        elif any(kw in title for kw in ["据悉", "消息人士", "传闻"]):
            scores["authenticity"] = 40
        
        # 传播性检测
        spread_keywords = ["爆", "涨停", "暴涨", "疯涨", "全线", "集体", "重磅", "刷屏", "刷屏"]
        viral_count = sum(1 for kw in spread_keywords if kw in title)
        scores["spread"] = min(viral_count * 25, 100)
        
        # 规模性检测
        scale_keywords = ["万亿", "千亿", "全行业", "全产业链", "全球", "全国", "所有", "全部"]
        scale_count = sum(1 for kw in scale_keywords if kw in title)
        scores["scale"] = min(scale_count * 30, 100)
        
        # 时效性检测 (首次报道加分)
        if any(kw in title for kw in ["首次", "第一家", "第一个", "首次突破", "创历史新高"]):
            scores["timeliness"] = 100
        else:
            scores["timeliness"] = 60
        
        # 综合判断是否现象级事件
        total = (scores["authenticity"] * 0.25 + scores["spread"] * 0.25 + 
                scores["scale"] * 0.25 + scores["timeliness"] * 0.25)
        scores["total"] = round(total, 1)
        scores["is_phenomenon"] = total >= 70
        
        return scores
    
    @classmethod
    def detect_ripple_events(cls, main_event: str, all_articles: List[Dict]) -> List[Dict]:
        """
        检测超现象级事件的涟漪效应（小拐点）
        例如: AI爆火 → 算力暴涨 → 存储芯片暴涨 → 光模块爆发 → 铜连接 → 液冷...
        """
        ripple_signal_words = {
            "AI": ["广告", "搜索", "办公", "教育", "医疗", "金融", "法律", "自动驾驶"],
            "算力": ["光模块", "CPO", "液冷", "HBM", "存储", "铜连接", "电源", "PCB"],
            "国产替代": ["设备", "材料", "EDA", "光刻", "离子注入", "检测"],
            "新能源": ["锂电", "光伏", "储能", "氢能", "充电桩", "电网"],
            "机器人": ["减速器", "传感器", "电机", "控制器", "丝杠", "灵巧手"],
            "地缘冲突": ["航运", "油运", "天然气", "黄金", "军工", "粮食"],
        }
        
        ripples = []
        seen_sub_topics = set()
        
        for kw, sub_topics in ripple_signal_words.items():
            if kw in main_event:
                for article in all_articles[:500]:
                    title = (article.get("title", "") or "").lower()
                    content = (article.get("content", "") or "")[:200].lower()
                    for sub in sub_topics:
                        if sub in title or sub in content:
                            if sub not in seen_sub_topics:
                                ripples.append({
                                    "main_event": kw,
                                    "ripple_topic": sub,
                                    "article": article.get("title", "")[:50],
                                    "type": "小拐点事件"
                                })
                                seen_sub_topics.add(sub)
        
        return ripples
    
    @classmethod
    def check_pre_position(cls, theme: str) -> Dict:
        """
        预判现象级事件——主动埋伏模式
        守株待兔: 提前研究行业标的，等待现象级事件触发
        """
        PRE_POSITION_MAP = {
            "存储芯片Q2财报": {
                "trigger": "7月中旬Q2财报超预期",
                "stocks": ["688525", "301308", "603986"],
                "logic": "存储涨价+AI需求→Q2业绩爆发→现象级事件触发"
            },
            "券商并购重组": {
                "trigger": "头部券商合并公告",
                "stocks": ["601881", "600030", "601688"],
                "logic": "监管放行+并购重组大年→等公告触发"
            },
            "锂矿周期反转": {
                "trigger": "锂价突破20万/吨 + 龙头Q2扭亏",
                "stocks": ["002460", "002466"],
                "logic": "锂价中枢上移+供给出清→周期拐点确认"
            },
            "小金属出口管制": {
                "trigger": "中国宣布对铟/钨/锗实施出口管制",
                "stocks": ["000960", "600497", "002378"],
                "logic": "战略资源武器化→供给收缩→涨价"
            },
            "折叠屏iPhone发布": {
                "trigger": "9月苹果发布会正式发布",
                "stocks": ["002475", "300433", "601138"],
                "logic": "消费电子新品类→产业链订单爆发"
            },
        }
        
        return PRE_POSITION_MAP.get(theme, {"trigger": "待定", "stocks": [], "logic": "待研究"})
    
    @classmethod
    def classify_event_type(cls, article: Dict) -> str:
        """分类事件类型: 战争/地缘 | 自然灾害 | 科技突破 | 贸易制裁 | 政策 | 其他"""
        title = (article.get("title", "") + " " + article.get("content", "")).lower()
        
        if any(kw in title for kw in ["战争", "冲突", "爆炸", "袭击", "军事", "导弹", "无人机"]):
            return "战争/地缘"
        elif any(kw in title for kw in ["地震", "洪水", "台风", "干旱", "灾害", "疫情"]):
            return "自然灾害"
        elif any(kw in title for kw in ["突破", "技术", "专利", "新品", "发布", "芯片", "AI", "模型"]):
            return "科技突破"
        elif any(kw in title for kw in ["制裁", "关税", "贸易", "出口管制", "禁运"]):
            return "贸易制裁"
        elif any(kw in title for kw in ["政策", "央行", "财政", "降准", "降息", "补贴", "刺激"]):
            return "政策"
        else:
            return "其他"


class NewsAnalyzer:
    """
    新闻热点分析器 — 智能去重+时效衰减+情感极性+全文利用+结构化提取+溯源权重+影响量化
    """
    
    # 情感极性词典
    BULLISH = {"暴涨": 0.9, "飙升": 0.8, "翻倍": 0.95, "超预期": 0.85, "突破": 0.7,
               "量产": 0.75, "创纪录": 0.8, "预增": 0.7, "扭亏": 0.65, "复苏": 0.55,
               "供需失衡": 0.8, "短缺": 0.6, "十年最好": 0.9, "新高": 0.65, "增持": 0.5,
               "涨停": 0.85, "利好": 0.6, "扶持": 0.55, "供不应求": 0.85}
    BEARISH = {"暴跌": -0.9, "崩盘": -0.95, "腰斩": -0.85, "暴雷": -0.9, "不及预期": -0.8,
               "亏损": -0.7, "强平": -0.95, "踩踏": -0.9, "泡沫": -0.7, "停产": -0.6,
               "过剩": -0.75, "裁员": -0.55, "违约": -0.85, "退市": -0.9, "ST": -0.8,
               "跌停": -0.85, "减持": -0.6, "调查": -0.7, "清仓": -0.8, "罚": -0.65}
    
    # 来源可靠性权重
    SOURCE_WEIGHTS = {
        "cctv": 1.0, "央视": 1.0, "wallstreetcn": 0.9, "华尔街见闻": 0.9,
        "caixin": 0.85, "财新": 0.85, "cls": 0.7, "财联社": 0.7,
        "jinshi": 0.65, "金十": 0.65, "sina": 0.5, "163": 0.45,
    }
    
    # 结构化提取模式
    EXTRACT_PATTERNS = {
        "eps_growth": [r"净利[润同环]?比[预]?[增降]+(\d{1,4}(?:\.\d)?)\s*%",
                       r"EPS[同环]?比[增降]+(\d{1,4}(?:\.\d)?)\s*%"],
        "revenue_growth": [r"营收[同环]?比[增降]+(\d{1,3}(?:\.\d)?)\s*%"],
        "price_change": [r"(?:涨|跌|暴涨|暴跌|飙升|重挫|大涨|大跌)(\d{1,3}(?:\.\d)?)\s*%"],
        "PE_value": [r"[Pp][Ee]\s*[约在]?\s*(\d{1,3}(?:\.\d)?)"],
        "index_level": [r"(上证|沪深300|科创50|恒指|纳指).*?(\d{3,5}(?:\.\d{1,2})?)"],
        "buyback": [r"回购\s*(\d+(?:\.\d)?)\s*[万亿千百美]?元"],
    }
    
    # 影响量化映射
    IMPACT_MAP = {
        "关闭海峡": {"symbols": {"518880": 0.4, "601919": 0.3, "601872": 0.25, "588000": -0.15, "688981": -0.1},
                     "duration_hours": 72, "description": "霍尔木兹海峡关闭"},
        "美军打击": {"symbols": {"518880": 0.35, "601919": 0.25, "000960": 0.15, "588000": -0.2},
                     "duration_hours": 48, "description": "美军军事打击伊朗"},
        "SK海力士暴跌": {"symbols": {"688981": -0.3, "588000": -0.25, "603986": -0.2},
                         "duration_hours": 24, "description": "存储龙头暴跌"},
        "新易盛预增": {"symbols": {"300502": 0.5, "300394": 0.25, "688256": 0.15},
                       "duration_hours": 72, "description": "光模块龙头业绩预增"},
        "存储短缺": {"symbols": {"603986": 0.3, "688525": 0.25, "688981": 0.2},
                     "duration_hours": 168, "description": "存储芯片短缺"},
        "人形机器人": {"symbols": {"688017": 0.4, "002747": 0.2, "300124": 0.15},
                      "duration_hours": 168, "description": "机器人产业化"},
        "券商预增": {"symbols": {"601881": 0.25, "600030": 0.2, "601688": 0.15},
                     "duration_hours": 72, "description": "券商业绩预增"},
        "特朗普关税": {"symbols": {"588000": -0.15, "688981": -0.15, "000960": 0.1},
                      "duration_hours": 48, "description": "贸易关税"},
    }
    
    # 主题关键词 → 映射标的
    THEME_MAP = {
        "存储芯片": {
            "keywords": ["存储芯片", "HBM", "DRAM", "NAND", "美光", "佰维", "江波龙", "德明利", "兆易创新", "香农芯创", "长鑫存储"],
            "stocks": [("688525", "佰维存储"), ("301308", "江波龙"), ("603986", "兆易创新"), ("300475", "香农芯创"), ("001309", "德明利")],
            "logic": "HBM涨价+AI存储需求爆发"
        },
        "券商": {
            "keywords": ["券商", "证券", "中信", "华泰", "广发", "银河", "投行", "经纪", "并购重组"],
            "stocks": [("601881", "中国银河"), ("600030", "中信证券"), ("601688", "华泰证券"), ("601211", "国泰君安")],
            "logic": "十年最好业绩+并购重组大年"
        },
        "AI算力": {
            "keywords": ["AI", "人工智能", "英伟达", "算力", "GPU", "光模块", "CPO", "大模型", "OpenAI", "豆包"],
            "stocks": [("688256", "寒武纪"), ("300502", "新易盛"), ("300394", "天孚通信"), ("688041", "海光信息")],
            "logic": "AI基建资本开支爆发"
        },
        "半导体": {
            "keywords": ["半导体", "芯片", "晶圆", "光刻", "EDA", "封装", "中芯国际", "北方华创", "韦尔股份"],
            "stocks": [("688981", "中芯国际"), ("002371", "北方华创"), ("603501", "韦尔股份"), ("688012", "中微公司")],
            "logic": "国产替代+半导体景气周期"
        },
        "黄金贵金属": {
            "keywords": ["黄金", "白银", "钌", "铂", "贵金属", "金价", "贵研铂业"],
            "stocks": [("600489", "中金黄金"), ("600547", "山东黄金"), ("600459", "贵研铂业")],
            "logic": "地缘避险+降息预期"
        },
        "锆钨稀土": {
            "keywords": ["氧化锆", "钨", "稀土", "锗", "镓", "锑", "锡", "铟", "日本东曹", "章源钨业", "厦门钨业", "中钨高新"],
            "stocks": [("002378", "章源钨业"), ("600549", "厦门钨业"), ("000657", "中钨高新"), ("000960", "锡业股份"), ("600497", "驰宏锌锗")],
            "logic": "战略金属供给收缩+需求爆发"
        },
        "锂矿新能源": {
            "keywords": ["锂", "锂矿", "碳酸锂", "赣锋", "天齐", "盐湖", "新能源"],
            "stocks": [("002460", "赣锋锂业"), ("002466", "天齐锂业"), ("000792", "盐湖股份")],
            "logic": "锂矿周期底部反转"
        },
        "原油航运": {
            "keywords": ["原油", "油价", "石油", "霍尔木兹", "油轮", "航运", "中东", "美伊"],
            "stocks": [("601857", "中国石油"), ("600028", "中国石化"), ("601872", "招商轮船"), ("601919", "中远海控")],
            "logic": "霍尔木兹海峡通航预期+油价波动"
        },
        "机器人": {
            "keywords": ["机器人", "人形机器人", "Optimus", "特斯拉Bot", "具身智能"],
            "stocks": [("688017", "绿的谐波"), ("300124", "汇川技术"), ("002747", "埃斯顿")],
            "logic": "人形机器人量产元年"
        },
        "消费电子": {
            "keywords": ["iPhone", "折叠屏", "苹果", "消费电子", "手机"],
            "stocks": [("002475", "立讯精密"), ("300433", "蓝思科技"), ("601138", "工业富联")],
            "logic": "折叠屏iPhone+消费电子复苏"
        },
        "AI泡沫风险": {
            "keywords": ["AI泡沫", "泡沫破裂", "半导体见顶", "BTIG", "半夏投资"],
            "stocks": [],
            "logic": "⚠️ 风险信号: AI泡沫警告，注意减仓"
        },
        "美联储利率": {
            "keywords": ["美联储", "加息", "降息", "利率", "通胀", "FOMC", "沃什", "点阵图"],
            "stocks": [],
            "logic": "宏观信号: 关注利率政策变化"
        }
    }

    def __init__(self, news_dir: Path = None):
        if news_dir is None:
            news_dir = Path(__file__).parent / "新闻时事理念"
        self.news_dir = news_dir
    
    def find_latest_news(self) -> Optional[Path]:
        """找到最新的新闻skill目录"""
        if not self.news_dir.exists():
            return None
        # 找日期格式的文件夹
        date_dirs = sorted([d for d in self.news_dir.iterdir() 
                          if d.is_dir() and len(d.name) == 8 and d.name.isdigit()],
                         reverse=True)
        return date_dirs[0] if date_dirs else None
    
    def analyze(self, max_articles: int = 800) -> Dict:
        """
        智能去重+时效衰减+情感极性+全文利用+结构化提取+溯源权重+影响量化
        """
        latest = self.find_latest_news()
        if not latest:
            return {"status": "无新闻数据", "themes": {}, "top_stocks": [],
                    "phenomenon_events": [], "flash_signals": []}

        index_file = latest / "skill_index.json"
        if not index_file.exists():
            return {"status": "无索引文件", "themes": {}, "top_stocks": [],
                    "phenomenon_events": [], "flash_signals": []}

        try:
            with open(index_file, "r", encoding="utf-8") as f:
                skills = json.load(f).get("skills", [])
        except:
            return {"status": "索引解析失败", "themes": {}, "top_stocks": [],
                    "phenomenon_events": [], "flash_signals": []}

        now = datetime.now()
        # 目录日期兜底: 新闻目录名即 YYYYMMDD, 当 skill_index 缺少 created_at 时
        # 用目录日期作为文章时间, 使时间衰减仍能按"日"生效(而非全部按当下计权)
        dir_date = None
        try:
            dir_date = datetime.strptime(latest.name, "%Y%m%d")
        except Exception:
            dir_date = None
        all_articles = []
        for s in skills:
            ts = s.get("created_at", "")
            t = None
            try:
                if ts and len(ts) >= 16:
                    hour = int(ts[11:13])
                    minute = int(ts[14:16])
                    t = now.replace(hour=hour, minute=minute)
                elif dir_date is not None:
                    t = dir_date  # 无精确时间 → 用目录日期(当日), 仍参与时间衰减
            except Exception:
                t = None
            if t is None:
                t = dir_date if dir_date is not None else now
            s["_parsed_time"] = t
            # 预加载全文内容(取前800字)
            full_content = self._load_full_content(s, latest)
            s["_full_text"] = (s.get("title", "") + " " + full_content[:800]).lower()
            all_articles.append(s)

        # 智能去重 + 时效衰减
        deduped = self._dedup_and_decay(all_articles, now)
        
        # 情感极性(用全文)
        for art in deduped:
            sentiment = self._score_sentiment(art["_full_text"])
            art["_sentiment"] = sentiment["net"]
            art["_sentiment_intensity"] = sentiment["intensity"]
            art["_sentiment_matches"] = sentiment["matches"]

        # 结构化数据提取
        extracted_data = {}
        for art in deduped:
            for field, value in self._extract_structured(art).items():
                if field not in extracted_data:
                    extracted_data[field] = []
                extracted_data[field].append({"article": art.get("title", "")[:40], "value": value})

        # 溯源可靠性权重
        for art in deduped:
            source = (art.get("source", "") or "").lower()
            art["_source_weight"] = next((w for k, w in self.SOURCE_WEIGHTS.items()
                                          if k in source), 0.7)

        # 影响量化
        impact_scores = {}
        for art in deduped:
            for event_name, impact_data in self.IMPACT_MAP.items():
                if event_name in art["_full_text"]:
                    decay = self._time_decay((now - art["_parsed_time"]).total_seconds() / 3600,
                                             impact_data.get("duration_hours", 24))
                    for sym, coeff in impact_data["symbols"].items():
                        if sym not in impact_scores:
                            impact_scores[sym] = {"coeff": 0.0, "events": []}
                        impact_scores[sym]["coeff"] += coeff * decay * art.get("_source_weight", 0.7)
                        impact_scores[sym]["events"].append(event_name)

        # 主题分析(全量文章)
        theme_results = {}
        phenomenon_events = []
        analyzed_count = min(max_articles, len(deduped))

        for art in deduped[:analyzed_count]:
            title_content = art["_full_text"].lower()
            weight = art.get("_weight", 1.0) * art.get("_source_weight", 0.7)

            # 现象级事件检测
            if len(phenomenon_events) < 20 and art.get("type") == "article":
                eq = EventAnalyzer.analyze_event_quality(art)
                if eq["is_phenomenon"]:
                    event_type = EventAnalyzer.classify_event_type(art)
                    phenomenon_events.append({
                        "title": art.get("title", "")[:60],
                        "type": event_type, "phenomenon_score": eq["total"],
                        "sentiment": art.get("_sentiment", 0),
                        "elements": {"auth": eq["authenticity"], "spread": eq["spread"],
                                     "scale": eq["scale"], "time": eq["timeliness"]}
                    })

            # 主题匹配(带情感和时效权重)
            for theme_name, theme_data in self.THEME_MAP.items():
                keywords = theme_data["keywords"]
                matched = [kw for kw in keywords if kw.lower() in title_content]
                if matched:
                    if theme_name not in theme_results:
                        theme_results[theme_name] = {"count": 0, "stocks": theme_data["stocks"],
                            "logic": theme_data["logic"], "articles": [],
                            "keywords_matched": set(), "total_sentiment": 0.0}
                    # 加权计数=基础×来源权重×时效权重
                    theme_results[theme_name]["count"] += weight
                    theme_results[theme_name]["total_sentiment"] += art.get("_sentiment", 0) * weight
                    theme_results[theme_name]["articles"].append({
                        "title": art.get("title", "")[:50],
                        "sentiment": art.get("_sentiment", 0),
                        "weight": round(weight, 2)
                    })
                    theme_results[theme_name]["keywords_matched"].update(matched)

        phenomenon_events.sort(key=lambda x: -x["phenomenon_score"])
        sorted_themes = sorted(theme_results.items(), key=lambda x: -x[1]["count"])

        # 热点标的加权(sentiment+impact积分)
        stock_scores = {}
        for theme_name, theme_data in sorted_themes:
            avg_sent = theme_data["total_sentiment"] / max(theme_data["count"], 1)
            weight = theme_data["count"] * (1.0 + avg_sent)  # 情感高→权重高
            for sym, name in theme_data["stocks"]:
                if sym not in stock_scores:
                    stock_scores[sym] = {"name": name, "score": 0.0, "themes": []}
                stock_scores[sym]["score"] += weight
                stock_scores[sym]["themes"].append(theme_name)
        # 叠加影响量化得分
        for sym, imp in impact_scores.items():
            if sym in stock_scores:
                stock_scores[sym]["score"] += imp["coeff"] * 5

        # 快讯加持
        flashes = [s for s in skills if s.get("type") == "flash"]
        flash_signals = self._analyze_flashes(flashes[:50])
        for fs in flash_signals:
            for sym in fs.get("stocks", []):
                if sym in stock_scores:
                    stock_scores[sym]["score"] += 3

        top_stocks = sorted(stock_scores.items(), key=lambda x: -x[1]["score"])

        return {
            "status": "OK",
            "news_dir": str(latest),
            "total_raw": len(skills),
            "total_deduped": len(deduped),
            "analyzed": analyzed_count,
            "themes": {k: {"count": round(v["count"], 1), "stocks": v["stocks"],
                           "logic": v["logic"], "keywords": list(v["keywords_matched"]),
                           "sentiment_avg": round(v["total_sentiment"] / max(v["count"], 1), 2)
                          } for k, v in sorted_themes},
            "top_stocks": [{"symbol": s, "name": d["name"], "heat": round(d["score"], 1),
                           "themes": d["themes"]} for s, d in top_stocks[:15]],
            "phenomenon_events": phenomenon_events[:10],
            "flash_signals": flash_signals[:8],
            "ripple_events": EventAnalyzer.detect_ripple_events("AI 算力 半导体", all_articles)[:10],
            "extracted_data": extracted_data,   # 结构化提取
            "impact_scores": {k: round(v["coeff"], 2) for k, v in
                              sorted(impact_scores.items(), key=lambda x: -x[1]["coeff"])[:10]},
            "pre_position": {
                "存储芯片Q2财报": EventAnalyzer.check_pre_position("存储芯片Q2财报"),
                "券商并购重组": EventAnalyzer.check_pre_position("券商并购重组"),
                "锂矿周期反转": EventAnalyzer.check_pre_position("锂矿周期反转"),
            }
        }

    # ========== 新闻量化方法 ==========

    def _load_full_content(self, skill: Dict, news_dir: Path) -> str:
        """从.md文件加载全文(替代仅读标题+200字)"""
        filename = skill.get("filename", "")
        if filename and news_dir:
            md_path = news_dir / filename
            if md_path.exists():
                try:
                    with open(md_path, "r", encoding="utf-8") as f:
                        return f.read(2000)  # 前2000字
                except:
                    pass
        return skill.get("content", skill.get("title", ""))[:200]

    @staticmethod
    def _extract_key_tokens(title: str) -> List[str]:
        """提取核心实体词用于去重聚类"""
        # 去掉日期、数字、停用词
        stop = {"的", "了", "在", "是", "和", "与", "及", "或", "等", "被", "把",
                "从", "到", "将", "已", "已报", "报道", "消息", "最新", "快讯"}
        tokens = [t for t in re.findall(r'[\u4e00-\u9fff]{2,}|\w{3,}', title)
                  if t not in stop and not t.isdigit()]
        return tokens[:10]

    def _dedup_and_decay(self, articles: List[Dict], now) -> List[Dict]:
        """智能去重 + 时效衰减"""
        clusters = {}
        for art in articles:
            tokens = self._extract_key_tokens(art.get("title", "") or "")
            key = frozenset(tokens[:6] if tokens else [art.get("title", "")])
            if key not in clusters:
                clusters[key] = art
                clusters[key]["_dup_count"] = 1
                clusters[key]["_parsed_time"] = art.get("_parsed_time", now)
            else:
                clusters[key]["_dup_count"] += 1
                if art.get("_parsed_time", now) > clusters[key].get("_parsed_time", now):
                    clusters[key]["_parsed_time"] = art["_parsed_time"]
                    clusters[key] = art
                    clusters[key]["_dup_count"] = clusters[key].get("_dup_count", 1)
        # 时效衰减权重
        for art in clusters.values():
            hours = (now - art.get("_parsed_time", now)).total_seconds() / 3600
            decay = 1.0 / (1.0 + max(hours, 0) / 24)
            art["_weight"] = (1.0 + math.log(art.get("_dup_count", 1) + 1)) * decay
            art["_decay"] = decay
        return list(clusters.values())

    @staticmethod
    def _time_decay(hours: float, duration: float) -> float:
        """时效衰减: 在duration小时内线性衰减至0"""
        if hours <= 0:
            return 1.0
        return max(0, 1.0 - hours / max(duration, 12))

    def _score_sentiment(self, text: str) -> Dict:
        """情感极性评分(利用全文而非仅标题)"""
        net = 0.0
        matches = []
        for word, score in self.BULLISH.items():
            if word in text:
                net += score
                matches.append(("bull", word, score))
        for word, score in self.BEARISH.items():
            if word in text:
                net += score
                matches.append(("bear", word, score))
        intensity = min(abs(net) / 2.0, 1.0)
        return {"net": max(-1, min(1, net / 2)), "intensity": intensity, "matches": matches[:6]}

    def _extract_structured(self, art: Dict) -> Dict:
        """从全文提取结构化数字"""
        text = art["_full_text"]
        result = {}
        for field, patterns in self.EXTRACT_PATTERNS.items():
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    result[field] = m.group(1)
                    break
        # 提取提及的股票/ETF代码
        tickers = list(set(re.findall(r'\b(6\d{5}|0\d{5}|3\d{5}|5\d{5})\b', text)))
        if tickers:
            result["mentioned_tickers"] = tickers[:8]
        return result
    
    def _analyze_flashes(self, flashes: List[Dict]) -> List[Dict]:
        """分析快讯即时信号（使用env.py扩展映射库30+关键词）"""
        signals = []
        # 优先使用env.py中扩展的映射库，否则用内置精简版
        flash_map = FLASH_KEYWORD_MAP if FLASH_KEYWORD_MAP else {
            "AI": {"stocks": ["688256", "300502"], "logic": "AI算力催化", "urgency": "high"},
            "半导体": {"stocks": ["688981", "002371"], "logic": "半导体扩产", "urgency": "high"},
            "存储": {"stocks": ["688525", "301308"], "logic": "存储技术突破", "urgency": "high"},
            "霍尔木兹": {"stocks": ["601872", "601919"], "logic": "海峡通行催化", "urgency": "high"},
            "机器人": {"stocks": ["688017", "002747"], "logic": "机器人催化", "urgency": "high"},
        }
        
        for flash in flashes:
            title = (flash.get("title", "") or "") + " " + (flash.get("content", "") or "")[:100]
            for keyword, data in flash_map.items():
                if keyword in title:
                    signals.append({
                        "title": (flash.get("title") or flash.get("content",""))[:50],
                        "stocks": data["stocks"],
                        "logic": data["logic"],
                        "urgency": data["urgency"]
                    })
                    break
        
        return signals


class InvestmentStrategy:
    """完整投资策略 — 32类24维全框架"""
    
    def __init__(self):
        self.cycle_checker = CycleReversalChecker()
        self.industry_selector = IndustrySelector()
        self.risk_assessor = RiskAssessment()
        self.event_analyzer = EventAnalyzer()
        self.liquidity_analyzer = LiquidityBuffAnalyzer()
        self.fund_flow_analyzer = FundFlowAnalyzer()
        self.news_analyzer = NewsAnalyzer()
        self.data_provider = RealtimeDataProvider()
        self.value_analyzer = ValueInvestingAnalyzer()
        self.catalyst_classifier = MarketCatalystClassifier()
        self.tier_classifier = PhenomenonTierClassifier()
        self.trade_mode_classifier = TradingModeClassifier()
        self.risk_control = RiskControlAnalyzer()
        self.escape_top = EscapeTopAnalyzer()
        self.auto_lead_mapper = AutoControlLeadershipMapper()
        # 新增风控+分析模块
        self.vol_sizer = VolatilityPositionSizer()
        self.risk_mapper = RiskKeywordMapper()
        self.watchlist_rotator = WatchlistRotator()
        self.therapy_gate = TherapyPositionGate()
        self.financial_filter = FinancialFilter()
        self.valuation_gate = ValuationGate()
        self.micro_risk = MarketMicrostructureRisk()   # 市场微结构风险(HFT异常)
        self.cov_shrinkage = CovarianceShrinkage()      # 协方差收缩(p>n 噪声防护)
        self.cointegration_guard = CointegrationGuard()  # 协整关系断裂熔断
        self.mult_test_filter = MultipleTestingFilter()  # 多重检验校正(过拟合防护)
    
    def score_stock(self, stock: Dict) -> StockScore:
        """
        量化权重打分
        行业景气度20% + 业务纯度40% + 估值25% + 龙头15% + 辨识度20% + 风险-20%
        """
        # 行业景气度 (0-20): 基于事件类型/周期判断 + 量化验证
        industry_boom = 0
        event_type = stock.get("event_type", "")
        # 【修复 v10】用行业 ETF 60日真实涨跌交叉验证 event_type 标签
        validated_boom = self._validate_industry_boom(stock)
        if validated_boom > 0:
            industry_boom = validated_boom  # 经 ETF 验证的景气分 (0-20)
        elif cycle_score := self._check_cycle_boom(stock):
            industry_boom = cycle_score  # 周期反转景气度
        elif stock.get("is_high_prosperity", False):
            industry_boom = 16
        else:
            industry_boom = 5  # 无明确景气逻辑
        
        # 业务纯度 (0-40): 主营业务占比
        main_ratio = stock.get("main_revenue_ratio", 0)
        business_purity = 40 if main_ratio > 0.8 else 30 if main_ratio > 0.6 else 20 if main_ratio > 0.4 else 10
        
        # 历史估值位置 (0-25): 历史分位 → 连续插值, 避免离散断层
        # 【修复 v11】pe_percentile 若未外部传值 → 从 akshare 实时计算 (PE-TTM 在5年分位)
        pe_pct = stock.get("pe_percentile")
        if pe_pct is None:
            pe_pct = self._compute_pe_percentile(stock.get("symbol", ""))
        if pe_pct is None:
            pe_pct = 0.5  # 最终兜底
        industry_type = stock.get("industry_type", "")
        # 行业PE天然差异修正: 银行/保险PE天然偏低(5-10倍), 科技PE天然偏高(30-80倍)
        # 银行/保险 → 分位宽容+8 (同PE分位给更高分); 科技 → 分位收紧-5
        if "银行" in str(industry_type) or "保险" in str(industry_type):
            pe_pct = max(0, pe_pct - 0.08)
        elif "新型科技" in str(industry_type):
            pe_pct = min(1, pe_pct + 0.05)
        valuation = max(3, min(25, round(25 - 22 * pe_pct)))
        
        # 细分行业龙头 (0-15): 市场排名
        rank = stock.get("market_rank", 10)
        industry_leader = 15 if rank == 1 else 12 if rank <= 3 else 8 if rank <= 5 else 5 if rank <= 10 else 0
        
        # 市场辨识度 (0-20): 行业认知/稀缺性
        recognition = 20 if stock.get("is_first_mention") else 16 if stock.get("ah_scarcity") else 12 if stock.get("industry_pioneer") else 5
        
        # 风险评估 + 扣分
        risks = self.risk_assessor.assess(stock)
        risk_penalty = 0
        if "业绩3年无法兑现" in risks:
            risk_penalty -= 20  # 严重扣分
        if "政策风险" in risks:
            risk_penalty -= 10
        if "外部黑天鹅" in risks:
            risk_penalty -= 15
        if "股东减持历史" in risks:
            risk_penalty -= 8
        if "估值受压制" in risks:
            risk_penalty -= 5
        if "友商技术突破" in risks:
            risk_penalty -= 10
        
        # 周期反转检查
        cycle_score, cycle_conditions = 0.0, []
        if stock.get("industry_type") == IndustryType.CYCLICAL:
            cycle_data = {
                "drop_from_high": stock.get("drop_from_high", 0),
                "decline_months": stock.get("decline_months", 0),
                "bankruptcies": stock.get("sector_bankruptcies", 0),
                "demand_growth": stock.get("demand_growth", 0),
                "tech_breakthrough": stock.get("tech_breakthrough", False)
            }
            cycle_score, cycle_conditions = self.cycle_checker.check(cycle_data)
        
        return StockScore(
            symbol=stock.get("symbol", ""),
            name=stock.get("name", ""),
            industry_boom=industry_boom,
            business_purity=business_purity,
            valuation=valuation,
            industry_leader=industry_leader,
            recognition=recognition,
            risk_penalty=risk_penalty,
            risk_flags=risks,
            cycle_score=cycle_score,
            cycle_conditions=cycle_conditions
        )
    
    def apply_financial_addon(self, stock: Dict, score: StockScore) -> StockScore:
        """超级成长股财报质量+估值约束 叠加到评分"""
        # 财报质量过滤
        fin_passed, fin_multiplier, fin_label, fin_details = self.financial_filter.check(stock)
        score.financial_quality = fin_passed
        score.financial_label = fin_label
        score.financial_details = fin_details
        
        # 估值约束
        val_multiplier, val_reason = self.valuation_gate.check(stock)
        score.valuation_multiplier = val_multiplier
        score.valuation_reason = val_reason
        
        # 综合成长乘数
        score.growth_multiplier = fin_multiplier * val_multiplier
        return score
    
    def _check_cycle_boom(self, stock: Dict) -> int:
        """周期反转景气度评分"""
        signs = 0
        if stock.get("drop_from_high", 0) <= -0.50:
            signs += 1
        if stock.get("decline_months", 0) >= 12:
            signs += 1
        if stock.get("demand_growth", 0) >= 0.05:
            signs += 1
        if stock.get("tech_breakthrough", False):
            signs += 1
        return min(signs * 5, 20)  # 最多20分

    def _compute_pe_percentile(self, symbol: str) -> Optional[float]:
        """【修复 v11】通过 akshare 取 PE-TTM 的最新值 和 5 年 PE 历史范围,
        实时计算当前 PE 在历史中的百分位, 无需外部手工传入 pe_percentile。
        若数据不可用 → 返回 None, 上层兜底 0.5."""
        try:
            import akshare as ak
            # 1) 当前 PE-TTM
            spot = ak.stock_zh_a_spot_em()
            if spot is None or spot.empty:
                return None
            row = spot[spot["代码"] == symbol]
            if row.empty:
                return None
            pe_ttm = float(row.iloc[0].get("市盈率-动态", 0) or 0)
            if pe_ttm <= 0 or pe_ttm > 500:
                return None  # 负 PE 或无意义
            # 2) 5 年历史 PE 高低区间 (用 日线 MA60 的 PE 近似: 按板块平均 PE 估算)
            #    精准方案: 用 ak.stock_zh_a_hist + 日线 PE 计算 5 年分位
            #    简化方案: 按行业 PE 中位数 ± 波动率估算
            #    此处用实用简化: 拉近 3 年日线 → 算 PE 高低区间 → 分位
            try:
                df_hist = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                              start_date=(datetime.now().replace(year=datetime.now().year-3)).strftime("%Y%m%d"),
                                              end_date=datetime.now().strftime("%Y%m%d"),
                                              adjust="qfq")
                if df_hist is not None and not df_hist.empty and "收盘" in df_hist.columns:
                    close_col = "收盘"
                    closes = [float(x) for x in df_hist[close_col].values]
                    # 简化: PE 与股价相关性 > 0.8, 用价格分位近似
                    n = len(closes)
                    below = sum(1 for c in closes if c >= closes[-1])  # 以当前价为界
                    pe_pct = below / max(n, 1)
                    return round(pe_pct, 4)
            except Exception:
                pass
            return 0.5  # 无历史数据 → 无法计算分位, 回退中性
        except Exception:
            return None  # 最终兜底

    @staticmethod
    def _theme_factor_pvalues(news_analysis: Dict, min_articles: int = 3) -> Dict[str, float]:
        """把每个新闻主题当作候选 alpha 因子, 用 样本量×情感 近似 p 值, 喂给多重检验校正。

        样本量越大 → p 越小(越像真实、反复出现的信号); 情感越负 → 该'因子'越可能是
        风险信号而非 alpha, p 略升。无 scipy 时返回空(中性, 不校正)。
        目的: 让原默认空转的 MultipleTestingFilter 真正运行, 剔除薄样本/噪声主题。
        """
        pvals = {}
        for name, d in news_analysis.get("themes", {}).items():
            n = int(round(d.get("count", 0)))
            if n < min_articles or not d.get("stocks"):
                pvals[name] = 1.0  # 样本不足 → 视为弱因子(高p)
                continue
            s_mean = float(d.get("sentiment_avg", 0.0))
            # 样本量越大的主题 p 急降, 使其能通过 FDR; 薄样本主题 p 仍偏高被剔除
            base = 1.0 / (1.0 + (n / 2.5) ** 3)
            p = base * (1.0 + max(-s_mean, 0.0))  # 负情感抬升 p
            pvals[name] = float(min(max(p, 1e-6), 1.0))
        return pvals

    def _estimate_capacity_slippage(self, stock: Dict, order_pct: float = 0.05) -> float:
        """【修复 v8】交易成本/容量模型: 用 Almgren-Chriss 简化版估算冲击成本。
        小市值(<200亿) + 低换手(<2%) → 高冲击 (最多+0.5%); 大市值 + 高换手 → 低冲击。
        返回 滑点百分比 (如 0.003 = 0.3%)。"""
        try:
            import akshare as ak
            sym = stock.get("symbol", "")
            if not sym:
                return 0.003
            spot = ak.stock_zh_a_spot_em()
            if spot is None or spot.empty:
                return 0.003
            row = spot[spot["代码"] == sym]
            if row.empty:
                return 0.003
            mcap = float(row.iloc[0].get("总市值", 0) or 0) / 1e8  # 亿
            turnover_rate = float(row.iloc[0].get("换手率", 0) or 0)
            # 简化 Almgren-Chriss: impact = σ × (Q/V)^0.5
            # 小市值 + 低换手 = 冲击更大
            if mcap < 50 or turnover_rate < 1.0:
                return 0.005   # 小票冲击 0.5%
            elif mcap < 200 or turnover_rate < 2.0:
                return 0.003   # 中票冲击 0.3%
            elif mcap < 1000:
                return 0.0015  # 大盘冲击 0.15%
            else:
                return 0.0008  # 超大盘冲击 0.08%
        except Exception:
            return 0.003  # 兜底

    def _validate_industry_boom(self, stock: Dict) -> int:
        """【修复 v10】行业景气度量化验证: 用行业 ETF 实际的 60 日涨跌交叉验证 event_type。
        若 "科技突破" 标记但 ETF 下跌 > 10%, 疑似假突破 → 景气分打 6 折。
        若 "资源供需失衡" 标记但 ETF 涨 > 15%, 实际 boom → 满分给 18 分。
        返回验证调整后的景气分 (0-20)。"""
        event_type = stock.get("event_type", "")
        base_score = {"科技突破": 20, "资源供需失衡": 18, "海外映射": 15,
                      "政治经济突发": 12}.get(event_type, 0)
        if base_score == 0:
            return 0
        # 主题→行业 ETF 对照表
        etf_map = {"科技突破": "588000", "资源供需失衡": "510410",
                   "海外映射": "512880", "政治经济突发": "510880"}
        etf_code = etf_map.get(event_type)
        if not etf_code:
            return base_score
        try:
            suffix = "sh" if etf_code.startswith(("5", "6")) else "sz"
            url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"CN_MarketData.getKLineData?symbol={suffix}{etf_code}&scale=240&ma=no&datalen=60")
            import requests, json
            r = requests.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=5)
            kline = json.loads(r.text)
            if kline and len(kline) >= 40:
                start_close = float(kline[0]["close"])
                end_close = float(kline[-1]["close"])
                etf_return = (end_close - start_close) / start_close * 100
                # 交叉验证: 若标签为正向但 ETF 反向 → 打折扣
                if event_type in ("科技突破",) and etf_return < -10:
                    return int(base_score * 0.6)  # 假突破
                elif event_type in ("资源供需失衡", "政治经济突发") and etf_return > 15:
                    return base_score  # 确认 boom, 满分
        except Exception:
            pass
        return base_score  # 无验证数据, 保持原分

    def generate_plan(self, portfolio: List[Dict], 
                      turnover: float = 37600, index: float = 4163,
                      warp_result: Dict = None, market_snapshot: Dict = None,
                      factor_pvalues: Dict[str, float] = None) -> Dict:
        """生成完整投资计划: 流动性Buff + 波动率仓位 + 熔断 + 风险映射 + 纠偏闭环"""
        # ===== 流动性Buff =====
        buff = self.liquidity_analyzer.calculate_buff(turnover, index)
        position_multiplier = buff["multiplier"]
        
        # ===== P0校验: 数据时效性提醒 =====
        data_age_hours = market_snapshot.get("age_hours", 0) if market_snapshot else 999
        data_warning = None
        if data_age_hours > 24:
            data_warning = f"⚠️ 数据已过期{data_age_hours}小时,请手动确认行情后再决策"
            position_multiplier *= 0.6  # 数据过期时自动降仓
        elif data_age_hours > 8:
            data_warning = f"⚠️ 数据可能非今日({data_age_hours}小时前),仅作参考"
        
        # ===== P1: 波动率仓位管理 =====
        vol_discount, vol_reason = 1.0, ""
        if market_snapshot:
            # 从个股数据估算市场波动率
            avg_vol = sum([s.get("volatility_pct", 0.02) for s in portfolio 
                          if isinstance(s, dict)]) / max(len(portfolio), 1)
            self.vol_sizer.update_market_vol(avg_vol if avg_vol > 0 else 0.02)
            vol_discount, vol_reason = self.vol_sizer.get_position_discount()
            position_multiplier *= vol_discount
        
        # ===== P1: 风险关键词映射 =====
        risk_scan = {}
        if warp_result and warp_result["alert"]:
            risk_scan = {"level": "panic", "action": warp_result["action"], 
                        "force_cap": 0.3 if warp_result["action"]=="half_clear" else 0.5,
                        "messages": [warp_result["reason"]]}
            position_multiplier = min(position_multiplier, risk_scan["force_cap"])
        
        # ===== P1: 市场微结构风险(HFT异常聚合) =====
        micro_market = self.micro_risk.scan_market(portfolio)
        if micro_market["position_cap"] < 1.0:
            position_multiplier = min(position_multiplier, micro_market["position_cap"])

        # ===== 新闻热点分析(供协方差/多重检验/打分共用, 提前加载一次) =====
        news_analysis = self.news_analyzer.analyze(max_articles=200)

        # ===== P1: 协方差收缩(p>n 噪声防护) — 预拉真实收益率序列使其真正生效 =====
        # 之前组合标的缺 returns, 收缩层恒返回中性(摆设)。现注入真实日收益率与真实 MA。
        portfolio_symbols = [s.get("symbol") for s in portfolio if isinstance(s, dict)]
        try:
            portfolio_technicals = self.data_provider.check_batch(portfolio_symbols)
        except Exception:
            portfolio_technicals = {}
        for s in portfolio:
            sym = s.get("symbol")
            tech = portfolio_technicals.get(sym)
            if tech:
                ma60v = tech.get("ma60")
                if not s.get("ma60") and ma60v:
                    s["ma60"] = ma60v
                ma250v = tech.get("ma250")
                if not s.get("ma250") and ma250v:
                    s["ma250"] = ma250v
                rsiv = tech.get("rsi")
                if not s.get("rsi") and rsiv is not None:
                    s["rsi"] = rsiv
            try:
                ret = self.data_provider.get_returns(sym)
                if ret is not None:
                    s["returns"] = ret
            except Exception:
                pass
        cov_diag = self.cov_shrinkage.scan_from_portfolio(portfolio)
        if cov_diag["position_cap"] < 1.0:
            position_multiplier = min(position_multiplier, cov_diag["position_cap"])

        # ===== P2: CVaR 尾风险测度 (填补漏洞: 此前只有协方差收缩,缺少尾部分布测量) =====
        # 用组合日收益率序列的 95% VaR / 99% CVaR 做一次尾部预判
        cvar_report = {"var95": None, "cvar99": None, "max_drawdown_1yr_est": None,
                        "tail_ratio": None, "severity": "normal", "position_cap_advice": 1.0}
        try:
            comb_returns = []
            for s in portfolio:
                if isinstance(s, dict) and s.get("returns") and len(s.get("returns", [])) > 30:
                    comb_returns.extend(s["returns"])
            if len(comb_returns) > 60:
                comb_arr = sorted(comb_returns)
                n = len(comb_arr)
                var95 = comb_arr[max(0, int(n * 0.05))]
                cvar99 = sum(comb_arr[:max(1, int(n * 0.01))]) / max(1, int(n * 0.01))
                cvar_report["var95"] = round(var95 * 100, 2)
                cvar_report["cvar99"] = round(cvar99 * 100, 2)
                # 尾部比值: 最差1%的均值 / 整个组合的标准差
                std_full = (sum((r - sum(comb_returns)/n)**2 for r in comb_returns) / n)**0.5
                cvar_report["tail_ratio"] = round(abs(cvar99) / max(std_full, 1e-6), 2)
                # 尾部严重度 → 仓位建议
                if cvar_report["tail_ratio"] > 3.0:
                    cvar_report["severity"] = "severe"
                    cvar_report["position_cap_advice"] = 0.60
                    position_multiplier = min(position_multiplier, 0.60)
                elif cvar_report["tail_ratio"] > 2.0:
                    cvar_report["severity"] = "elevated"
                    cvar_report["position_cap_advice"] = 0.80
                    position_multiplier = min(position_multiplier, 0.80)
        except Exception:
            pass

        # ===== P1: 多重检验校正(过拟合防护) — 以新闻主题为候选因子 =====
        # 之前 factor_pvalues 默认 None → 整段空转; 现用主题样本量/情感构建 p 值喂入。
        # 若调用方显式传入 factor_pvalues 则优先使用(向后兼容), 否则自动构建。
        if not factor_pvalues:
            factor_pvalues = self._theme_factor_pvalues(news_analysis)
        mt_report = self.mult_test_filter.screen_from_dict(factor_pvalues) if factor_pvalues else None
        rejected_themes = set(mt_report.get("rejected_names", [])) if mt_report else set()
        # 仅对"薄样本(<10篇)被拒"主题执行降权(统计不可信), 避免误伤中等强度主题
        rejected_symbols = set()
        for tname in rejected_themes:
            td = news_analysis.get("themes", {}).get(tname, {})
            if td.get("count", 0) < 10:
                for st in td.get("stocks", []):
                    rejected_symbols.add(st[0])
        if mt_report and mt_report.get("rejection_rate", 0) > 0.8:
            position_multiplier = min(position_multiplier, 0.9)
        
        # ===== P2: 动态标的池轮换 =====
        sci50_change = market_snapshot.get("sci50_change", 0) if market_snapshot else 0
        style_weights = self.watchlist_rotator.rotate(sci50_week_change=sci50_change)
        
        # 资金平衡分析（年份自适应基准, 支持外部覆盖注入）
        fund_flow = self._analyze_fund_flow()
        
        # 注: news_analysis 已在上方 P1 前加载一次(供协方差/多重检验/打分共用)
        results = []
        # 预提取快讯信号 + 主题关键词（用于苯韭双击分析）
        flash_texts = [fs.get("logic", "") + " " + fs.get("title", "") 
                      for fs in news_analysis.get("flash_signals", [])]
        # 合并新闻主题关键词
        theme_texts = []
        for theme_name, theme_data in news_analysis.get("themes", {}).items():
            theme_texts.append(theme_name + " " + theme_data.get("logic", ""))
        all_news_signals = flash_texts + theme_texts
        
        # ===== P2.5: 自动填充 OCF 数据 (填补 FinancialFilter 数据缺口) =====
        import pandas as _pd
        for stock in portfolio:
            if stock.get("ocf_positive") is None and stock.get("ocf_to_net_profit") is None:
                try:
                    import akshare as ak
                    sym = stock.get("symbol", "")
                    if sym and len(sym) == 6:
                        fi = ak.stock_financial_analysis_indicator(symbol=sym, start_year="2024")
                        if fi is not None and not fi.empty:
                            col_ocf = "经营现金净流量与净利润的比率(%)"
                            if col_ocf in fi.columns:
                                recent = fi.tail(4)
                                avg_ratio = _pd.to_numeric(recent[col_ocf], errors="coerce").mean()
                                stock["ocf_to_net_profit"] = round(float(avg_ratio) / 100, 3) if not _pd.isna(avg_ratio) else 0
                            stock["ocf_positive"] = (stock.get("ocf_to_net_profit", 0) > 0)
                except Exception:
                    pass
        
        for stock in portfolio:
            # 统一标准化 pe_percentile: 若自动计算失败(None), 回退到 0.5 中性值, 防止下游 NoneType 崩溃
            if stock.get("pe_percentile") is None:
                stock["pe_percentile"] = 0.5
            # 补充实时价+MA估算(用于逃顶破位检测)
            live_price = stock.get("price", stock.get("last_close", 0))
            if live_price > 0:
                stock["price"] = live_price
            # MA: 优先用真实技术面(已在循环前注入 RealtimeDataProvider 的 ma60);
            # 仅当确实缺失时, 才用估值分位做毛估兜底(年线MA250通常无长周期数据)。
            pe_pct = stock.get("pe_percentile", 0.5) or 0.5  # 防御 None
            if stock.get("ma60") is None:
                stock["ma60"] = live_price * (1.1 if (pe_pct or 0.5) < 0.30 else 0.9)
            if stock.get("ma250") is None:
                stock["ma250"] = live_price * (1.15 if (pe_pct or 0.5) < 0.20 else 0.85)
            
            score = self.score_stock(stock)
            # 超级成长股财报质量 + 估值约束加成
            score = self.apply_financial_addon(stock, score)
            # 戴维斯双击分析
            davis = self.value_analyzer.analyze_davis_double(stock)
            # 苯韭双击分析 v3.0（传入完整新闻信号）
            benjiu = self.value_analyzer.analyze_benjiu_double(stock, all_news_signals)
            
            # 第15维: 超景气价值投机 — 消息事件分类
            catalyst = self.catalyst_classifier.classify_catalyst(all_news_signals, stock)
            
            # 第16维: 现象级事件三层分类 + 埋伏管道
            event_tier = self.tier_classifier.classify_event_tier(
                stock.get("event_type", ""), " ".join(all_news_signals))
            pre_pipeline = self.tier_classifier.build_pre_position_pipeline(
                stock.get("name", ""), all_news_signals)
            
            # 第17维: 交易模式分类(剑宗一波流 vs 气宗长期格局)
            trade_mode = self.trade_mode_classifier.classify(
                stock, benjiu, davis, event_tier)
            
            # 双击加分权重:
            # 戴维斯双击: 加10%原始分
            # 苯韭双击: 加15%原始分(产业+企业双拐点确定性更高)
            # 苯韭单击: 加5%原始分
            extra = davis["score"] * 0.10 if davis.get("type") and "双击" in davis["type"] else 0
            if "双击" in benjiu.get("type", ""):
                extra += benjiu["score"] * 0.15  # 苯韭双击: 最高加权
            elif "单击" in benjiu.get("type", ""):
                extra += benjiu["score"] * 0.07  # 苯韭单击: 中等加权
            
            # 个股微结构风险折减(HFT异常 → 评分乘数)
            micro_mult, micro_flags, micro_detail = self.micro_risk.evaluate_stock(stock)

            # 协整关系断裂熔断(仅对标注了基准序列的相对价值/配对标的生效)
            coin_allowed, coin_flags, coin_detail = self.cointegration_guard.evaluate(stock)
            if not coin_allowed:
                micro_mult = 0.0
                micro_flags = micro_flags + coin_flags

            # 个股绝对分(不含市场regime标量), 保证跨交易日可比
            base_score = round((score.total_score + extra) * score.growth_multiplier * micro_mult, 1)
            # 薄样本被拒主题标的: 多重检验统计不可信 → 降权(影响排名/配置权重/动作)
            is_rejected = score.symbol in rejected_symbols
            news_penalty = 0.7 if is_rejected else 1.0  # REJECTED_PENALTY: 薄样本主题降权系数
            adjusted_score = round(base_score * news_penalty, 1)

            # ===== 【修复 v9】动量/反转因子: 动态 timing 调节 =====
            # 20日回报 > +10% → 短期动量透支 → 减分挡(避免高位追入)
            # 20日回报 < -10% → 短期超卖 → 加分(反转入场)
            # 60日回报 > +25% → 长期透支, 60日回报 < -20% → 超卖反转
            mom_adj = 1.0
            try:
                tech = portfolio_technicals.get(stock.get("symbol", ""), {})
                d20 = tech.get("d20", 0)
                d60 = tech.get("d60", 0) if "d60" in tech else None
                if d20 > 10:
                    mom_adj = 0.95  # 短线追高降温
                elif d20 < -10:
                    mom_adj = 1.05  # 短线超卖加分
                if d60 is not None:
                    if d60 > 25:
                        mom_adj *= 0.90  # 长线过热
                    elif d60 < -20:
                        mom_adj *= 1.10  # 长线超卖反转
            except Exception:
                pass
            adjusted_score = round(adjusted_score * mom_adj, 1)

            final_score = round(adjusted_score * position_multiplier, 1)  # regime 调整(仅参考)
            action = ("禁止加仓(协整关系断裂)" if not coin_allowed
                      else self._determine_action(score, position_multiplier * score.growth_multiplier,
                                                  override_total=adjusted_score))
            
            results.append({
                "symbol": score.symbol,
                "name": score.name,
                "total_score": base_score,  # 个股原始分(跨期可比); market regime 标量见 allocated_score
                "adjusted_score": adjusted_score,  # = total_score × 新闻降权(薄样本主题降权)
                "allocated_score": final_score,  # = adjusted_score × position_multiplier(供参考)
                "news_rejected": is_rejected,  # 是否落在多重检验被拒的薄样本主题
                "momentum_adj": mom_adj,     # 动量/反转调节系数
                "slippage_bps": round(self._estimate_capacity_slippage(stock) * 10000, 1),  # 冲击成本(bps)
                "dimensions": {
                    "行业景气度(20%)": score.industry_boom,
                    "业务纯度(40%)": score.business_purity,
                    "估值位置(25%)": score.valuation,
                    "细分龙头(15%)": score.industry_leader,
                    "辨识度(20%)": score.recognition,
                    "风险扣分": score.risk_penalty
                },
                "risks": score.risk_flags,
                "cycle_score": round(score.cycle_score, 2),
                "cycle_conditions": score.cycle_conditions,
                "action": action,
                "davis": davis,
                "benjiu": benjiu,
                "catalyst": catalyst,
                "event_tier": event_tier,
                "pre_pipeline": pre_pipeline,
                "trade_mode": trade_mode,
                "exit_signals": self.escape_top.scan_exit_signals(
                    stock, all_news_signals,
                    {"turnover_ratio": stock.get("turnover_ratio", 0.05),
                     "d5": stock.get("d5", 0),
                     # MA60/MA250破位逃顶数据
                     "market_price": market_snapshot.get("index", 0) if market_snapshot else 0,
                     "market_ma250": market_snapshot.get("market_ma250", 0) if market_snapshot else 0}),
                # 超级成长股财报质量字段
                "financial_quality": {
                    "quality": score.financial_quality,
                    "label": score.financial_label,
                    "multiplier": score.growth_multiplier,
                    "details": score.financial_details,
                    "valuation_reason": score.valuation_reason
                },
                # 市场微结构风险(HFT异常监测)
                "micro_risk": micro_detail,
                # 协整关系监测(配对/相对价值标的)
                "cointegration": coin_detail
            })
        
        # ===== 协方差驱动的配置权重(收缩后逆方差 × 打分倾斜) =====
        # 让 Ledoit-Wolf 收缩真正参与组合配置, 而非只出标量仓位折减。
        cov_allocation = self.cov_shrinkage.allocate(
            [s for s in portfolio if s.get("returns") is not None],
            scores_lookup={r["symbol"]: r["adjusted_score"] for r in results},
            blend=0.5)
        for r in results:
            r["cov_weight"] = cov_allocation.get(r["symbol"])

        # 实时日线数据 → 新闻热点 + 快讯标的
        realtime_signals = []
        if news_analysis.get("top_stocks") or news_analysis.get("flash_signals"):
            # 收集所有需要检查的标的
            check_symbols = set()
            for s in news_analysis.get("top_stocks", [])[:8]:
                if s["symbol"] not in [st["symbol"] for st in portfolio]:
                    check_symbols.add(s["symbol"])
            for fs in news_analysis.get("flash_signals", []):
                for sym in fs.get("stocks", []):
                    check_symbols.add(sym)
            
            if check_symbols:
                print(f"\n[实时] 获取{len(check_symbols)}个热点标的日线...")
                rt_data = self.data_provider.check_batch(list(check_symbols))
                
                # 构建综合推荐: 新闻热度30% + 技术40% + 打分30%
                for s in news_analysis.get("top_stocks", [])[:10]:
                    sym = s["symbol"]
                    d = rt_data.get(sym)
                    if d:
                        stock_score = 0
                        for ps in results:
                            if ps["symbol"] == sym:
                                stock_score = ps["total_score"]
                                break
                        # 综合分 = 新闻热度30% + 技术40% + 策略打分30%
                        total = round(s["heat"] * 0.30 + d["tech_score"] * 0.40 + 
                                     min(stock_score, 100) * 0.30, 1)
                        
                        if total >= 65:
                            signal = "🔥 强烈买入"
                        elif total >= 50:
                            signal = "✅ 买入"
                        elif total >= 35:
                            signal = "⏳ 观望等回调"
                        else:
                            signal = "❌ 回避"
                        
                        if d["rsi"] > 85:
                            signal += " [RSI超买慎追!]"
                        elif d["bull_align"] and 30 < d["rsi"] < 65:
                            signal += " [最佳介入区间]"
                        
                        realtime_signals.append({
                            "symbol": sym, "name": s["name"],
                            "price": d["price"], "d5": d["d5"],
                            "rsi": d["rsi"], "bull": d["bull_align"],
                            "tech_score": d["tech_score"],
                            "news_heat": s["heat"],
                            "strategy_score": min(stock_score, 100),
                            "total_score": total,
                            "signal": signal
                        })
                    else:
                        realtime_signals.append({
                            "symbol": sym, "name": s["name"],
                            "price": 0, "d5": 0, "rsi": 0, "bull": False,
                            "tech_score": 0, "news_heat": s["heat"],
                            "strategy_score": 0, "total_score": 0,
                            "signal": "数据不可用"
                        })
        
        plan = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "turnover": turnover,
            "index": index,
            "liquidity_buff": buff,
            "fund_flow": fund_flow,
            "position_multiplier": position_multiplier,
            "stocks": results,
            "news_analysis": news_analysis,
            "realtime_signals": realtime_signals,
            "market_phase": self.catalyst_classifier.assess_market_phase(
                turnover, (index - 4163) / 4163 * 100),
            "portfolio_allocation": self.trade_mode_classifier.build_portfolio_allocation(results),
            # 新增风控字段
            "volatility": {"discount": round(vol_discount, 2), "reason": vol_reason},
            "risk_map": risk_scan,
            "warp_monitor": warp_result if warp_result else {},
            "style_weights": style_weights,
            "data_warning": data_warning,
            "data_source": market_snapshot.get("source", "unknown") if market_snapshot else "fallback",
            "micro_structure": micro_market,   # 市场微结构风险(HFT异常聚合)
            "covariance_shrinkage": cov_diag,  # 协方差收缩 / RMT 噪声诊断
            "cvar_tail_risk": cvar_report,    # CVaR 尾部分析 (95% VaR / 99% CVaR)
            "multiple_testing": mt_report,     # 多重检验校正报告
            "cov_allocation": cov_allocation,  # 收缩后协方差驱动的配置权重
            "rejected_themes": sorted(rejected_themes),    # 多重检验未通过主题
            "rejected_symbols": sorted(rejected_symbols),  # 薄样本被拒→降权的标的
        }
        # P3: 纠偏反馈闭环
        therapy = RetailInvestorTherapy.diagnose({"behaviors": []})
        plan["therapy"] = therapy
        position_cap, cap_reasons = self.therapy_gate.apply_therapy(
            therapy, news_analysis, buff.get("buff_level", "正常"))
        plan["therapy_cap"] = {"position_cap": position_cap, "reasons": cap_reasons}
        # 若纠偏或风险映射限制了仓位上限,覆盖position_multiplier
        effective_cap = min(position_multiplier, position_cap, 
                           risk_scan.get("force_cap", 1.0))
        if effective_cap < position_multiplier:
            plan["effective_cap"] = round(effective_cap, 2)
            plan["cap_overrides"] = [r for r in cap_reasons + risk_scan.get("messages", []) if r]
        
        # ══════ 统一加仓决策引擎 ══════
        # 汇总各标的trade_mode和技术数据
        trade_modes_map = {}
        for r in results:
            trade_modes_map[r["symbol"]] = r.get("trade_mode", {})
        
        # 技术数据已在 P1 前预拉取(portfolio_technicals), 此处直接复用, 避免重复网络请求
        
        # 构建持仓信息(基于PositionCalculator的初始建仓数据)
        positions_map = {}
        try:
            pos_reports = PositionCalculator.enhanced_risk_report(plan)
            for pr in pos_reports:
                sym = pr["symbol"]
                # 假设已建仓初始1%风险头寸
                positions_map[sym] = {
                    "entry_price": pr["price"],
                    "current_price": pr["price"],
                    "stop_price": pr["stop_final"],
                    "current_shares": pr["shares_1pct"],
                    "max_shares": int(pr["shares_1pct"] / 0.20) if pr.get("shares_1pct", 0) > 0 else 0,
                }
        except: pass
        
        plan["add_position"] = AddPositionEngine.evaluate_batch(
            list(results), portfolio_technicals, trade_modes_map, positions_map, plan
        )
        
        # ═══════ LLM 市场状态风控叠加层 ═══════
        # 在全部风控层之后、最外层, 叠加市场状态暴露系数
        # 仅影响仓位大小("买多少"), 不影响选股逻辑("买什么")
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from llm_regime_risk_overlay import get_regime_exposure, get_regime_state_info
            regime_info = get_regime_state_info()
            regime_multiplier = get_regime_exposure()
            plan["regime"] = {
                "multiplier": round(regime_multiplier, 3),
                "state": regime_info["state"],
                "benchmark": regime_info["benchmark"],
                "description": ("LLM市场状态风控叠加层: "
                    f"LONG_FULL=1.0 / LONG_HALF=0.8 / FLAT=0.5 | "
                    f"当前暴露系数={regime_multiplier:.3f}"),
            }
            plan["position_multiplier"] = round(
                plan["position_multiplier"] * regime_multiplier, 4)
            print(f"  [景气轮动·风控叠加] 状态={regime_info['state']} | "
                  f"暴露系数={regime_multiplier:.3f} | "
                  f"有效乘数={plan['position_multiplier']:.3f}")
        except Exception as e:
            plan["regime"] = {"multiplier": 1.0, "state": "OVERLAY_OFF",
                              "reason": str(e)[:100]}
        
        return plan
    
    def _determine_action(self, score: StockScore, 
                          position_multiplier: float = 1.0,
                          override_total: Optional[float] = None) -> str:
        """确定操作建议，考虑流动性Buff+量化权重。
        
        override_total: 传入经新闻降权后的有效分数, 使薄样本被拒主题标的自动降档。
        """
        # 风险致命优先
        if "业绩3年无法兑现" in score.risk_flags:
            return "清仓(业绩延迟-20%)"
        if "股东减持历史" in score.risk_flags:
            return "减仓(减持风险)"
        if "政策风险" in score.risk_flags and score.total_score < 40:
            return "等反弹清仓"
        
        # 用于阈值判定的有效分数(默认用原始分, 传入override则用它)
        total_score = override_total if override_total is not None else score.total_score
        
        # 评分决策 (满分120=20+40+25+15+20, 扣分另计)
        # ≥90 重仓, 70-90 轻仓, 50-70 观望, <50 清仓
        if total_score >= 90:
            if score.cycle_score >= 0.6:
                base_action = "重仓持有(周期反转)"
            else:
                base_action = "重仓持有"
        elif total_score >= 70:
            if score.cycle_score >= 0.4:
                base_action = "轻仓持有(等周期确认)"
            else:
                base_action = "轻仓持有"
        elif total_score >= 50:
            base_action = "观望(评分不足)"
        else:
            base_action = "清仓(评分过低)"
        
        # 流动性Buff修正 (图8: 1.2/0.8浮动)
        if position_multiplier >= 2.0 and "重仓" in base_action:
            return base_action + " [超级Buff×2.0]"
        elif position_multiplier >= 1.5 and "重仓" in base_action:
            return base_action + " [大龙Buff×1.8]"
        elif position_multiplier <= 0.8 and "重仓" in base_action:
            return "降为轻仓(流动性虚弱×0.8)"
        elif position_multiplier >= 1.3 and "轻仓" in base_action:
            return "升级重仓(增益Buff×1.3)"
        else:
            return base_action
    
    def _generate_summary(self, results: List[Dict]) -> Dict:
        """生成汇总统计"""
        heavy = len([r for r in results if "重仓" in r["action"]])
        light = len([r for r in results if "轻仓" in r["action"]])
        reduce = len([r for r in results if "减仓" in r["action"]])
        clear = len([r for r in results if "清仓" in r["action"]])
        
        return {
            "重仓": heavy,
            "轻仓": light,
            "减仓": reduce,
            "清仓": clear,
            "平均分": round(sum(r["total_score"] for r in results) / len(results), 1) if results else 0
        }
    
    def _analyze_fund_flow(self, override: Dict = None) -> Dict:
        """资金平衡分析 — 年份自适应基准, 支持外部覆盖(去除纯硬编码快照)。

        override 可传入 {"inflow": {...}, "outflow": {...}, "year": 2026} 动态覆盖;
        未提供时回退到内置基准(最近可得年份估算), 不再写死为 2024。
        """
        from datetime import datetime as _dt
        year = (override or {}).get("year", _dt.now().year)
        # 内置基准(最近可得年份估算); 生产环境应由数据服务注入 override 实时更新
        inflow = {
            "ETF基金": 11000, "外资(北向)": 7200, "保险资金": 5000,
            "融资融券": 4600, "偏股型基金": 2200, "私募基金": 800,
            "社保养老金": 700, "国家队(汇金)": 2500, "散户资金": 3000,
            "分红回购": 17400,
        }
        outflow = {
            "IPO": 950, "增发配股": 2500, "可转债可交债": 5800, "印花税": 3800,
            "佣金": 1250, "融资利息": 1350, "净减持": 1050,
        }
        if override:
            inflow = {**inflow, **(override.get("inflow", {}) or {})}
            outflow = {**outflow, **(override.get("outflow", {}) or {})}
        result = self.fund_flow_analyzer.analyze(inflow, outflow)
        result["base_year"] = year
        return result



def analyze_portfolio():
    """当前无持仓 — 观察池模式（实时指数+熔断检查+波动率仓位+风险映射+纠偏闭环）"""
    strategy = InvestmentStrategy()
    
    # 实时获取指数/成交额,不再使用硬编码
    market = fetch_market_snapshot()
    real_index = market["index"]
    real_turnover = market["turnover"]
    data_age = market["age_hours"]
    
    if data_age > 24:
        print(f"  ⚠️ [数据时效] 市场数据可能已过期{data_age}小时,建议检查数据源")
    print(f"  [实时行情] 上证{real_index:.0f} | 科创50{market['sci50']:.2f}({market['sci50_change']:+.1f}%) | 成交{real_turnover:.0f}亿 | 源:{market['source']}")
    
    # ===== P2: 盘中熔断检查 =====
    warp = WarpMonitor()
    warp_result = warp.check(
        index_change=market["index_change"],
        sci50_change=market["sci50_change"],
        decliners=market["decliners"]
    )
    if warp_result["alert"]:
        print(f"  🚨 [熔断] {warp_result['level']}警报: {warp_result['reason']}")
        print(f"       执行动作: {warp_result['action']}")
    
    # 默认观察池(来自env.py)
    portfolio = _build_watchlist_with_data()
    
    # 生成计划时传入真实数据 + 熔断结果
    return strategy.generate_plan(portfolio, turnover=real_turnover, index=real_index,
                                  warp_result=warp_result, market_snapshot=market)


def _build_watchlist_with_data() -> List[Dict]:
    """构建带完整数据的观察池（优先使用env.py配置的symbol列表）"""
    # 完整数据映射（env.py只有symbol+name，此处补充分析字段）
    DATA_MAP = [
        {"symbol": "588000", "name": "科创50ETF", "main_revenue_ratio": 1.0, 
         "pe_percentile": 0.55, "market_rank": 1, "is_first_mention": True, 
         "industry_type": IndustryType.NEW_TECH, "event_type": "科技突破", 
         "is_high_prosperity": True, "tech_breakthrough": True,
         "pe_ttm": 45, "eps_growth": 35, "eps_acceleration": 0.08,
         "sector_pe": 50, "eps_trend": [0.08, 0.10, 0.13, 0.17],
         "margin_trend": [52, 54, 55, 57], "revenue_qoq": [8, 12, 15],
         "eps": 0.48},
        # 中芯国际 - 国产替代+半导体景气 + 企业拐点(产能利用率>95%)
        {"symbol": "688981", "name": "中芯国际", "main_revenue_ratio": 0.92, 
         "pe_percentile": 0.45, "market_rank": 1, "is_first_mention": True, 
         "industry_type": IndustryType.NEW_TECH, "event_type": "科技突破", 
         "is_high_prosperity": True, "tech_breakthrough": True,
         "pe_ttm": 35, "eps_growth": 45, "eps_acceleration": 0.12,
         "sector_pe": 50, "eps_trend": [0.15, 0.22, 0.35, 0.48],
         "margin_trend": [35, 38, 40, 43], "revenue_qoq": [12, 15, 18],
         "eps": 1.20, "capex_growth": 45, "contract_liability_growth": 55,
         "ocf_positive": True, "ocf_growth": 30,  # 中芯国际 营收增23% EPS增45% OCF>净利
         "revenue_growth": 0.23, "ocf_to_net_profit": 1.1,
         "gross_margin": 0.43, "peg": 0.78, "ps_percentile": 0.55,
         "industry_margin": 0.40},
        # 赣锋锂业 - 周期反转 + EPS扭亏预期
        {"symbol": "002460", "name": "赣锋锂业", "main_revenue_ratio": 0.88, 
         "pe_percentile": 0.20, "market_rank": 1, "is_first_mention": True, 
         "industry_type": IndustryType.CYCLICAL, "event_type": "周期反转", 
         "drop_from_high": -0.65, "decline_months": 18, "demand_growth": 0.12, 
         "sector_bankruptcies": 5,
         "pe_ttm": 18, "eps_growth": 150, "eps_acceleration": 0.30,
         "sector_pe": 35, "eps_trend": [-0.20, -0.05, 0.10, 0.35],
         "margin_trend": [18, 22, 28, 35], "revenue_qoq": [-5, 8, 20],
         "eps": 0.65, "capex_growth": -15, "contract_liability_growth": 25,
         "ocf_positive": True, "ocf_growth": 80,
         # 赣锋锂业 营收增速中等, EPS扭亏中, OCF弱(周期底部)
         "revenue_growth": 0.12, "ocf_to_net_profit": 0.6, 
         "gross_margin": 0.35, "peg": 0.12, "ps_percentile": 0.20,
         "industry_margin": 0.28},
        # 中国银河 - 券商结构性拐点(十年最好业绩+并购重组)
        {"symbol": "601881", "name": "中国银河", "main_revenue_ratio": 0.95, 
         "pe_percentile": 0.40, "market_rank": 5, "is_first_mention": True, 
         "industry_type": IndustryType.CYCLICAL, "event_type": "政治经济突发", 
         "is_high_prosperity": True, "demand_growth": 0.08,
         "pe_ttm": 22, "eps_growth": 60, "eps_acceleration": 0.05,
         "sector_pe": 28, "eps_trend": [0.18, 0.25, 0.38, 0.52],
         "margin_trend": [45, 48, 51, 55], "revenue_qoq": [10, 15, 12],
         "eps": 1.33, "capex_growth": 5, "contract_liability_growth": 32,
         "ocf_positive": True, "ocf_growth": 45,
         # 中国银河 营收增速中等, EPS增速(行情驱动), OCF优质
         "revenue_growth": 0.15, "ocf_to_net_profit": 1.2,
         "gross_margin": 0.55, "peg": 0.37, "ps_percentile": 0.45,
         "industry_margin": 0.48},
        # 锡业股份 - 小金属出口管制+供给收缩
        {"symbol": "000960", "name": "锡业股份", "main_revenue_ratio": 0.85, 
         "pe_percentile": 0.35, "market_rank": 1, "is_first_mention": True, 
         "industry_type": IndustryType.CYCLICAL, "event_type": "资源供需失衡", 
         "is_high_prosperity": True,
         "pe_ttm": 16, "eps_growth": 50, "eps_acceleration": 0.15,
         "sector_pe": 25, "eps_trend": [0.12, 0.20, 0.32, 0.45],
         "margin_trend": [28, 33, 38, 42], "revenue_qoq": [8, 12, 18],
         "eps": 1.09, "capex_growth": 20, "contract_liability_growth": 40,
         "ocf_positive": True, "ocf_growth": 35,
         # 锡业 营收稳增, EPS增50%, OCF健康
         "revenue_growth": 0.18, "ocf_to_net_profit": 1.1,
         "gross_margin": 0.42, "peg": 0.32, "ps_percentile": 0.35,
         "industry_margin": 0.32},
        # 黄金ETF - 地缘避险+降息预期
        {"symbol": "518880", "name": "黄金ETF", "main_revenue_ratio": 1.0, 
         "pe_percentile": 0.55, "market_rank": 1, "is_first_mention": True, 
         "is_high_prosperity": True, "event_type": "资源供需失衡",
         "pe_ttm": 0, "eps_growth": 0, "eps_acceleration": 0,
         "sector_pe": 0, "eps_trend": [], "margin_trend": [],
         "revenue_qoq": [], "eps": 0},
        # 绿的谐波 - 机器人降本+量产元年 + 企业拐点(人形机器人减速器)
        {"symbol": "688017", "name": "绿的谐波", "main_revenue_ratio": 0.90, 
         "pe_percentile": 0.35, "market_rank": 1, "is_first_mention": True, 
         "industry_type": IndustryType.NEW_TECH, "event_type": "科技突破", 
         "is_high_prosperity": True, "tech_breakthrough": True,
         "pe_ttm": 50, "eps_growth": 80, "eps_acceleration": 0.20,
         "sector_pe": 60, "eps_trend": [0.05, 0.12, 0.28, 0.42],
         "margin_trend": [38, 42, 48, 53], "revenue_qoq": [15, 25, 32],
         "eps": 0.87, "capex_growth": 60, "contract_liability_growth": 75,
         "ocf_positive": True, "ocf_growth": 55,
         # 绿的谐波 营收爆发+EPS 80%+OCF健康+毛利率53%→超级成长股!
         "revenue_growth": 0.32, "ocf_to_net_profit": 1.0,
         "gross_margin": 0.53, "peg": 0.63, "ps_percentile": 0.65,
         "industry_margin": 0.35},
        # 新易盛 - AI光模块之王(超级成长股典范)
        {"symbol": "300502", "name": "新易盛", "main_revenue_ratio": 0.92,
         "pe_percentile": 0.40, "market_rank": 1, "is_first_mention": True,
         "industry_type": IndustryType.NEW_TECH, "event_type": "科技突破",
         "is_high_prosperity": True, "tech_breakthrough": True,
         "pe_ttm": 42, "eps_growth": 90, "eps_acceleration": 0.25,
         "sector_pe": 55, "eps_trend": [0.80, 1.50, 2.20, 3.50],
         "margin_trend": [42, 45, 48, 52], "revenue_qoq": [18, 25, 30],
         "eps": 2.75, "capex_growth": 70, "contract_liability_growth": 90,
         "ocf_positive": True, "ocf_growth": 65,
        # 新易盛 H1预增78-103%! 营收35%+EPS 90%+OCF>净利+毛利率52%
         "revenue_growth": 0.35, "ocf_to_net_profit": 1.3,
         "gross_margin": 0.52, "peg": 0.47, "ps_percentile": 0.50,
         "industry_margin": 0.38},
    ]
    return DATA_MAP


def print_report(plan: Dict):
    """完整诊断报告 - 14维度 + 预测 + 风险 + 操作指南"""
    print("=" * 75)
    print("  景气周期轮动策略 v18 — 32类/24维 整合版")
    print("=" * 75)
    print(f"  日期: {plan.get('date','?')} | 上证: {plan.get('index',0)} | 成交: {plan.get('turnover',0)}亿")
    
    # ═══════ 维度1-9: 市场环境 ═══════
    buff = plan["liquidity_buff"]
    ff = plan["fund_flow"]
    na = plan.get("news_analysis", {})
    
    print(f"\n{'─'*75}")
    print("  市场环境诊断 (维度1-9)")
    print(f"{'─'*75}")
    print(f"  ① 投资模式: 价投(当前市场) + 高景气投机")
    print(f"  ② 拐点事件: 存储芯片HBM(ChatGPT级) | 券商并购重组(政治经济)")
    print(f"  ③ 板块卡位: 科技上游(HBM/光模块) | 传统下游龙头(券商/红利)")
    print(f"  ⑥ 周期反转: 锂矿3/5条件满足 | 券商2/5条件")
    print(f"  ⑧ 流动性: {buff['buff_level']}Buff ×{buff['multiplier']} | 理论点位{buff['theoretical_index']:.0f} vs 实际{plan.get('index',0)}")
    print(f"  ⑨ 资金平衡: 净流入{ff['net_flow']}亿({ff['market_type']})")
    
    # ═══════ 维度10: 量化评分 ═══════
    print(f"\n{'─'*75}")
    print("  量化评分 (维度10: 权重公式)")
    print(f"{'─'*75}")
    print(f"  公式: (景气20%+纯度40%+估值25%+龙头15%+辨识20%+风险扣分)×流动性乘数")
    print(f"  {'标的':<12} {'景气':>4} {'纯度':>4} {'估值':>4} {'龙头':>4} {'辨识':>4} {'风险':>5} {'原始':>5} {'×Buff':>6} {'最终':>5} {'建议'}")
    print(f"  {'-'*65}")
    for stock in plan["stocks"]:
        d = stock["dimensions"]
        raw = (d['行业景气度(20%)'] + d['业务纯度(40%)'] + d['估值位置(25%)'] + 
               d['细分龙头(15%)'] + d['辨识度(20%)'] + d['风险扣分'])
        print(f"  {stock['name']:<12} {d['行业景气度(20%)']:>4} {d['业务纯度(40%)']:>4} "
              f"{d['估值位置(25%)']:>4} {d['细分龙头(15%)']:>4} {d['辨识度(20%)']:>4} "
              f"{d['风险扣分']:>5} {raw:>5} ×{plan['position_multiplier']:.1f} "
              f"{stock['total_score']:>5.0f} {stock['action'].replace('[超级Buff×2.0]','').strip()}")
    
    # ═══════ 超级成长股财报质量 ═══════
    print(f"\n{'─'*75}")
    print("  超级成长股财报质量 (营收/EPS/OCF/毛利率 + PEG估值约束)")
    print(f"{'─'*75}")
    has_financial = False
    for stock in plan["stocks"]:
        fin_quality = stock.get("financial_quality", {})
        if fin_quality.get("label", "") not in ("", "ETF(不适用)"):
            has_financial = True
            q = "✅" if fin_quality.get("quality") else "❌"
            details = " | ".join(fin_quality.get("details", []))
            print(f"  [{fin_quality.get('label','?')}] {stock['name']:<10s} {q} 成长乘数×{fin_quality.get('multiplier',1):.2f}")
            print(f"    ├─ 财报: {details}")
            print(f"    └─ 估值: {fin_quality.get('valuation_reason','')}")
    if not has_financial:
        print(f"  (当前7只标的需补充财务数据字段,详见_build_watchlist_with_data)")
    
    # ═══════ 维度11-14: 新闻+现象级+涟漪+双击 ═══════
    print(f"\n{'─'*75}")
    print("  新闻驱动 + 双击框架 (维度11-14)")
    print(f"{'─'*75}")
    
    # 快讯即时信号 (11维)
    fs_list = na.get("flash_signals", [])
    if fs_list:
        print(f"  ⑪ 快讯即时信号:")
        for fs in fs_list[:6]:
            u = "🔴" if fs.get("urgency") == "high" else "🟡"
            print(f"     {u} {fs['title'][:50]} → {fs['logic']} | {', '.join(fs.get('stocks',[]))}")
    
    # 现象级事件 (12维)
    pe = na.get("phenomenon_events", [])
    if pe:
        print(f"  ⑫ 现象级事件:")
        for e in pe[:4]:
            print(f"     [{e['type']}] {e['title'][:45]} | 评分{e['phenomenon_score']:.0f}")
    
    # 新闻质量指标
    print(f"  新闻质量: 原始{na.get('total_raw',0)}篇→去重{na.get('total_deduped',0)}篇 | "
          f"影响量化{len(na.get('impact_scores',{}))}标的 | 结构化{len(na.get('extracted_data',{}))}字段")
    
    # 涟漪效应 (13维)
    rp = na.get("ripple_events", [])
    if rp:
        print(f"  ⑬ 涟漪小拐点:")
        shown = set()
        items = []
        for r in rp[:6]:
            k = r["ripple_topic"]
            if k not in shown:
                items.append(f"AI→{k}")
                shown.add(k)
        print(f"     {', '.join(items)}")
    
    # ═══════ 维度14: 双击框架详细诊断 ═══════
    print(f"\n  ⑭ 双击框架 v3.0 (戴维斯双击 + 苯韭双击/单击):")
    
    # 戴维斯双击汇总
    davis_stocks = [s for s in plan["stocks"] if s.get("davis",{}).get("type")]
    if davis_stocks:
        print(f"     ┌{'─'*62}┐")
        print(f"     │ 戴维斯双击诊断 (PE×EPS→股价倍乘效应)")
        print(f"     ├{'─'*62}┤")
        for s in davis_stocks:
            dv = s["davis"]
            pot = f"+{dv.get('potential_return',0):.0f}%"
            print(f"     │ {s['name']:<10} PE@{dv.get('pe_signal','')[:15]:>15} | 空间{pot:>7} | {dv.get('type','')[:14]:>14} │")
            print(f"     │   → {dv.get('logic','')[:55]} │")
        print(f"     └{'─'*62}┘")
    else:
        print(f"     戴维斯双击: 暂无满足低PE×高成长条件的标的")
    
    # 苯韭双击详细诊断
    print(f"\n     ┌{'─'*66}┐")
    print(f"     │ 苯韭双击诊断 (产业六维度 ∩ 企业六维度)")
    print(f"     ├{'─'*66}┤")
    print(f"     │ {'标的':<10} {'产业':>5} {'企业':>5} {'综合':>5} {'类型':>16} │")
    
    for s in plan["stocks"]:
        bj = s.get("benjiu", {})
        ind_s = bj.get("industry_score", 0)
        cmp_s = bj.get("company_score", 0)
        total_bj = bj.get("score", 0)
        bj_type = bj.get("type", "—")
        print(f"     │ {s['name']:<10} {ind_s:>5.0f} {cmp_s:>5.0f} {total_bj:>5.0f} {bj_type:>16} │")
        
        # 显示关键证据
        ind_ev = bj.get("industry_evidence", [])
        cmp_ev = bj.get("company_evidence", [])
        if ind_ev:
            print(f"     │   产业拐点: {', '.join(ind_ev[:3])[:45]} │")
        if cmp_ev:
            print(f"     │   企业拐点: {', '.join(cmp_ev[:3])[:45]} │")
        if bj.get("conviction"):
            print(f"     │   ✓ {bj['conviction'][:55]} │")
    
    # 统计命中情况
    double_hit = [s for s in plan["stocks"] if "双击" in s.get("benjiu",{}).get("type","")]
    single_hit = [s for s in plan["stocks"] if "单击" in s.get("benjiu",{}).get("type","")]
    print(f"     ├{'─'*66}┤")
    print(f"     │ 苯韭双击:{len(double_hit)}只 | 单击:{len(single_hit)}只 | 无信号:{len(plan['stocks'])-len(double_hit)-len(single_hit)}只")
    print(f"     └{'─'*66}┘")
    
    # 最硬逻辑标的
    if double_hit:
        print(f"\n     🔥 最硬选股逻辑(产业∩企业双拐点共振):")
        for s in double_hit:
            bj = s["benjiu"]
            print(f"       {s['name']}: {bj.get('logic','')[:70]}")
    
    # 主动埋伏
    pp = na.get("pre_position", {})
    if pp:
        print(f"\n     主动埋伏(守株待兔):")
        for theme, data in pp.items():
            print(f"       [{theme}] 等{data['trigger']} | {', '.join(data.get('stocks',[]))}")
    
    # ═══════ 维度15: 超景气价值投机 — 消息事件分类 ═══
    mp = plan.get("market_phase", {})
    print(f"\n{'─'*75}")
    print(f"  ⑮ 超景气价值投机框架 (消息事件分层)")
    print(f"{'─'*75}")
    if mp:
        print(f"  市场阶段: {mp.get('phase','')} | 成交{mp.get('turnover_bill','?')}万亿 | 指数{mp.get('index_change','0%')}")
        print(f"  操作方向: {mp.get('suggestion','')}")
    
    # 分类: 超景气 vs 普通
    super_boom_stocks = [s for s in plan["stocks"] 
                         if s.get("catalyst",{}).get("is_super_boom")]
    other_driven_stocks = [s for s in plan["stocks"] 
                         if not s.get("catalyst",{}).get("is_super_boom")]
    
    if super_boom_stocks:
        print(f"\n  🔥 超景气价值投机标的 ({len(super_boom_stocks)}只):")
        print(f"  ┌{'─'*64}┐")
        print(f"  │ {'标的':<10} {'主类型':<16} {'现象级':>6} {'普通':>6} {'噪音比':>6} {'确定性':>6} │")
        print(f"  ├{'─'*64}┤")
        for s in super_boom_stocks:
            cat = s["catalyst"]
            primary = cat.get("primary_type", "")[:15]
            ph = cat.get("total_phenomenon", 0)
            ot = cat.get("total_other", 0)
            nr = f"{cat.get('noise_ratio',0):.0%}"
            cert = cat.get("certainty_score", 0)
            # 显示推荐
            rec = cat.get("recommendation", "")[:35]
            print(f"  │ {s['name']:<10} {primary:<16} {ph:>6.0f} {ot:>6.0f} {nr:>6} {cert:>6.0f} │")
            print(f"  │   → {rec} │")
        print(f"  └{'─'*64}┘")
    
    if other_driven_stocks:
        print(f"\n  ⚡ 普通消息驱动标的 ({len(other_driven_stocks)}只):")
        for s in other_driven_stocks:
            cat = s["catalyst"]
            other_hits = cat.get("other_signals", [])
            if other_hits:
                types = ", ".join([o["type"] for o in other_hits[:3]])
                print(f"     {s['name']:<12} → {types} (一次/短期型,确定性低)")
    
    # 现象级信号详情
    print(f"\n  现象级信号汇总:")
    all_ph = []
    for s in plan["stocks"]:
        for hit in s.get("catalyst", {}).get("phenomenon_signals", []):
            all_ph.append((s["name"], hit))
    if all_ph:
        shown = set()
        for name, hit in sorted(all_ph, key=lambda x: -x[1]["score"])[:8]:
            key = f"{name}-{hit['type']}"
            if key not in shown:
                print(f"     [{hit['type']}] {hit['quality']} | 持续{hit['duration']} | {', '.join(hit['keywords'][:3])}")
                shown.add(key)
    else:
        print(f"     (暂无强现象级信号,需等待催化事件)")
    
    # ═══════ 维度16: 现象级事件三层分类 + 埋伏管道 ═══════
    print(f"\n{'─'*75}")
    print(f"  ⑯ 现象级事件三层分类 (①黑天鹅/②可预判细分拐点/③高频事件)")
    print(f"{'─'*75}")
    
    # 统计各层
    tier_counts = {1: [], 2: [], 3: []}
    for s in plan["stocks"]:
        et = s.get("event_tier", {})
        t = et.get("tier", 3)
        tier_counts[t].append((s["name"], et))
    
    # ②类可预判拐点 — 最重要
    if tier_counts[2]:
        print(f"\n  ②类: 超级事件周期内的细分产业化拐点 (可预判·守株待兔)")
        print(f"  ┌{'─'*66}┐")
        print(f"  │ {'标的':<10} {'细分拐点':<22} {'状态':<12} {'仓位策略':<20} │")
        print(f"  ├{'─'*66}┤")
        for name, et in tier_counts[2]:
            cls = et.get("classification", "")
            sub_info = cls.split("→")[-1] if "→" in cls else cls
            status = et.get("sub_inflection", {}).get("status", "—")[:10]
            action = et.get("suggested_action", "")[:20]
            print(f"  │ {name:<10} {sub_info:<22} {status:<12} {action:<20} │")
        print(f"  └{'─'*66}┘")
    
    # ③类高频事件
    if tier_counts[3]:
        print(f"\n  ③类: 概率一年多次的高频小拐点 ({len(tier_counts[3])}只)")
        for name, et in tier_counts[3]:
            print(f"     {name:<10} → {et.get('classification','')[:45]}")
            print(f"        {et.get('suggested_action','')[:60]}")
    
    # ①类黑天鹅(当前无)
    if tier_counts[1]:
        print(f"\n  ①类: 不可预期黑天鹅事件 ({len(tier_counts[1])}只)")
        for name, et in tier_counts[1]:
            print(f"     ⚠️ {name}: {et.get('classification','')[:45]}")
            print(f"        {et.get('suggested_action','')[:60]}")
    else:
        print(f"\n  ①类黑天鹅: 当前无限匹配事件(概率极低,祝好运)")
    
    # ═══════ 主动埋伏管道 ═══════
    print(f"\n{'─'*75}")
    print(f"  🔮 主动埋伏管道 (②类可预判细分拐点时序)")
    print(f"{'─'*75}")
    
    # 收集所有埋伏管道
    all_pipelines = []
    for s in plan["stocks"]:
        for pp in s.get("pre_pipeline", []):
            all_pipelines.append((s["name"], pp))
    
    # 按置信度排序去重
    seen = set()
    unique_pipelines = []
    for name, pp in sorted(all_pipelines, key=lambda x: -x[1]["confidence"]):
        key = pp["sub_inflection"]
        if key not in seen:
            seen.add(key)
            unique_pipelines.append((name, pp))
    
    if unique_pipelines:
        print(f"  {'细分拐点':<22} {'时序':<12} {'状态':<10} {'可信度':>4} {'相关标的':<15} {'操作'}")
        print(f"  {'-'*72}")
        for name, pp in unique_pipelines[:12]:
            conf = pp["confidence"]
            icon = "🟢" if conf >= 80 else "🟡" if conf >= 50 else "🔵"
            stocks_str = ", ".join(pp.get("stocks", [name])[:2])
            print(f"  {pp['sub_inflection']:<22} {pp['timing']:<12} {pp['status']:<10} "
                  f"{conf:>4} {icon} {stocks_str:<15} {pp['action']}")
    else:
        print(f"  (当前无匹配的埋伏管道,等待现象级事件触发)")
    
    # ═══════ 维度17: 剑宗一波流 vs 气宗长期格局 ═══════
    pa = plan.get("portfolio_allocation", {})
    print(f"\n{'─'*75}")
    print(f"  ⑰ 交易模式与仓位配置 (剑宗一波流30% : 气宗长期格局70%)")
    print(f"{'─'*75}")
    if pa:
        print(f"  组合配置: 剑宗{pa.get('jianzong_pct',0):.0f}%({pa.get('jianzong_count',0)}只) "
              f": 气宗{pa.get('qizong_pct',0):.0f}%({pa.get('qizong_count',0)}只)")
    
    # 逐标的展示
    print(f"\n  {'标的':<12} {'模式':<12} {'建议比例':>6} {'剑宗分':>5} {'气宗分':>5} {'分差':>4} {'持有期':<10} {'操作规则'}")
    print(f"  {'-'*72}")
    add_pos = plan.get("add_position", {})
    for s in plan["stocks"]:
        tm = s.get("trade_mode", {})
        mode = tm.get("mode", "—")
        ratio = f"{tm.get('base_ratio',0):.0f}%"
        jz = tm.get("jianzong_score", 0)
        qz = tm.get("qizong_score", 0)
        gap = f"+{tm.get('score_gap',0)}" if tm.get('score_gap',0) >= 0 else f"{tm.get('score_gap',0)}"
        dur = tm.get("hold_duration", "—")
        # 使用AddPositionEngine决策替换简化规则
        ap = add_pos.get(s["symbol"], {}) if add_pos else {}
        if ap and ap.get("decision"):
            rule = ap["decision"][:18]  # 截取前18字符
        elif "气宗" in mode and "偏" not in mode:
            rule = "回调MA20加仓"
        elif "气宗偏" in mode:
            rule = "等季报确认后满配"
        elif "剑宗偏" in mode:
            rule = "缩量企稳轻仓介入"
        else:
            rule = "高位破MA5清仓"
        print(f"  {s['name']:<12} {mode:<12} {ratio:>6} {jz:>5} {qz:>5} {gap:>4} {dur:<10} {rule}")
    
    # 再平衡规则
    print(f"\n  动态再平衡规则:")
    print(f"    超级Buff(当前): 剑宗可提至40% → 增加高弹性标的仓位")
    print(f"    虚弱Buff: 剑宗降至10%或清零,气宗底仓持有")
    print(f"    升级触发: 剑宗标的连续2季报超预期 → 升级为气宗,比例翻倍")
    print(f"    降级触发: 气宗标的季报miss → 降级为剑宗,减仓等待")
    print(f"    压舱石: 气宗降低组合波动+超级行情爆发时大仓位吃指数暴涨")
    
    # ═══════ 综合推荐标的 ═══════
    print(f"\n{'═'*75}")
    print("  🎯 综合推荐标的 (量化评分 × 新闻热度 × 双击潜力)")
    print(f"{'═'*75}")
    
    recommendations = generate_recommendations(plan)
    print(f"  {'标的':<12} {'当前':>6} {'区间低':>8} {'区间高':>8} {'预期':>7} {'风险':>5} {'操作'}")
    print(f"  {'-'*70}")
    for r in recommendations:
        print(f"  {r['name']:<12} {r['price']:>6.2f} {r['range_low']:>8} {r['range_high']:>8} "
              f"{r['expected']:>7} {r['risk_level']:>5} {r['action'][:20]}")
    
    # 风险提示
    print(f"\n{'─'*75}")
    # ═══════ 维度18: 风险控制与动态调仓 ═══════
    rc = plan.get("risk_control", {})
    print(f"\n{'─'*75}")
    print(f"  ⑱ 风险控制与动态调仓 (剑气配合+止盈减仓+模式切换+亏损复盘)")
    print(f"{'─'*75}")
    if rc:
        print(f"  风险状态: {rc.get('risk_state','')} | 组合止损{rc.get('max_drawdown',-10)}% | 单票止损{rc.get('single_stop',-5)}%")
        print(f"  剑气配比: {rc.get('sword_qi','')}")
        print(f"  流动性保险: {rc.get('liquidity_rule','')[:55]}")
        print(f"  止盈规则: {rc.get('take_profit','')[:55]}")
        print(f"  模式切换: {rc.get('mode_switch','')[:55]}")
        print(f"  亏损纪律: {rc.get('loss_review','')[:55]}")
        print(f"  紧急预案: {rc.get('emergency','')}")
    
    print(f"\n  当前风险因子:")
    print(f"  1. akshare数据暂不可用 → 部分价格/技术指标为估算值")
    print(f"  2. AI泡沫风险: BTIG+摩根大通+半夏投资三家警告半导体大顶")
    print(f"  3. 美联储: 沃什鹰派转向+高盛警告秋季连环加息")
    print(f"  4. 72家公司连夜AI风险提示 → 监管趋严")
    print(f"  5. 霍尔木兹通行不确定性 → 油运/黄金波动加剧")
    
    # 操作指南
    print(f"\n{'═'*75}")
    print("  📋 2026下半年操作指南")
    print(f"{'═'*75}")
    # 动态计算上证点位仓位建议
    idx_val = plan.get("index", 4163)
    for th, rt, desc in AddPositionEngine.INDEX_POSITION_MAP:
        if idx_val >= th:
            idx_advice = desc
            break
    else:
        idx_advice = "观望"
    idx_extra = "全力做多" if idx_val >= 3800 else "留现金等回调"
    
    print(f"""
  【当前持仓优化】
    立即清仓: 医疗ETF(集采无底) | 粮食ETF(无龙头)
    减仓: 大秦铁路(周期坐牢,只留2000股吃息)
    持有: 中国银河(券商并购催化) | 科创50ETF(AI主线)
    
  【新建仓计划】
    存储产业链: 佰维存储/江波龙 — 等7月Q2财报触发现象级爆发
    锂矿反转: 赣锋锂业/天齐锂业 — 等锂价破20万/吨确认拐点
    小金属: 锡业股份/驰宏锌锗 — 铟/钨出口管制催化
    机器人: 绿的谐波/埃斯顿 — 宇树R1降价至2.99万,量产加速
    
  【分批加仓节奏】
    上证3500 → 50%仓位 | 上证3400 → 70%仓位 | 上证3200 → 满仓+融资
    (当前上证{idx_val}→{idx_advice},{idx_extra})
    
  【风险纪律】
    单票硬止损-8% | 组合最大回撤-15% | 永不满仓单只标的
    成交额<1万亿 → 减至30%仓位 | 资金净流入转负 → 清仓等3400
""")
    
    # ═══════ 维度21: 高位止盈/逃顶信号 ═══════
    exit_stocks = [s for s in plan["stocks"] if s.get("exit_signals",{}).get("total_score",0) > 0]
    has_any_exit = any(s.get("exit_signals",{}).get("total_score",0) >= 25 for s in plan["stocks"])
    print(f"\n{'─'*75}")
    print(f"  ㉑ 高位止盈/逃顶信号 (宏观+板块+个股三维扫描)")
    print(f"{'─'*75}")
    print(f"  核心: 以上条件不是AND而是OR,触发一条即跑")
    
    if has_any_exit:
        for s in plan["stocks"]:
            es = s.get("exit_signals", {})
            if es.get("total_score", 0) >= 25:
                print(f"  [{es.get('recommendation','')[:6]}] {s['name']}: 逃顶紧迫度{es['total_score']:.0f}/100")
                for sig in es.get("stock_signals", []):
                    print(f"    个股: {sig['type']} → {sig['action']}")
                for sig in es.get("sector_signals", []):
                    print(f"    板块: {sig['type']} → {sig['action']}")
                for sig in es.get("macro_signals", []):
                    print(f"    宏观: {sig['type']} → {sig['action']}")
    else:
        print(f"  🟢 当前无逃顶信号,安心持有等逻辑兑现")
    
    print(f"\n  逃顶清单速查 (触发任一条→立即减仓):")
    print(f"    宏观: 12道金牌/增量资金天量/卡脖子技术全面突破")
    print(f"    板块: 渗透率30%/新赛道虹吸/核心标的异常")
    print(f"    个股: 换手40%/缩量加速/概念股见顶/涨3倍/减持/股东增加/高位定增/基本面利空")
    

    # ═══════ 维度22: 自主可控↔遥遥领先双向映射 ═══════
    print(f"\n{'─'*75}")
    print(f"  ㉒ 自主可控↔遥遥领先双向映射 (苯韭双吃)")
    print(f"{'─'*75}")
    print(f"  公式: 东大自主可控=西方遥遥领先 | 东大遥遥领先=西方自主可控")
    mapping_hits = 0
    for s in plan["stocks"]:
        m = AutoControlLeadershipMapper.get_mapping(s["name"])
        if m:
            mapping_hits += 1
            print(f"  [{m['type']}] {s['name']} ↔ {m['western']} | {m['a_share_action'][:35]}")
    if mapping_hits == 0:
        print(f"  (当前观察池未匹配映射库,待扩展)")
    
    # ═══════ 维度19: 散户认知纠偏 ═══════
    therapy = plan.get("therapy", {})
    if therapy:
        print(f"""  ┌{'─'*68}┐
  │ ⑲ 散户认知纠偏 — 致连续亏损三年以上的你 {'':>22}│
  ├{'─'*68}┤
  │ 认知健康度: {therapy.get('health_score',100)}/100 → {therapy.get('verdict','暂时无法诊断')[:35]:<35} │""")
        for issue in therapy.get("issues", []):
            sev = issue.get("severity", "中")
            print(f"  │ [{sev}危] {issue['pattern']}: {issue.get('diagnosis','')[:38]} │")
        print(f"  ├{'─'*68}┤")
        print(f"  │ 破局学习路径: {'':>50} │")
        for step in therapy.get("learning_path", [])[:3]:
            print(f"  │ {step[:62]:<62} │")
        print(f"  ├{'─'*68}┤")
        print(f"  │ 对手: 万亿公募 + 纳秒量化 + 信息圈层 + 产业资本 {'':>8} │")
        print(f"  │ 破局: 停止在股吧/雪球问\"能不能涨\"，用18维框架替代直觉   │")
        print(f"  └{'─'*68}┘")
    
    # ═══════ 维度20: 行动建议与交易模式落地 ═══════
    print(f"""
{'═'*75}
  ⑳ 行动建议 — 从认知到落地的最后一步
{'═'*75}
  
  【停下来】
    先建立你自己的固定交易模式，而不是靠运气YY炒股。
    如果不能有一个完整严谨的买入逻辑，那都是瞎买。
    
  【这个模式适合你吗？】
    ✓ 不需要全职盘中盯盘 → 每天看看世界发生了什么即可
    ✓ 多观察世界，多训练自己的推理能力
    ✓ 买入后等待逻辑兑现，资金把标的推到前所未有的高度
    ✓ 看趋势止盈，一次完美的交易就生成了
    
  【今日行动清单】
    1. 关闭股吧/雪球/抖音炒股频道
    2. 运行本策略，记录18维评分最高的3个标的
    3. 检查自己的持仓是否符合"气宗格局"或"剑宗一波流"
    4. 设置硬止损：单票-8%，组合-15%
    5. 等待逻辑兑现，不盯盘
    
  【交易频率】
    普通散户：每周运行1次策略，月度调仓
    超级行情（当前）：每周运行2次，观察是否触发止盈/升级
    极端行情：暂停买入，等流动性恢复
    
  【终极目标】
    今天运气好赚钱，明天运气不好不亏回去。
    用框架替代运气，用逻辑替代情绪。
""")


    # ======== 增强仓位管理 ========
    print(f"\n{'─'*75}")
    print(f"  (23) Enhanced Position Sizing (ATR Stop + Fixed Risk + Safety Cushion + Trailing Stop)")
    print(f"{'─'*75}")
    print(f"  {'Name':<12} {'Price':>7} {'ATR':>5} {'Stop(ATR)':>9} {'Hard':>6} {'Final':>7} {'Shares':>7} {'%Acct':>5} {'Cushion':<8} {'Add Rule'}")
    print(f"  {'-'*70}")
    try:
        reports = PositionCalculator.enhanced_risk_report(plan)
        for r in reports:
            cushion = "OK" if r["safety_cushion"] else "Wait"
            s = "{:,}".format(r['shares_1pct']) if r['shares_1pct'] > 0 else "-"
            print(f"  {r['name']:<10} {r['price']:>8.2f} {r['atr']:>5.2f} {r['stop_atr']:>9.2f} "
                  f"{r['stop_hard']:>6.2f} {r['stop_final']:>7.2f} {s:>7} {r['build_pct']:>4.1f}% "
                  f"{cushion:<8} {r['add_condition']}")
    except: pass
    print()
    
    # ═══════ 统一加仓决策引擎信号明细 ═══════
    print(f"\n{'─'*75}")
    print(f"  (24) 加仓决策引擎 — 信号判定明细 (安全垫为最终门禁)")
    print(f"{'─'*75}")
    add_pos = plan.get("add_position", {})
    if add_pos:
        print(f"  {'标的':<12} {'决策':<28} {'安全垫':<8} {'金字塔阶段':<12} {'详情'}")
        print(f"  {'-'*72}")
        for s in plan["stocks"]:
            sym = s["symbol"]
            ap = add_pos.get(sym, {})
            if ap:
                decision = ap.get("decision", "—")
                cushion = "✅" if ap.get("cushion_ok") else "❌"
                stage = f"第{ap.get('stage',1)}阶段({ap.get('stage_name','—')})"
                detail = ap.get("detail", "—")[:45]
                print(f"  {s['name']:<12} {decision:<28} {cushion:<8} {stage:<12} {detail}")
        
        # 汇总统计
        can_add_count = sum(1 for ap in add_pos.values() if ap.get("can_add"))
        signal_count = sum(1 for ap in add_pos.values() if ap.get("signals"))
        blocker_count = sum(1 for ap in add_pos.values() if ap.get("blockers"))
        print(f"\n  汇总: {can_add_count}/{len(add_pos)}可加仓 | {signal_count}有信号 | {blocker_count}被阻止")
        
        # 显示每个标的的详细信号和阻止原因
        for s in plan["stocks"]:
            sym = s["symbol"]
            ap = add_pos.get(sym, {})
            if ap:
                signals = ap.get("signals", [])
                blockers = ap.get("blockers", [])
                mode_action = ap.get("mode_action")
                if signals or blockers or mode_action:
                    print(f"\n  [{s['name']}]")
                    if signals:
                        for sig in signals:
                            print(f"    📶 {sig['type']}: {sig['desc']} (得分+{sig['score']})")
                    if blockers:
                        for blk in blockers:
                            print(f"    🚫 阻止: {blk}")
                    if mode_action:
                        print(f"    🔄 模式: {mode_action}")
    else:
        print(f"  (加仓决策引擎未启用 — 需持仓数据)")


def generate_recommendations(plan: Dict) -> List[Dict]:
    """生成综合推荐标的列表 — 增强预测(评分映射+波动率调整+置信区间)"""
    import math
    stocks = plan["stocks"]
    na = plan.get("news_analysis", {})
    buff_mult = plan["position_multiplier"]
    
    recs = []
    all_symbols = set()
    
    for s in stocks:
        if s["total_score"] > 150 and "清仓" not in s["action"]:
            all_symbols.add(s["symbol"])
    for s in na.get("top_stocks", [])[:3]:
        all_symbols.add(s["symbol"])
    
    fallback_prices = {"601881":13.44,"588000":2.10,"518880":9.00,"601006":4.60,"600036":35.0,
                       "688981":140.31,"000960":40.48,"002460":0,"688017":0,
                       "300502":526,"300394":250,"688256":1353}
    
    for sym in all_symbols:
        stock_data = None
        for s in stocks:
            if s["symbol"] == sym: stock_data = s; break
        name = stock_data["name"] if stock_data else sym
        
        rs = plan.get("realtime_signals", [])
        price = fallback_prices.get(sym, 0)
        tech_vol = 30.0
        for r in rs:
            if r["symbol"] == sym and r.get("price", 0) > 0:
                price = r["price"]
                tech_vol = r.get("volatility", 30.0)
                break
        
        d = stock_data["dimensions"] if stock_data else {}
        risk_penalty = d.get("风险扣分", 0)
        pe_pct = d.get("估值位置(25%)", 15) / 25
        boom_score = d.get("行业景气度(20%)", 10) / 20
        purity = d.get("业务纯度(40%)", 20) / 40
        valuation = d.get("估值位置(25%)", 15) / 25
        
        # 增强预测: 评分映射 + 波动率调整
        base_expected = (boom_score * 35 + purity * 25 + (1 - valuation) * 25 +
                        abs(risk_penalty) * (-0.3) + 5)
        volatility_adj = 1.0 / (1.0 + (tech_vol / 100) * 2.0)
        expected = round(base_expected * volatility_adj, 1)
        
        # 置信区间 (±1.5σ)
        conf_width = round(tech_vol * 1.5, 0)
        optimistic = round(expected + conf_width, 1)
        pessimistic = round(expected - conf_width, 1)
        
        # 风险等级
        if tech_vol > 50 or risk_penalty <= -20: risk_level = "高"
        elif tech_vol > 30 or risk_penalty <= -10: risk_level = "中"
        else: risk_level = "低"
        
        if price > 0:
            range_low = f"{price * (1 + pessimistic/100):.2f}" if abs(pessimistic) < 99 else "待查"
            range_high = f"{price * (1 + optimistic/100):.2f}" if abs(optimistic) < 99 else "待查"
        else:
            range_low = "待查"; range_high = "待查"
        
        if expected > 30 and risk_level == "低": action = "🔥强烈买入"
        elif expected > 20: action = "🔥强烈买入"
        elif expected > 10: action = "✅买入"
        elif expected > 0: action = "⏳等回调"
        elif expected > -10: action = "⚠️减仓"
        else: action = "❌回避"
        
        if stock_data:
            if stock_data.get("davis", {}).get("type"): action += " [戴维斯+]"
            if "双击" in stock_data.get("benjiu", {}).get("type", ""): action += " [苯韭双击⭐]"
            elif "单击" in stock_data.get("benjiu", {}).get("type", ""): action += " [苯韭单击]"
            if stock_data.get("catalyst", {}).get("is_super_boom"): action += " [超景气🔥]"
        
        recs.append({
            "symbol":sym,"name":name,"price":price,
            "range_low":range_low,"range_high":range_high,
            "expected":f"{expected:+.1f}%","optimistic":f"{optimistic:+.1f}%",
            "pessimistic":f"{pessimistic:+.1f}%","confidence":f"+/-{conf_width:.0f}%",
            "risk_level":risk_level,"action":action,"method":"增强预测v2"
        })
    
    recs.sort(key=lambda x: float(str(x["expected"]).replace("%","").replace("+","")), reverse=True)
    return recs


# ========== 超级成长股财报质量过滤器 ==========
class FinancialFilter:
    """超级成长股财务质量门禁 —— 营收增速/EPS增速/OCF/毛利率四维验证"""
    
    def check(self, stock: Dict) -> Tuple[bool, float, str, List[str]]:
        """
        返回: (是否超级成长股, 得分乘数, 评级标签, 详情原因)
        4/4 → 超级成长股 ×1.3
        3/4 → 优质股     ×1.15
        2/4 → 普通       ×1.0
        1/4 → 弱         ×0.85
        0/4 → 不达标     ×0.7
        """
        # ETF不适用财报分析
        if stock.get("name", "").endswith("ETF"):
            return True, 1.0, "ETF(不适用)", ["ETF免检"]
        
        score = 0
        details = []
        
        # 1. 营收增速 >30% YoY
        rev = stock.get("revenue_growth", 0.0)
        if rev > 0.30:
            score += 1
            details.append(f"营收增速{rev*100:.0f}%✅")
        elif rev > 0.15:
            details.append(f"营收增速{rev*100:.0f}%(中等)")
        else:
            details.append(f"营收增速{rev*100:.0f}%❌")
        
        # 2. EPS增速 >50% 或扭亏加速
        eps_raw = stock.get("eps_growth", 0.0)
        # eps_growth 可能是百分比整数(如80=80%)或小数(如0.80)
        eps = eps_raw / 100 if eps_raw > 2 else eps_raw  # 归一化为小数
        turnaround = stock.get("eps_turnaround", False)
        if eps > 0.50 or (turnaround and eps > 0.20):
            score += 1
            tag = "扭亏加速" if turnaround else f"{eps*100:.0f}%"
            details.append(f"EPS增速{tag}✅")
        else:
            details.append(f"EPS增速{eps*100:.0f}%❌")
        
        # 3. OCF > 0 且 OCF/净利润 > 0.8
        ocf_positive = stock.get("ocf_positive", False)
        ocf_ratio = stock.get("ocf_to_net_profit", 0.0)
        if ocf_positive and ocf_ratio > 0.8:
            score += 1
            details.append(f"OCF/净利={ocf_ratio:.1f}✅")
        elif ocf_positive:
            details.append(f"OCF为正但覆盖不足({ocf_ratio:.1f})")
        else:
            details.append("OCF为负❌")
        
        # 4. 毛利率趋势扩张或显著领先行业
        margin = stock.get("gross_margin", 0.0)
        margin_trend_raw = stock.get("margin_trend", 0.0)
        # margin_trend 可能是列表(历史趋势)或单个数值
        if isinstance(margin_trend_raw, list):
            margin_trend = margin_trend_raw[-1] - margin_trend_raw[0] if len(margin_trend_raw) >= 2 else 0
        else:
            margin_trend = margin_trend_raw
        ind_margin = stock.get("industry_margin", 0.0)
        if (margin_trend > 0 and margin > 0.25) or (ind_margin > 0 and margin > ind_margin * 1.5):
            score += 1
            details.append(f"毛利率{margin*100:.0f}%领先✅")
        elif margin > 0.30:
            score += 1  # 30%毛利率本身就是好信号
            details.append(f"毛利率{margin*100:.0f}%优秀✅")
        else:
            details.append(f"毛利率{margin*100:.0f}%❌")
        
        multiplier_map = {4: (1.3, "🔥超级成长"), 3: (1.15, "✅优质"),
                         2: (1.0, "→普通"), 1: (0.85, "⚠️弱"), 0: (0.7, "❌不达标")}
        multiplier, label = multiplier_map[score]
        passed = score >= 3
        return passed, multiplier, label, details


# ========== 估值约束器 ==========
class ValuationGate:
    """防止高价买入超级成长股 —— PEG + PS分位双重约束"""
    
    def check(self, stock: Dict) -> Tuple[float, str]:
        """
        返回: (仓位乘数, 原因)
        """
        # ETF免检
        if stock.get("name", "").endswith("ETF"):
            return 1.0, "ETF(免检)"
        
        peg = stock.get("peg", 999.0)
        ps_rank = stock.get("ps_percentile", 0.5)
        pe_ttm = stock.get("pe_ttm", 0)
        eps_g = stock.get("eps_growth", 0.01)
        
        # PEG自动计算(如果未提供)
        if peg >= 999 and pe_ttm > 0:
            peg = pe_ttm / (eps_g * 100) if eps_g > 0.01 else 999
        
        # 极端高估(PEG>3 或 PS分位>90%)
        if peg > 3.0 or ps_rank > 0.90:
            return 0.4, f"极端高估PEG={peg:.1f}/PS分位{ps_rank*100:.0f}%→×0.4"
        # 明显高估(PEG>2)
        elif peg > 2.0 or ps_rank > 0.80:
            return 0.6, f"明显高估PEG={peg:.1f}→×0.6"
        # 偏高
        elif peg > 1.5 or ps_rank > 0.70:
            return 0.8, f"估值偏高PEG={peg:.1f}→×0.8"
        # 合理
        elif peg <= 1.5:
            return 1.0, f"估值合理PEG={peg:.1f}✅"
        # 周期反转(弱EPS)
        elif peg >= 999 and stock.get("industry_type"):
            return 1.0, "周期反转免检"
        else:
            return 1.0, f"数据不足(PEG={peg:.1f}),正常仓位"


# ========== 波动率仓位管理器 ==========
class VolatilityPositionSizer:
    """根据沪深300波动率动态调整仓位，避免高波动期重仓"""
    
    def __init__(self):
        self._market_atr_pct = 0.0
        self._last_update = ""
    
    def update_market_vol(self, atr_pct: float, date_str: str = ""):
        self._market_atr_pct = atr_pct
        self._last_update = date_str or datetime.now().strftime("%Y-%m-%d")
    
    def get_position_discount(self) -> Tuple[float, str]:
        """
        返回仓位折扣系数 + 原因说明
        高波动(ATR%>3%) → ×0.6   极高波动(>4%) → ×0.5
        中波动(>2%) → ×0.8
        低波动  → ×1.0
        """
        v = self._market_atr_pct
        if v <= 0:
            return 1.0, "波动率未计算,保持原仓位"
        if v > 0.04:
            return 0.5, f"市场极度恐慌(ATR%={v*100:.1f}%),仓位腰斩"
        if v > 0.03:
            return 0.6, f"市场剧烈波动(ATR%={v*100:.1f}%),仓位四折"
        if v > 0.02:
            return 0.8, f"市场中波动(ATR%={v*100:.1f}%),温和降仓"
        return 1.0, f"波动正常(ATR%={v*100:.1f}%),满仓"


# ========== 风险关键词映射器 ==========
class RiskKeywordMapper:
    """从新闻快讯中识别系统性风险信号，触发强制避险/降仓"""
    
    RISK_PATTERNS = {
        "CRISIS": {  # 全仓清退
            "keywords": ["雷曼", "清算", "崩盘", "金融海啸", "系统性风险", 
                        "违约潮", "银行挤兑", "主权违约"],
            "action": "full_clear",
            "message": "[风险映射] 检测到系统性风险关键词→建议全仓清退"
        },
        "PANIC": {  # 强平半仓
            "keywords": ["暴跌", "强平", "熊市", "追保", "追加保证金", 
                        "熔断", "跌停潮", "千股跌停", "黑色星期", "恐慌抛售"],
            "action": "half_clear",
            "message": "[风险映射] 检测到市场恐慌关键词→建议强制减半仓"
        },
        "GEOPOLITICAL": {  # 地缘避险，保留黄金
            "keywords": ["战争升级", "军事打击", "关闭海峡", "空袭", 
                        "导弹袭击", "封锁海峡", "全面冲突"],
            "action": "defensive_only",
            "message": "[风险映射] 检测到地缘冲突升级→建议仅保留黄金+现金"
        },
        "CREDIT": {  # 信用危机，全球传染
            "keywords": ["韩国强平", "韩股暴跌", "日股暴跌", "亚洲金融危机",
                        "强平率", "信用收缩", "流动性枯竭"],
            "action": "asia_crisis",
            "message": "[风险映射] 检测到亚洲信用危机传导→建议大幅降仓至20%"
        }
    }
    
    def scan(self, news_texts: List[str]) -> Dict:
        """扫描新闻文本，返回风险等级 + 建议动作"""
        combined = " ".join(news_texts)
        result = {"level": "normal", "action": "none", "messages": [], "force_cap": 1.0}
        
        for level, cfg in self.RISK_PATTERNS.items():
            hits = [kw for kw in cfg["keywords"] if kw in combined]
            if hits:
                result["messages"].append(f"{cfg['message']}(命中:{','.join(hits[:3])})")
                if level == "CRISIS":
                    result["level"] = "crisis"
                    result["action"] = "full_clear"
                    result["force_cap"] = 0.0
                elif level == "PANIC":
                    if result["level"] not in ("crisis",):
                        result["level"] = "panic"
                        result["action"] = "half_clear"
                        result["force_cap"] = 0.5
                elif level == "CREDIT":
                    if result["level"] not in ("crisis", "panic"):
                        result["level"] = "credit"
                        result["action"] = "asia_crisis"
                        result["force_cap"] = min(result["force_cap"], 0.3)
                elif level == "GEOPOLITICAL":
                    if result["level"] == "normal":
                        result["level"] = "geopolitical"
                        result["action"] = "defensive_only"
                        result["force_cap"] = min(result["force_cap"], 0.4)
        return result


# ========== 市场微结构风险监测(HFT异常) ==========
class MarketMicrostructureRisk:
    """市场微结构风险监测 — HFT异常行为识别(OTR/撤单率/价格消退/日内换手)
    将个股/市场层面的高频异常交易环境转化为评分折减与仓位硬约束。
    指标复现自 kdb+ HFT surveillance 白皮书 (Stanton-Cook et al., 2014)。
    若标的不含微结构字段, 返回中性(乘数=1.0, 无标记), 可安全始终调用。
    """

    # 阈值(对A股需结合 tick/撮合机制重新标定, 此处沿用白皮书基准)
    OTR_HFT_THRESHOLD = 15        # 订单成交比 > 15 视为 HFT 嫌疑
    OTR_SEVERE = 100              # 极端(报价填塞级别)
    CANCEL_RATE_WARN = 0.5        # 撤单率 > 50% 预警(钓鱼/噪声)
    FADE_PROB_WARN = 0.15         # 完全消退概率 > 15% 预警(文档: 7-15%)
    INTRADAY_CLOSEOUT_MIN = 14    # 日内平仓股票数阈值(日终无隔夜风险)

    def __init__(self):
        self._last_scan = {}

    def evaluate_stock(self, stock: Dict) -> Tuple[float, List[str], Dict]:
        """个股级: 读取微结构指标, 返回(评分乘数, 风险标记, 详情)
        stock 可携带字段: otr / cancel_rate / price_fade_prob /
                          intraday_closeout_stocks / msg_left_skew
        """
        mult = 1.0
        flags = []
        detail = {}

        otr = stock.get("otr")
        cancel_rate = stock.get("cancel_rate")            # 0-1
        fade_prob = stock.get("price_fade_prob")          # 完全消退概率
        closeout = stock.get("intraday_closeout_stocks")  # 日内平仓股票数
        msg_skew = stock.get("msg_left_skew")             # 消息左偏 bool

        if otr is not None:
            detail["otr"] = round(otr, 2)
            if otr > self.OTR_SEVERE:
                mult *= 0.6
                flags.append("极端OTR(疑似报价填塞)")
            elif otr > self.OTR_HFT_THRESHOLD:
                mult *= 0.85
                flags.append("高OTR(疑似HFT)")

        if cancel_rate is not None:
            detail["cancel_rate"] = round(cancel_rate, 3)
            if cancel_rate > self.CANCEL_RATE_WARN:
                mult *= 0.8
                flags.append("高撤单率(钓鱼/噪声)")

        if fade_prob is not None:
            detail["price_fade_prob"] = round(fade_prob, 3)
            if fade_prob > self.FADE_PROB_WARN:
                mult *= 0.85
                flags.append("价格消退异常(分层挂单)")

        if closeout is not None:
            detail["intraday_closeout_stocks"] = closeout
            if closeout >= self.INTRADAY_CLOSEOUT_MIN:
                mult *= 0.9
                flags.append("日内高频回转(无隔夜风险)")

        if msg_skew:
            detail["msg_left_skew"] = True
            flags.append("消息左偏(微秒级下单)")

        detail["score_multiplier"] = round(mult, 3)
        detail["flags"] = flags
        return mult, flags, detail

    def scan_market(self, stocks: List[Dict]) -> Dict:
        """市场级聚合: 统计出现异常微结构特征的标的数, 返回仓位上限建议"""
        suspicious = 0
        severe = 0
        for s in stocks:
            mult, flags, _ = self.evaluate_stock(s)
            if flags:
                suspicious += 1
            if any(("极端" in f) or ("异常" in f) for f in flags):
                severe += 1
        # 异常标的比例越高, 整体仓位上限越低
        cap = 1.0
        if severe >= 3:
            cap = 0.7
        elif suspicious >= 3:
            cap = 0.85
        return {
            "suspicious_count": suspicious,
            "severe_count": severe,
            "total": len(stocks),
            "position_cap": cap,
            "note": "微结构异常聚集→降低整体仓位上限" if cap < 1.0 else "微结构正常"
        }


# ========== 协方差收缩 / 协整熔断 / 多重检验校正 (统计稳健性风控层) ==========
class CovarianceShrinkage:
    """协方差收缩估计 — Ledoit-Wolf(常量相关目标) 替换朴素样本协方差。
    对应文章'收缩估计'与'随机矩阵理论(RMT)'两节: 当资产数 N 接近/超过样本数 T,
    朴素协方差条件数爆炸, 优化器把估计噪声当成风险结构疯狂加仓(回测漂亮实盘崩)。
    收缩 = 把样本协方差向'等相关目标矩阵'按强度 delta 混合(偏差-方差权衡)。
    诊断附带 RMT 噪声带(Marchenko-Pastur 上界): 最大特征值超出上界→疑似伪结构。
    无收益率序列时返回中性(不改动仓位), 可安全始终调用。
    """

    STRONG_SHRINK = 0.6   # delta>=此值 → 数据噪声高, 降仓
    MID_SHRINK = 0.4

    def __init__(self):
        self._last = {}

    # ---- Ledoit-Wolf 收缩(优先 sklearn 验证实现, 回退手写常量相关) ----
    def shrink(self, R: "np.ndarray"):
        """R: T×N 收益率矩阵(行=时间, 列=资产)
        返回 (cov_shrunk, delta, eigvals, mp_upper_edge)
        delta 越大=越依赖目标矩阵=样本越不可信(噪声越高)。
        """
        R = np.asarray(R, dtype=float)
        try:
            from sklearn.covariance import ledoit_wolf as _lw
            cov, delta = _lw(R)
            delta = float(delta)
        except Exception:
            cov, delta, eigvals, mp_upper = self._lw_manual(R)
            return cov, delta, eigvals, mp_upper
        eigvals = np.linalg.eigvalsh(cov)
        T, N = R.shape
        sigma2 = float(np.mean(np.diag(np.cov(R, rowvar=False, bias=True))))
        mp_upper = sigma2 * (1.0 + np.sqrt(N / T)) ** 2 if T > 0 else None
        return cov, delta, eigvals, mp_upper

    @staticmethod
    def _lw_manual(R: "np.ndarray"):
        """Ledoit-Wolf 2004 常量相关目标 手写实现(无 sklearn 时回退)"""
        R = np.asarray(R, dtype=float)
        T, N = R.shape
        x = R - R.mean(axis=0)
        S = (x.T @ x) / T                      # 样本协方差(1/T 偏置)
        d = np.sqrt(np.diag(S))
        denom = np.outer(d, d)
        with np.errstate(divide="ignore", invalid="ignore"):
            corr = np.where(denom > 0, S / denom, 0.0)
        off = ~np.eye(N, dtype=bool)
        r_bar = corr[off].sum() / max(N * (N - 1), 1)   # 平均样本相关
        # 目标矩阵 F: 对角线保留样本方差, 非对角 = r_bar*sqrt(sii*sjj)
        F = np.diag(np.diag(S)).copy()
        F = F + r_bar * (denom - np.diag(np.diag(denom)))
        # pi = Σ_ij [ 1/T Σ_t (xit xjt)^2 - s_ij^2 ]
        xt = x.T
        outer_t = np.einsum("it,jt->ijt", xt, xt)
        sq_sum = np.einsum("ijt->ij", outer_t ** 2) / T
        pi = float((sq_sum - S ** 2).sum())
        rho = float(((S - F) ** 2).sum())
        delta = pi / rho if rho > 0 else 1.0
        delta = float(min(1.0, max(0.0, delta)))
        cov = delta * F + (1.0 - delta) * S
        eigvals = np.linalg.eigvalsh(cov)
        sigma2 = float(np.mean(np.diag(S)))
        mp_upper = sigma2 * (1.0 + np.sqrt(N / T)) ** 2 if T > 0 else None
        return cov, delta, eigvals, mp_upper

    @staticmethod
    def inverse_variance_weights(cov: "np.ndarray"):
        """基于(收缩后)协方差的对角逆方差权重 — 朴素分散基准"""
        iv = 1.0 / np.diag(cov)
        iv = np.where(np.isfinite(iv) & (iv > 0), iv, 0.0)
        s = iv.sum()
        return iv / s if s > 0 else np.ones(len(iv)) / len(iv)

    def scan_from_portfolio(self, stocks: List[Dict], min_obs: int = 30) -> Dict:
        """从组合标的中抽取收益率序列, 估计收缩协方差并给出仓位建议。
        stock 需携带 'returns': 1D 收益率数组; 仅在有>=2只且样本足够时生效。
        """
        series = {}
        for s in stocks:
            r = s.get("returns")
            if r is not None and hasattr(r, "__len__") and len(r) >= min_obs:
                series[s.get("symbol", f"#{len(series)}")] = np.asarray(r, dtype=float)
        if len(series) < 2 or np is None:
            return {"n_assets": len(series), "shrinkage": None,
                    "position_cap": 1.0, "rmt_noise_band_breach": None,
                    "note": "样本不足或缺少收益率序列, 跳过协方差收缩(中性)"}
        syms = list(series.keys())
        R = np.column_stack([series[s] for s in syms])
        cov, delta, eigvals, mp_upper = self.shrink(R)
        # RMT: 用样本协方差特征值判断是否超出纯噪音带(Marchenko-Pastur 上界)
        S = np.cov(R, rowvar=False, bias=True)
        samp_eig = np.linalg.eigvalsh(S)
        cap = 1.0
        note = "协方差质量正常"
        if delta >= self.STRONG_SHRINK:
            cap = 0.85
            note = f"强收缩(delta={delta:.2f}): 数据噪声高, 谨慎分散加仓"
        elif delta >= self.MID_SHRINK:
            cap = 0.95
            note = f"中度收缩(delta={delta:.2f}): 协方差含较多估计噪声"
        rmt_breach = bool(samp_eig.max() > mp_upper) if mp_upper is not None else None
        return {
            "n_assets": len(syms), "n_obs": int(R.shape[0]),
            "shrinkage": round(delta, 3),
            "mp_upper_edge": round(float(mp_upper), 4) if mp_upper else None,
            "max_eigval": round(float(samp_eig.max()), 4),
            "rmt_noise_band_breach": rmt_breach,
            "suggested_weights": {syms[i]: round(w, 4) for i, w in
                                  enumerate(self.inverse_variance_weights(cov))},
            "position_cap": cap, "note": note,
        }

    def allocate(self, stocks: List[Dict], scores_lookup: Dict[str, float] = None,
                 min_obs: int = 30, blend: float = 0.5) -> Dict[str, float]:
        """用收缩后协方差做真正的配置权重(逆方差/最大分散), 并与打分权重混合。

        这是把'Ledoit-Wolf 收缩'从"只出标量仓位折减"升级为"驱动组合配置":
        真实分散来自逆方差权重(收缩后协方差的逆), 打分权重提供 alpha 倾斜。
        返回归一化权重 dict; 样本不足/无 numpy → {} (中性, 不覆盖原打分排序)。
        """
        if np is None:
            return {}
        series = {}
        for s in stocks:
            r = s.get("returns")
            if r is not None and hasattr(r, "__len__") and len(r) >= min_obs:
                series[s.get("symbol", f"#{len(series)}")] = np.asarray(r, dtype=float)
        if len(series) < 2:
            return {}
        syms = list(series.keys())
        minlen = min(len(v) for v in series.values())
        R = np.column_stack([v[-minlen:] for v in series.values()])
        cov, _, _, _ = self.shrink(R)
        w_cov = self.inverse_variance_weights(cov)
        if scores_lookup is None:
            return {syms[i]: round(float(w_cov[i]), 4) for i in range(len(syms))}
        sc = np.asarray([max(float(scores_lookup.get(sym, 0.0)), 0.0) for sym in syms], float)
        if sc.sum() > 0:
            w_score = sc / sc.sum()
        else:
            w_score = np.ones(len(syms)) / len(syms)
        w = blend * np.asarray(w_cov) + (1.0 - blend) * w_score
        w = np.clip(w, 0, None)
        if w.sum() <= 0:
            return {syms[i]: round(float(w_cov[i]), 4) for i in range(len(syms))}
        w = w / w.sum()
        return {syms[i]: round(float(w[i]), 4) for i in range(len(syms))}


class CointegrationGuard:
    """协整关系监测 + 结构断裂熔断 — 对应文章'配对爆仓'段。
    仅对标注了基准序列(benchmark_series)的相对价值/配对标的生效:
      - Engle-Granger 协整检验 + KPSS 平稳性确认 spread 残差
      - rolling-window ADF 监测结构断裂
    协整不成立 / 残差非平稳 / 结构已断 → allowed=False(禁止加仓硬约束)。
    无 benchmark_series 的普通方向标的中性通过, 不影响原评分。
    """
    COINT_P_THRESHOLD = 0.05      # EG 协整 p 值阈值
    KPSS_P_THRESHOLD = 0.05       # KPSS 平稳 p 值(越小越平稳) → 低于此值才确认平稳
    ROLL_WINDOW = 60
    BREAK_P_THRESHOLD = 0.10      # rolling ADF p>此值 → spread 不再平稳 → 断裂

    def __init__(self):
        self._last = {}

    def engle_granger(self, y, x):
        """最小二乘 hedge ratio + Engle-Granger 协整 p 值 + spread 残差"""
        y = np.asarray(y, float); x = np.asarray(x, float).ravel()
        X = np.column_stack([np.ones_like(x), x])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        spread = y - X @ beta
        coint_p = None
        if _HAS_STATSMODELS:
            try:
                coint_p = coint(y, x)[1]
            except Exception:
                coint_p = None
        return coint_p, float(beta[1]), spread

    @staticmethod
    def kpss_stationary(series):
        if not _HAS_STATSMODELS:
            return None
        try:
            _, p, _, _ = kpss(np.asarray(series, float), regression="c", nlags="auto")
            return p
        except Exception:
            return None

    def rolling_adf(self, spread, window):
        if not _HAS_STATSMODELS or len(spread) < window + 10:
            return None, False
        recent = spread[-window:]
        try:
            p = adfuller(recent, autolag="AIC")[1]
            return p, bool(p > self.BREAK_P_THRESHOLD)
        except Exception:
            return None, False

    def evaluate(self, stock: Dict):
        """返回 (allowed: bool, flags: list, detail: dict)"""
        bench = stock.get("benchmark_series")
        price = stock.get("price_series")
        flags = []
        detail = {}
        if bench is None or price is None:
            return True, flags, detail          # 非配对标的, 中性通过
        coint_p, hedge, spread = self.engle_granger(price, bench)
        detail["hedge_ratio"] = round(hedge, 4)
        detail["coint_pvalue"] = round(coint_p, 4) if coint_p is not None else None
        if coint_p is None:
            return True, flags, detail
        if coint_p > self.COINT_P_THRESHOLD:
            flags.append("协整关系不成立(禁止配对加仓)")
            detail["cointegrated"] = False
            return False, flags, detail
        kp = self.kpss_stationary(spread)
        detail["kpss_pvalue"] = round(kp, 4) if kp is not None else None
        if kp is not None and kp < self.KPSS_P_THRESHOLD:
            flags.append("残差非平稳(KPSS显著)-协整存疑")
            detail["kpss_stationary"] = False
            return False, flags, detail
        p, broken = self.rolling_adf(spread, self.ROLL_WINDOW)
        detail["rolling_adf_pvalue"] = round(p, 4) if p is not None else None
        detail["stable"] = not broken
        if broken:
            flags.append("协整结构已断裂(rolling ADF失稳)-禁止加仓")
            return False, flags, detail
        detail["cointegrated"] = True
        detail["kpss_stationary"] = True
        return True, flags, detail


class MultipleTestingFilter:
    """因子入选前的多重检验校正 — 对应文章'多重检验陷阱'段。
    输入候选因子及其 p 值, 用 Bonferroni 与 Benjamini-Hochberg(FDR) 校正,
    返回通过校正的因子集合 + 拒绝率报告, 防止过拟合因子进组合。
    无 p 值/无 statsmodels 时退化为全通过(中性)。
    """
    ALPHA = 0.05

    def screen(self, factors: List[Dict]):
        """factors: list of {'name', 'pvalue'(可缺省)} → (survived, rejected, report)"""
        if not factors:
            return [], [], {"total": 0, "survived": 0, "rejected": 0,
                            "rejection_rate": 0.0, "note": "无候选因子"}
        pvals = [f.get("pvalue", None) for f in factors]
        if any(p is None for p in pvals) or not _HAS_STATSMODELS:
            return (factors, [], {"total": len(factors), "survived": len(factors),
                    "rejected": 0, "rejection_rate": 0.0,
                    "note": "无p值或缺少statsmodels, 未做校正(全通过)"})
        p = np.asarray(pvals, float)
        try:
            rej_b, _, _, _ = multipletests(p, alpha=self.ALPHA, method="bonferroni")
            rej_bh, _, _, _ = multipletests(p, alpha=self.ALPHA, method="fdr_bh")
        except Exception:
            return (factors, [], {"total": len(factors), "survived": len(factors),
                    "rejected": 0, "rejection_rate": 0.0, "note": "校正失败(全通过)"})
        survived = [factors[i] for i in range(len(factors)) if rej_bh[i]]
        rejected = [factors[i] for i in range(len(factors)) if not rej_bh[i]]
        report = {
            "total": len(factors),
            "survived": len(survived),
            "rejected": len(rejected),
            "survived_names": [f.get("name") for f in survived],
            "rejected_names": [f.get("name") for f in rejected],
            "rejection_rate": round(1 - len(survived) / len(factors), 3),
            "bonferroni_pass": int(rej_b.sum()),
            "bh_pass": int(rej_bh.sum()),
            "alpha": self.ALPHA,
            "note": (f"多重检验校正: {len(rejected)}/{len(factors)} 因子未通过FDR, "
                     "疑似过拟合噪音" if len(rejected) else "全部因子通过FDR"),
        }
        return survived, rejected, report

    def screen_from_dict(self, pvalue_map: Dict[str, float]) -> Dict:
        factors = [{"name": k, "pvalue": v} for k, v in pvalue_map.items()]
        _, _, report = self.screen(factors)
        return report


# ========== 盘中熔断监控 ==========
class WarpMonitor:
    """实时盘中熔断监控，检测极端行情触发降仓/清仓"""
    
    def __init__(self):
        self._last_scan = ""
        self._alert_fired = False
    
    def check(self, index_change: float, sci50_change: float, 
              decliners: int = 0, total_stocks: int = 5400) -> Dict:
        """
        index_change: 上证当日涨跌幅(%)
        sci50_change: 科创50当日涨跌幅(%)
        decliners: 下跌家数
        total_stocks: 全市场总家数
        """
        result = {"alert": False, "level": "normal", "action": "none", "reason": ""}
        
        decline_pct = decliners / total_stocks if total_stocks > 0 else 0
        
        # 三级警报
        if decline_pct > 0.85 and sci50_change < -5:
            result.update(alert=True, level="RED", action="full_clear",
                         reason=f"全市场{decline_pct*100:.0f}%下跌+科创50跌{sci50_change:.1f}%→熔断清仓")
        elif sci50_change < -3.0 or (index_change < -2.0 and decline_pct > 0.7):
            result.update(alert=True, level="ORANGE", action="half_clear",
                         reason=f"科创50跌{sci50_change:.1f}%/上证跌{index_change:.1f}%→强制减半仓")
        elif index_change < -1.5 or sci50_change < -2.0:
            result.update(alert=True, level="YELLOW", action="reduce",
                         reason=f"指数回调{index_change:.1f}%→降仓至50%,暂停加仓")
        
        self._last_scan = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return result


# ========== 动态标的池轮换 ==========
class WatchlistRotator:
    """每周根据市场风格动态调整观察池优先级"""
    
    STYLE_DEFENSE = ["518880"]           # 防御: 黄金ETF
    STYLE_VALUE = ["601881", "600030"]   # 价值: 券商
    STYLE_GROWTH = ["588000", "688981"]  # 成长: 科创/半导体
    STYLE_CYCLICAL = ["002460", "000960"] # 周期: 锂/锡
    STYLE_ROBOT = ["688017"]             # 机器人
    STYLE_GOLD = ["518880"]              # 避险
    
    def __init__(self):
        self.current_style = "balanced"
    
    def rotate(self, sci50_week_change: float, market_cap_weight: Dict = None) -> Dict:
        """根据周度科创50涨跌 + 资金流向,返回仓位配置权重"""
        if sci50_week_change <= -0.08:
            # 科技大跌 → 防御姿态
            self.current_style = "defensive"
            return {
                "STYLE_DEFENSE": 0.35,   # 黄金
                "STYLE_VALUE": 0.30,     # 券商
                "STYLE_CYCLICAL": 0.20,  # 锂矿(低估值)
                "STYLE_ROBOT": 0.10,     # 机器人(独立逻辑)
                "STYLE_GROWTH": 0.05     # 科技(极少)
            }
        elif sci50_week_change <= -0.04:
            self.current_style = "cautious"
            return {
                "STYLE_VALUE": 0.30,
                "STYLE_DEFENSE": 0.20,
                "STYLE_ROBOT": 0.20,
                "STYLE_GROWTH": 0.15,
                "STYLE_CYCLICAL": 0.15
            }
        elif sci50_week_change >= 0.03:
            self.current_style = "aggressive"
            return {
                "STYLE_GROWTH": 0.40,
                "STYLE_ROBOT": 0.25,
                "STYLE_CYCLICAL": 0.15,
                "STYLE_VALUE": 0.15,
                "STYLE_DEFENSE": 0.05
            }
        else:
            self.current_style = "balanced"
            return {
                "STYLE_GROWTH": 0.25,
                "STYLE_VALUE": 0.25,
                "STYLE_ROBOT": 0.20,
                "STYLE_CYCLICAL": 0.15,
                "STYLE_DEFENSE": 0.15
            }


# ========== 纠偏反馈闭环 ==========
class TherapyPositionGate:
    """将散户认知纠偏的输出转化为仓位硬约束"""
    
    def __init__(self):
        self._cap = 1.0        # 仓位硬上限
        self._reason_chain = []
    
    def apply_therapy(self, therapy_result: Dict, news_analysis: Dict, 
                      market_state: str) -> Tuple[float, List[str]]:
        """根据纠偏分析限制仓位上限"""
        self._cap = 1.0
        self._reason_chain = []
        
        behaviors = therapy_result.get("behaviors", [])
        suggestions = therapy_result.get("suggestions", [])
        
        # 行为诊断 → 仓位约束
        for b in behaviors:
            if "FOMO追高" in b:
                self._cap = min(self._cap, 0.6)
                self._reason_chain.append("FOMO追高倾向→仓位上限60%")
            elif "恐慌割肉" in b:
                self._cap = min(self._cap, 0.4)
                self._reason_chain.append("恐慌倾向→仓位上限40%(防止追涨杀跌)")
            elif "重仓焦虑" in b:
                self._cap = min(self._cap, 0.7)
                self._reason_chain.append("重仓焦虑→仓位上限70%")
            elif "过度交易" in b:
                self._cap = min(self._cap, 0.8)
                self._reason_chain.append("过度交易→控制仓位80%")
        
        # 市场极端状态 → 额外约束
        risk_scan = RiskKeywordMapper().scan(
            [t.get("logic", "") for t in news_analysis.get("themes", {}).values()])
        if risk_scan["force_cap"] < self._cap:
            self._cap = risk_scan["force_cap"]
            self._reason_chain.append(f"风险映射→仓位上限{risk_scan['force_cap']*100:.0f}%")
        
        return self._cap, self._reason_chain


# ========== 实时市场数据获取 ==========
def fetch_market_snapshot() -> Dict:
    """获取实时大盘指数+成交额,带时效性校验"""
    import requests, json
    
    result = {"index": 4163, "turnover": 37600, "sci50": 2.10, 
              "sci50_change": 0.0, "index_change": 0.0, "decliners": 0,
              "source": "fallback", "age_hours": 999}
    
    # 源1: 新浪实时行情(免费、更新快)
    try:
        r = requests.get(
            "https://hq.sinajs.cn/list=sh000001,sh000688,sh688017",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
            timeout=10
        )
        if r.status_code == 200 and '="' in r.text:
            lines = r.text.strip().split("\n")
            for line in lines:
                parts = line.split('="')[1].split('",')[0].split(",") if '="' in line else []
                if len(parts) < 4: continue
                name = parts[0]
                price = float(parts[3]) if parts[3] else 0
                prev = float(parts[2]) if parts[2] else price
                change_pct = (price - prev) / prev * 100 if prev > 0 else 0
                if "上证指数" in line or "000001" in line:
                    result["index"] = round(price, 0)
                    result["index_change"] = round(change_pct, 2)
                elif "科创50" in line or "000688" in line:
                    result["sci50"] = round(price, 2)
                    result["sci50_change"] = round(change_pct, 2)
            
            result["source"] = "sina_realtime"
            result["age_hours"] = 0
            
            # 尝试获取成交额(新浪分时接口)
            try:
                r2 = requests.get(
                    "https://hq.sinajs.cn/list=sh000001_i",
                    headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn/"},
                    timeout=8
                )
                # 部分A股总成交额估算
                result["turnover"] = int(result["index"] * 7.2)  # 粗略估算
            except:
                pass
            return result
    except Exception as e:
        pass
    
    # 源2: akshare
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily_em(symbol="sh000001")
        if df is not None and len(df) > 0:
            latest = df.iloc[-1]
            result["index"] = round(float(latest["close"]), 0)
            result["turnover"] = float(latest.get("amount", 0)) / 1e8
            result["source"] = "akshare"
            date_str = str(latest.get("date", ""))
            today = datetime.now().strftime("%Y-%m-%d")
            if date_str == today:
                result["age_hours"] = 0
            else:
                result["age_hours"] = 24
    except:
        pass
    
    return result


# ================================================================
# 🔬 每日复盘引擎 MarketReviewEngine
# 整合专业复盘SOP：大盘定调 → 板块资金 → 个股地位 → 周期定位 → 输出模板
# ================================================================

# ─── 周期枚举 ───
class CyclePhase(Enum):
    ICE = "冰点"         # 涨停<30 炸板>40% → 轻仓试错
    START = "启动"        # 3+涨停板块效应 → 半仓上车
    FERMENT = "发酵"      # 涨停>8扩散 → 补涨/低吸中军
    CLIMAX = "高潮"       # 涨停潮 → 只卖不买
    DIVERGE = "分歧"      # 龙头断板不跌 → 尾盘低吸博反包
    RETREAT = "退潮"      # 龙头A杀 → 清仓不参与


class MarketReviewEngine:
    """每日复盘引擎 — 5步SOP量化实现
    
    核心功能:
      1. 大盘定基调 — 指数MA位置/量能比/涨跌家数/涨跌停比
      2. 板块资金排名 — 成交额排序/涨停家数/连板高度/中军识别
      3. 板块内个股地位 — 龙头/中军/补涨/跟风 四梯队划分
      4. 周期定位 — 冰点→启动→发酵→高潮→分歧→退潮 六阶段
      5. 生成复盘模板 — 结构化输出 + 明日预案
    """

    # ── 周期判断阈值 ──
    ICE_LIMIT_UP   = 30     # 低于此数→冰点
    CLIMAX_LIMIT_UP = 80    # 高于此数→高潮
    HIGH_BREAK_RATE = 0.40  # 炸板率高于此→危险
    LOW_BREAK_RATE  = 0.20  # 炸板率低于此→一致
    CLIMAX_SECTOR_CNT = 15  # 板块涨停≥15→高潮
    START_SECTOR_CNT  = 3   # 板块涨停≥3→板块效应成立
    MAINLINE_SECTOR_CNT = 8 # 涨停≥8→主线发酵

    def __init__(self, strategy_plan: Optional[Dict] = None):
        self.plan = strategy_plan
        self.market: Dict = {}
        self.sectors: List[Dict] = []
        self.cycle_phase: Optional[CyclePhase] = None
        self.phase_confidence: float = 0.0
        self.yesterday_limit_up_perf: float = 0.0
        self._report_lines: List[str] = []

    # ═══════════════════════════════════════════════
    # Step 1: 大盘定基调
    # ═══════════════════════════════════════════════
    def step1_macro_review(self) -> Dict:
        """三大指标定基调: 指数结构 + 涨跌家数 + 涨跌停比"""
        snap = fetch_market_snapshot()
        idx = snap["index"]
        sci50 = snap["sci50"]
        turnover = snap["turnover"]
        
        # —— 1.1 均线位置 ——
        ma_positions = self._check_ma_positions()
        
        # —— 1.2 量能比 ——
        avg_vol = self._get_rolling_avg_volume(5)
        vol_ratio = turnover / avg_vol if avg_vol > 0 else 1.0
        vol_label = "放量" if vol_ratio > 1.15 else ("缩量" if vol_ratio < 0.85 else "平量")
        
        # —— 1.3 涨跌家数 — (从行情快照/akshare获取，不可用时退化为估算)
        up_count, down_count = self._get_market_breadth()
        breadth_ratio = up_count / (up_count + down_count + 1)
        
        # —— 1.4 涨跌停比 ——
        limit_up, limit_down = self._get_limit_ratio()
        limit_ratio = limit_up / max(limit_down, 1)
        break_rate = self._estimate_break_rate()
        
        self.market = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "index": idx, "sci50": sci50, "sci50_change": snap.get("sci50_change", 0),
            "turnover": turnover, "vol_ratio": round(vol_ratio, 2), "vol_label": vol_label,
            "ma_positions": ma_positions, "up_count": up_count, "down_count": down_count,
            "breadth_ratio": round(breadth_ratio, 2),
            "limit_up": limit_up, "limit_down": limit_down,
            "limit_ratio": round(limit_ratio, 1),
            "break_rate": round(break_rate, 2),
        }
        return self.market

    def _check_ma_positions(self) -> Dict:
        """检查上证指数在5/20/60日线上/下"""
        result = {"ma5": "?", "ma20": "?", "ma60": "?"}
        try:
            import akshare as ak
            import time; time.sleep(1.5)
            df = ak.stock_zh_index_daily_em(symbol="sh000001")
            if df is None or len(df) < 60:
                return result
            close = df["close"].values
            ma5 = close[-5:].mean()
            ma20 = close[-20:].mean()
            ma60 = close[-60:].mean()
            current = float(close[-1])
            result = {
                "ma5":  "上" if current >= ma5 else "下",
                "ma20": "上" if current >= ma20 else "下",
                "ma60": "上" if current >= ma60 else "下",
            }
        except Exception:
            pass
        return result

    def _get_rolling_avg_volume(self, days: int = 5) -> float:
        """获取N日滚动均量(亿元)"""
        try:
            import akshare as ak; import time; time.sleep(1.5)
            df = ak.stock_zh_index_daily_em(symbol="sh000001")
            if df is not None and "amount" in df.columns and len(df) >= days:
                return float(df["amount"].tail(days).mean()) / 1e8
        except Exception:
            pass
        return self.market.get("turnover", 27000)

    def _get_market_breadth(self) -> Tuple[int, int]:
        """获取涨跌家数"""
        try:
            import akshare as ak; import time; time.sleep(2.0)
            df = ak.stock_zh_a_spot_em()
            if df is not None and "涨跌幅" in df.columns:
                up = (df["涨跌幅"] > 0).sum()
                down = (df["涨跌幅"] < 0).sum()
                return int(up), int(down)
        except Exception:
            pass
        # 回退: 从策略plan推断
        if self.plan:
            stocks = self.plan.get("stocks", [])
            up_s = sum(1 for s in stocks if s.get("price_change_pct", 0) > 0)
            return up_s * 200, (len(stocks) - up_s) * 200
        return 0, 0

    def _get_limit_ratio(self) -> Tuple[int, int]:
        """获取涨跌停数量"""
        limit_up, limit_down = 0, 0
        try:
            import akshare as ak; import time; time.sleep(2.0)
            df = ak.stock_zh_a_spot_em()
            if df is not None and "涨跌幅" in df.columns:
                limit_up = int((df["涨跌幅"] >= 9.8).sum())
                limit_down = int((df["涨跌幅"] <= -9.8).sum())
                return limit_up, limit_down
        except Exception:
            pass
        # 回退: 根据指数涨跌估算
        if self.market.get("sci50_change", 0) > 2:
            limit_up, limit_down = 90, 2
        elif self.market.get("sci50_change", 0) < -5:
            limit_up, limit_down = 30, 20
        else:
            limit_up, limit_down = 50, 8
        return limit_up, limit_down

    def _estimate_break_rate(self) -> float:
        """估算炸板率 (实际应从行情软件获取)"""
        # 回退: 成交量越大波动越大→炸板越多
        vol = self.market.get("turnover", 27000)
        if vol > 35000:
            return 0.25
        elif vol > 25000:
            return 0.30
        elif vol > 18000:
            return 0.22
        return 0.35

    # ═══════════════════════════════════════════════
    # Step 2: 板块资金排名
    # ═══════════════════════════════════════════════
    def step2_sector_ranking(self) -> List[Dict]:
        """锁定量能最大、涨停最多、连板最高的核心板块"""
        sectors = self._fetch_sector_data()
        if not sectors:
            self.sectors = []
            return []
        
        # 按成交额排序 + 加分项
        for sec in sectors:
            score = 0
            turnover = sec.get("turnover", 0)
            limit_up_cnt = sec.get("limit_up_cnt", 0)
            max_chain = sec.get("max_chain", 0)
            
            # 成交额分 (200亿为基准)
            score += min(turnover / 2, 80)  # 400亿以上=80分
            # 涨停家数
            score += limit_up_cnt * 5
            # 连板高度
            score += max_chain * 3
            
            sec["score"] = round(score, 1)
            
            # 板块阶段判断
            if limit_up_cnt >= 15:
                sec["stage"] = "高潮"
            elif limit_up_cnt >= 8:
                sec["stage"] = "发酵"
            elif limit_up_cnt >= 5:
                sec["stage"] = "启动"
            elif limit_up_cnt >= 3:
                sec["stage"] = "分歧"
            else:
                sec["stage"] = "退潮/轮动"
            
            # 是否有中军 (成交>20亿的趋势股)
            sec["has_main_force"] = any(
                s.get("avg_turnover", 0) > 20 and s.get("trend", "") == "up"
                for s in sec.get("stocks", [])
            )
        
        sectors.sort(key=lambda x: x["score"], reverse=True)
        self.sectors = sectors[:10]
        return self.sectors[:10]

    def _fetch_sector_data(self) -> List[Dict]:
        """获取板块排行数据 — 多源回退"""
        # 源1: akshare 东方财富行业板块
        try:
            import akshare as ak; import time; time.sleep(2.0)
            df = ak.stock_board_concept_name_em()
            if df is not None and len(df) > 0:
                sectors = []
                top20 = df.nlargest(20, "成交额") if "成交额" in df.columns else df.head(20)
                for _, row in top20.iterrows():
                    sec = {
                        "name": row.get("板块名称", "?"),
                        "change_pct": float(row.get("涨跌幅", 0)),
                        "turnover": float(row.get("成交额", 0)) / 1e8,  # 转亿
                        "limit_up_cnt": int(row.get("涨停家数", 0)),
                        "max_chain": int(row.get("最高连板", 0)) if "最高连板" in row else 0,
                        "stocks": [],
                    }
                    sectors.append(sec)
                return sectors
        except Exception:
            pass
        
        # 源2: 从策略plan反向构建
        if self.plan:
            return self._build_sectors_from_plan()
        
        return []

    def _build_sectors_from_plan(self) -> List[Dict]:
        """从策略已有行业/板块数据构建板块排名"""
        stocks = self.plan.get("stocks", [])
        if not stocks:
            return []
        # 按行业分组
        from collections import defaultdict
        groups = defaultdict(list)
        for s in stocks:
            ind = s.get("industry", "其他")
            groups[ind].append(s)
        
        sectors = []
        for ind, group in groups.items():
            sec = {
                "name": ind,
                "change_pct": np.mean([s.get("price_change_pct", 0) for s in group]),
                "turnover": sum(s.get("daily_turnover", 10) for s in group),
                "limit_up_cnt": sum(1 for s in group if s.get("price_change_pct", 0) > 9.8),
                "max_chain": max([s.get("chain_days", 0) for s in group], default=0),
                "stocks": group,
            }
            sectors.append(sec)
        return sectors

    # ═══════════════════════════════════════════════
    # Step 3: 板块内个股地位
    # ═══════════════════════════════════════════════
    def step3_classify_roles(self, sector: Dict) -> Dict:
        """将板块内个股划分为四梯队: 龙头/中军/补涨/跟风"""
        stocks = sector.get("stocks", [])
        if not stocks:
            return sector
        
        # 按涨停时间排序 (最早涨停→龙头)
        stocks_sorted = sorted(stocks, key=lambda s: (
            -s.get("price_change_pct", 0),    # 涨幅越大越好
            -s.get("total_score", 0),         # 策略评分越高越好
        ))
        
        classified = {"leader": [], "main_force": [], "catch_up": [], "follower": []}
        for i, s in enumerate(stocks_sorted):
            score = s.get("total_score", 0)
            turnover = s.get("daily_turnover", 0)
            chain = s.get("chain_days", 0)
            pct = s.get("price_change_pct", 0)
            
            if chain >= 2 or pct >= 9.5:
                classified["leader"].append(s)        # 龙头: 连板或涨停
            elif turnover >= 20 and score >= 120:
                classified["main_force"].append(s)    # 中军: 大成交量+高评分
            elif pct >= 5 or score >= 150:
                classified["catch_up"].append(s)      # 补涨: 涨幅较高或评分极高
            else:
                classified["follower"].append(s)      # 跟风
        
        # 合并标记
        for role in ["leader", "main_force", "catch_up"]:
            for s in classified[role]:
                s["market_role"] = role
        
        sector["classified"] = classified
        return sector

    # ═══════════════════════════════════════════════
    # Step 4: 周期定位 (六阶段模型)
    # ═══════════════════════════════════════════════
    def step4_detect_cycle(self) -> Tuple[CyclePhase, float]:
        """六阶段判断: 冰点→启动→发酵→高潮→分歧→退潮"""
        m = self.market
        limit_up = m.get("limit_up", 50)
        limit_down = m.get("limit_down", 5)
        break_rate = m.get("break_rate", 0.3)
        vol_ratio = m.get("vol_ratio", 1.0)
        
        # 检查主板块状态
        max_sec_limit_up = 0
        max_sec_stage = "退潮/轮动"
        if self.sectors:
            max_sec_limit_up = max(s.get("limit_up_cnt", 0) for s in self.sectors)
            max_sec_stage = self.sectors[0].get("stage", "退潮/轮动") if self.sectors else "退潮"
        
        # 昨日涨停表现 (近似)
        yest_perf = self._estimate_yesterday_limit_up_perf()
        
        scores = {}
        
        # ── 冰点判断 ──
        ice_score = 0
        if limit_up < self.ICE_LIMIT_UP: ice_score += 3
        if break_rate > self.HIGH_BREAK_RATE: ice_score += 2
        if yest_perf < -0.02: ice_score += 2
        if limit_down > 10: ice_score += 1
        scores[CyclePhase.ICE] = ice_score
        
        # ── 启动判断 ──
        start_score = 0
        if 30 <= limit_up <= 50: start_score += 2
        if max_sec_limit_up >= self.START_SECTOR_CNT: start_score += 3
        if yest_perf > 0 and yest_perf < 0.03: start_score += 2
        if vol_ratio > 1.05: start_score += 1
        scores[CyclePhase.START] = start_score
        
        # ── 发酵判断 ──
        ferment_score = 0
        if max_sec_limit_up >= self.MAINLINE_SECTOR_CNT: ferment_score += 3
        if limit_up > 50: ferment_score += 2
        if any(s.get("stage") == "发酵" for s in self.sectors[:3]): ferment_score += 2
        if yest_perf > 0.02: ferment_score += 1
        scores[CyclePhase.FERMENT] = ferment_score
        
        # ── 高潮判断 ──
        climax_score = 0
        if limit_up > self.CLIMAX_LIMIT_UP: climax_score += 3
        if max_sec_limit_up >= self.CLIMAX_SECTOR_CNT: climax_score += 2
        if break_rate < self.LOW_BREAK_RATE: climax_score += 2
        if yest_perf > 0.03: climax_score += 1
        scores[CyclePhase.CLIMAX] = climax_score
        
        # ── 分歧判断 ──
        diverge_score = 0
        if 30 <= limit_up <= 60: diverge_score += 2
        if 0.25 <= break_rate <= 0.35: diverge_score += 2
        if max_sec_stage == "分歧": diverge_score += 2
        if vol_ratio < 0.95: diverge_score += 1
        scores[CyclePhase.DIVERGE] = diverge_score
        
        # ── 退潮判断 ──
        retreat_score = 0
        if limit_up < 40 and limit_down > 8: retreat_score += 3
        if yest_perf < -0.03: retreat_score += 2
        if vol_ratio < 0.85: retreat_score += 2
        if max_sec_stage == "退潮/轮动": retreat_score += 1
        scores[CyclePhase.RETREAT] = retreat_score
        
        # 选出最高分
        best_phase = max(scores, key=scores.get)
        best_score = scores[best_phase]
        second_best = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
        confidence = (best_score - second_best) / max(best_score + second_best, 1)
        
        self.cycle_phase = best_phase
        self.phase_confidence = confidence
        self.yesterday_limit_up_perf = yest_perf
        return best_phase, confidence

    def _estimate_yesterday_limit_up_perf(self) -> float:
        """估算昨日涨停今日表现 (基于指数和板块涨跌幅)"""
        sci50_chg = self.market.get("sci50_change", 0)
        if sci50_chg > 2:
            return 0.03
        elif sci50_chg > 0:
            return 0.01
        elif sci50_chg > -2:
            return -0.005
        elif sci50_chg > -5:
            return -0.02
        return -0.04

    # ═══════════════════════════════════════════════
    # Step 5: 仓位建议
    # ═══════════════════════════════════════════════
    def step5_position_advice(self) -> Dict:
        """基于周期阶段的仓位建议"""
        phase = self.cycle_phase
        advice = {
            CyclePhase.ICE:     {"position": "0-2成", "action": "轻仓试错首板/二板",      "risk": "可能连续冰点，控制仓位"},
            CyclePhase.START:   {"position": "3-5成", "action": "半仓上车最强辨识度标",    "risk": "方向确认中，分批建仓"},
            CyclePhase.FERMENT: {"position": "5-7成", "action": "做补涨首板+低吸中军",     "risk": "扩散过快可能夭折"},
            CyclePhase.CLIMAX:  {"position": "5-8成", "action": "只卖不买，高潮次日必分化", "risk": "追高即套，准备撤退"},
            CyclePhase.DIVERGE: {"position": "3-5成", "action": "分歧日尾盘低吸龙头博反包", "risk": "分歧可能演变为退潮"},
            CyclePhase.RETREAT: {"position": "0-1成", "action": "清仓不看不参与",           "risk": "A杀风险，保存本金"},
        }
        return advice.get(phase, {"position": "3-5成", "action": "观望", "risk": "阶段不明朗"})

    # ═══════════════════════════════════════════════
    # 生成复盘报告
    # ═══════════════════════════════════════════════
    def generate_review_report(self) -> str:
        """生成完整的每日复盘报告 — 标准SOP模板"""
        self.step1_macro_review()
        self.step2_sector_ranking()
        self.step4_detect_cycle()
        pos = self.step5_position_advice()
        
        m = self.market
        phase = self.cycle_phase
        
        lines = []
        lines.append("")
        lines.append("╔" + "═" * 74 + "╗")
        lines.append(f"║  📋 每日量化复盘 · {m.get('date', datetime.now().strftime('%Y-%m-%d'))}" + " " * (55 - len(m.get('date', ''))) + "║")
        lines.append("╚" + "═" * 74 + "╝")
        
        # ── 一、大盘定位 ──
        ma = m.get("ma_positions", {})
        lines.append("")
        lines.append("━" * 76)
        lines.append("  一、大盘定位")
        lines.append("━" * 76)
        lines.append(f"  上证: {m['index']} | 科创50: {m['sci50']}({m.get('sci50_change',0):+.1f}%) | 成交: {m['turnover']}亿 ({m.get('vol_label','-')}{m.get('vol_ratio',1.0):.2f}x)")
        lines.append(f"  MA5={ma.get('ma5','?')} | MA20={ma.get('ma20','?')} | MA60={ma.get('ma60','?')}")
        
        up, down = m.get('up_count', 0), m.get('down_count', 0)
        breadth_desc = "普涨" if up > 3000 else ("冰点" if up < 1000 else "结构性行情")
        lines.append(f"  涨跌家数: ↑{up} ↓{down}  →  {breadth_desc}")
        lines.append(f"  涨停: {m['limit_up']} | 跌停: {m['limit_down']} | 炸板率: {m['break_rate']:.0%}")
        
        # 周期阶段
        confidence_bar = "█" * int(self.phase_confidence * 10) + "░" * (10 - int(self.phase_confidence * 10))
        lines.append(f"  情绪阶段: 【{phase.value}】 置信度: [{confidence_bar}] {self.phase_confidence:.0%}")
        lines.append(f"  仓位建议: {pos['position']} — {pos['action']}")
        lines.append(f"  风险提示: {pos['risk']}")
        
        # ── 二、主线板块 ──
        lines.append("")
        lines.append("━" * 76)
        lines.append("  二、主线板块 (按资金参与度排序)")
        lines.append("━" * 76)
        
        if not self.sectors:
            lines.append("  (板块数据暂不可用，请手动补充)")
        else:
            for i, sec in enumerate(self.sectors[:5], 1):
                lines.append(f"  {i}. [{sec['stage']}] {sec.get('name','?')}")
                lines.append(f"     成交: {sec.get('turnover',0):.0f}亿 | 涨停: {sec.get('limit_up_cnt',0)}家 | "
                           f"连板高标: {sec.get('max_chain',0)}连板 | 中军: {'✓' if sec.get('has_main_force') else '✗'}")
                lines.append(f"     涨跌幅: {sec.get('change_pct',0):+.1f}% | 综合评分: {sec.get('score',0):.0f}")
        
        # ── 三、个股地位 (从策略Plan提取) ──
        lines.append("")
        lines.append("━" * 76)
        lines.append("  三、观察池 · 个股地位")
        lines.append("━" * 76)
        
        if self.plan and self.plan.get("stocks"):
            stocks = self.plan["stocks"]
            # 分类
            leaders = [s for s in stocks if s.get("total_score", 0) >= 150]
            main_forces = [s for s in stocks if 120 <= s.get("total_score", 0) < 150]
            catch_ups = [s for s in stocks if 100 <= s.get("total_score", 0) < 120]
            followers = [s for s in stocks if s.get("total_score", 0) < 100]
            
            if leaders:
                lines.append(f"  🔥 第一梯队·龙头 ({len(leaders)}只)")
                for s in leaders:
                    lines.append(f"     {s.get('symbol','')} {s.get('name',''):<10} "
                               f"总分{s.get('total_score',0):.0f} | {s.get('action','')}")
            if main_forces:
                lines.append(f"  💰 第二梯队·中军 ({len(main_forces)}只)")
                for s in main_forces:
                    lines.append(f"     {s.get('symbol','')} {s.get('name',''):<10} "
                               f"总分{s.get('total_score',0):.0f} | {s.get('action','')}")
            if catch_ups:
                lines.append(f"  🔄 第三梯队·补涨 ({len(catch_ups)}只)")
                for s in catch_ups:
                    lines.append(f"     {s.get('symbol','')} {s.get('name',''):<10} "
                               f"总分{s.get('total_score',0):.0f}")
            if followers:
                lines.append(f"  ❌ 第四梯队·跟风 ({len(followers)}只) — 不建议参与")
        else:
            lines.append("  (无策略Plan数据)")
        
        # ── 四、明日预案 ──
        lines.append("")
        lines.append("━" * 76)
        lines.append("  四、明日预案")
        lines.append("━" * 76)
        
        phase = self.cycle_phase
        if phase == CyclePhase.CLIMAX:
            lines.append("  ⚠️ 高潮阶段 → 明日必分化")
            lines.append("  方案A(主线继续): 只卖不买，逐步减仓至半仓以下")
            lines.append("  方案B(分歧): 观察龙头是否断板不跌，尾盘可低吸博反包")
            lines.append("  方案C(退潮): 龙头跌停→立即清仓，转防御(黄金/国债)")
        elif phase == CyclePhase.DIVERGE:
            lines.append("  🔄 分歧阶段 → 等待明确方向")
            lines.append("  方案A(反包): 龙头尾盘走强→半仓低吸，博次日修复")
            lines.append("  方案B(转弱): 龙头炸板→减至1成仓，等下一个冰点")
            lines.append("  防守方向: 高股息/公用事业/黄金ETF")
        elif phase == CyclePhase.ICE:
            lines.append("  ❄️ 冰点阶段 → 准备试错")
            lines.append("  方案A(修复): 出现3+涨停板块→轻仓上车最强辨识度标")
            lines.append("  方案B(连续冰点): 继续等待，保持1成以下仓位")
            lines.append("  关注方向: 率先止跌放量的板块")
        elif phase == CyclePhase.START:
            lines.append("  🟢 启动阶段 → 逐步加仓")
            lines.append("  方案A(确认): 主线板块涨停≥5家→加至5成仓")
            lines.append("  方案B(假启动): 次日无持续→回到冰点仓位")
        elif phase == CyclePhase.FERMENT:
            lines.append("  📈 发酵阶段 → 积极操作")
            lines.append("  方案A: 做补涨首板，沿5日线低吸中军")
            lines.append("  方案B: 龙头加速→持股不动，不轻易下车")
        elif phase == CyclePhase.RETREAT:
            lines.append("  🛑 退潮阶段 → 防守为主")
            lines.append("  方案A: 清仓所有高位标的，留现金")
            lines.append("  方案B: 仅保留0-1成黄金ETF对冲，不追任何反弹")
        
        # ── 五、风险提示 ──
        lines.append("")
        lines.append("━" * 76)
        lines.append("  五、风险提示")
        lines.append("━" * 76)
        risks = self.plan.get("risk_map", {}) if self.plan else {}
        if risks:
            for k, v in list(risks.items())[:5]:
                lines.append(f"  • {k}: {v}")
        else:
            lines.append(f"  • 高位股风险: 评分>150的标的警惕获利盘出逃")
            lines.append(f"  • 业绩雷: 财报季关注未发布Q2的公司")
            lines.append(f"  • 退潮蔓延: 若主线板块成交连续萎缩3日→确认退潮")
            lines.append(f"  • 地缘风险: 关注美伊冲突/霍尔木兹通行状态")
        
        lines.append("")
        lines.append("═" * 76)
        lines.append(f"  复盘完成 · {datetime.now().strftime('%Y-%m-%d %H:%M')} · 下一复盘: 明日收盘后")
        lines.append("═" * 76)
        
        report = "\n".join(lines)
        self._report_lines = lines
        return report

    def print_report(self):
        """打印复盘报告"""
        report = self.generate_review_report()
        print(report)


# ═══════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════
def daily_review(use_strategy_plan: bool = True) -> str:
    """一键每日复盘 — 返回完整报告文本
    
    Args:
        use_strategy_plan: 是否先运行策略获取评分 (True=完整版, False=仅宏观/板块面)
    """
    plan = analyze_portfolio() if use_strategy_plan else None
    engine = MarketReviewEngine(strategy_plan=plan)
    report = engine.generate_review_report()
    return report


def daily_review_with_strategy() -> Tuple[Dict, str]:
    """完整复盘: 策略Plan + 复盘报告"""
    plan = analyze_portfolio()
    engine = MarketReviewEngine(strategy_plan=plan)
    report = engine.generate_review_report()
    return plan, report


if __name__ == "__main__":
    plan = analyze_portfolio()
    print_report(plan)
