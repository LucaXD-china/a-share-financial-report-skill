#!/usr/bin/env python3
"""Fetch and review public A-share financial reports with AkShare."""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SEVERITY_ORDER = {"red": 0, "yellow": 1, "info": 2}
DISCLOSURE_CATEGORIES = ["年报", "半年报", "一季报", "三季报"]
RISK_KEYWORDS = {
    "red": ["退市", "立案", "处罚", "非标准", "无法表示意见", "否定意见"],
    "yellow": ["更正", "补充", "风险提示", "ST", "监管", "问询函", "关注函", "保留意见"],
}
FINANCIAL_INDUSTRY_KEYWORDS = ["银行", "保险", "证券", "信托", "期货", "金融", "多元金融"]
REAL_ESTATE_KEYWORDS = ["房地产", "地产开发", "房屋建设"]


class FriendlyError(Exception):
    """Expected user-facing failure."""


@dataclass(frozen=True)
class RuntimeDependencies:
    pandas: Any
    akshare: Any


@dataclass(frozen=True)
class SymbolInfo:
    code: str
    exchange: str
    em_symbol: str
    sina_symbol: str


def import_runtime_dependencies(
    importer: Callable[[str], Any] = importlib.import_module,
) -> RuntimeDependencies:
    missing = []
    try:
        pandas = importer("pandas")
    except ImportError:
        pandas = None
        missing.append("pandas")
    try:
        akshare = importer("akshare")
    except ImportError:
        akshare = None
        missing.append("akshare")
    if missing:
        packages = " ".join(missing)
        raise FriendlyError(
            "缺少运行依赖: "
            + ", ".join(missing)
            + f"\n请在当前 Python 环境安装: python -m pip install -U {packages}"
        )
    return RuntimeDependencies(pandas=pandas, akshare=akshare)


def normalize_symbol(raw_symbol: str) -> SymbolInfo:
    raw = raw_symbol.strip()
    match = re.search(r"(\d{6})", raw)
    if not match:
        raise FriendlyError(f"无法识别股票代码: {raw_symbol!r}; 请提供 6 位 A 股代码。")
    code = match.group(1)
    upper = raw.upper()

    if upper.startswith("SH") or upper.endswith(".SH"):
        exchange = "SH"
    elif upper.startswith("SZ") or upper.endswith(".SZ"):
        exchange = "SZ"
    elif upper.startswith("BJ") or upper.endswith(".BJ"):
        exchange = "BJ"
    else:
        exchange = infer_exchange(code)

    return SymbolInfo(
        code=code,
        exchange=exchange,
        em_symbol=f"{exchange}{code}",
        sina_symbol=f"{exchange.lower()}{code}",
    )


def infer_exchange(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "SH"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZ"
    if code.startswith(("430", "83", "87", "88", "92")):
        return "BJ"
    raise FriendlyError(
        f"无法根据代码 {code} 自动判断交易所；请使用 SH{code}、SZ{code} 或 BJ{code}。"
    )


def parse_yyyymmdd(value: Optional[str], field_name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise FriendlyError(f"{field_name} 必须是 YYYYMMDD 格式: {value}") from exc


def subtract_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(month=2, day=28, year=day.year - years)


def date_to_ak(day: date) -> str:
    return day.strftime("%Y%m%d")


def to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "<na>"}:
        return None
    text = text.split("T", 1)[0].split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except TypeError:
        pass
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "nat", "none", "<na>", "--"}:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def percent(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def money(value: Optional[float]) -> str:
    if value is None:
        return "-"
    abs_value = abs(value)
    if abs_value >= 1e8:
        return f"{value / 1e8:.2f} 亿元"
    if abs_value >= 1e4:
        return f"{value / 1e4:.2f} 万元"
    return f"{value:.2f} 元"


def pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous)


def ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def sanitize_column(name: str) -> str:
    return re.sub(r"[\s_（）()：:/-]+", "", str(name).upper())


def find_column(columns: Iterable[str], aliases: Sequence[str]) -> Optional[str]:
    by_sanitized = {sanitize_column(col): col for col in columns}
    for alias in aliases:
        key = sanitize_column(alias)
        if key in by_sanitized:
            return by_sanitized[key]
    for alias in aliases:
        key = sanitize_column(alias)
        for sanitized, original in by_sanitized.items():
            if key and key in sanitized:
                return original
    return None


def get_value(row: Mapping[str, Any], aliases: Sequence[str]) -> Optional[float]:
    column = find_column(row.keys(), aliases)
    if column is None:
        return None
    return safe_float(row.get(column))


def get_text(row: Mapping[str, Any], aliases: Sequence[str]) -> Optional[str]:
    column = find_column(row.keys(), aliases)
    if column is None:
        return None
    value = row.get(column)
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "<na>"}:
        return None
    return text


def call_akshare(func: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return func(**kwargs)
    except TypeError:
        kwargs.pop("timeout", None)
        return func(**kwargs)


def df_empty(df: Any) -> bool:
    return df is None or getattr(df, "empty", True)


def dataframe_to_records(df: Any) -> List[Dict[str, Any]]:
    if df_empty(df):
        return []
    records = df.to_dict(orient="records")
    return [dict(record) for record in records]


def fetch_company_info(ak: Any, symbol: SymbolInfo) -> Tuple[Dict[str, Any], List[str]]:
    warnings = []
    try:
        df = call_akshare(ak.stock_individual_info_em, symbol=symbol.code, timeout=10)
    except Exception as exc:  # AkShare/data-source error
        return {}, [f"公司信息接口失败: {exc}"]
    if df_empty(df):
        return {}, ["公司信息接口返回空数据。"]

    info = {}
    for row in dataframe_to_records(df):
        key = row.get("item")
        value = row.get("value")
        if key is not None:
            info[str(key)] = value
    if not info:
        warnings.append("公司信息结构无法识别。")
    return info, warnings


def fetch_disclosures(
    ak: Any,
    pd: Any,
    symbol: SymbolInfo,
    start_date: date,
    end_date: date,
) -> Tuple[Any, List[str]]:
    frames = []
    warnings = []
    for category in DISCLOSURE_CATEGORIES:
        try:
            df = ak.stock_zh_a_disclosure_report_cninfo(
                symbol=symbol.code,
                market="沪深京",
                category=category,
                start_date=date_to_ak(start_date),
                end_date=date_to_ak(end_date),
            )
            if not df_empty(df):
                df = df.copy()
                df["检索分类"] = category
                frames.append(df)
        except Exception as exc:  # AkShare/data-source error
            warnings.append(f"公告接口失败[{category}]: {exc}")
    if not frames:
        return pd.DataFrame(), warnings + ["公告接口未返回年报/半年报/季报数据。"]
    combined = pd.concat(frames, ignore_index=True)
    subset = [col for col in ["代码", "公告标题", "公告时间", "公告链接"] if col in combined.columns]
    if subset:
        combined = combined.drop_duplicates(subset=subset)
    return combined, warnings


def fetch_eastmoney_statements(ak: Any) -> Dict[str, Tuple[str, Callable[..., Any]]]:
    return {
        "balance_report": ("资产负债表-东财-按报告期", ak.stock_balance_sheet_by_report_em),
        "profit_report": ("利润表-东财-按报告期", ak.stock_profit_sheet_by_report_em),
        "cash_report": ("现金流量表-东财-按报告期", ak.stock_cash_flow_sheet_by_report_em),
        "profit_quarterly": ("利润表-东财-按单季度", ak.stock_profit_sheet_by_quarterly_em),
        "cash_quarterly": ("现金流量表-东财-按单季度", ak.stock_cash_flow_sheet_by_quarterly_em),
    }


def fetch_statements(ak: Any, pd: Any, symbol: SymbolInfo) -> Tuple[Dict[str, Any], List[str]]:
    data: Dict[str, Any] = {}
    warnings: List[str] = []

    for key, (label, func) in fetch_eastmoney_statements(ak).items():
        try:
            df = func(symbol=symbol.em_symbol)
            if not df_empty(df):
                data[key] = df
            else:
                data[key] = pd.DataFrame()
                warnings.append(f"{label} 返回空数据。")
        except Exception as exc:  # AkShare/data-source error
            data[key] = pd.DataFrame()
            warnings.append(f"{label} 获取失败: {exc}")

    core_keys = ["balance_report", "profit_report", "cash_report"]
    missing_core = [key for key in core_keys if df_empty(data.get(key))]
    if missing_core:
        fallback_map = {
            "sina_balance": "资产负债表",
            "sina_profit": "利润表",
            "sina_cash": "现金流量表",
        }
        for key, sheet_name in fallback_map.items():
            try:
                df = ak.stock_financial_report_sina(stock=symbol.sina_symbol, symbol=sheet_name)
                if not df_empty(df):
                    data[key] = df
                else:
                    data[key] = pd.DataFrame()
                    warnings.append(f"新浪{sheet_name}返回空数据。")
            except Exception as exc:  # AkShare/data-source error
                data[key] = pd.DataFrame()
                warnings.append(f"新浪{sheet_name}获取失败: {exc}")

    return data, warnings


def filter_records_by_date(
    records: List[Dict[str, Any]],
    start_date: date,
    end_date: date,
) -> List[Dict[str, Any]]:
    filtered = []
    for record in records:
        report_date = to_date(record.get("report_date"))
        if report_date is None:
            filtered.append(record)
            continue
        if start_date <= report_date <= end_date:
            filtered.append(record)
    return filtered


def period_type(report_date: Optional[date]) -> str:
    if report_date is None:
        return "unknown"
    if report_date.month == 3 and report_date.day == 31:
        return "Q1"
    if report_date.month == 6 and report_date.day == 30:
        return "H1"
    if report_date.month == 9 and report_date.day == 30:
        return "Q3"
    if report_date.month == 12 and report_date.day == 31:
        return "FY"
    return f"{report_date.month:02d}-{report_date.day:02d}"


def extract_statement_metrics(
    records: List[Dict[str, Any]],
    date_aliases: Sequence[str],
    metric_aliases: Mapping[str, Sequence[str]],
) -> Dict[str, Dict[str, Optional[float]]]:
    by_date: Dict[str, Dict[str, Optional[float]]] = {}
    for row in records:
        date_text = get_text(row, date_aliases)
        report_date = to_date(date_text)
        if report_date is None:
            continue
        key = report_date.isoformat()
        target = by_date.setdefault(key, {})
        for metric, aliases in metric_aliases.items():
            value = get_value(row, aliases)
            if value is not None and target.get(metric) is None:
                target[metric] = value
    return by_date


def normalize_statements(
    statements: Mapping[str, Any],
    start_date: date,
    end_date: date,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings = []
    balance_records = dataframe_to_records(
        statements.get("balance_report")
        if not df_empty(statements.get("balance_report"))
        else statements.get("sina_balance")
    )
    profit_records = dataframe_to_records(
        statements.get("profit_report")
        if not df_empty(statements.get("profit_report"))
        else statements.get("sina_profit")
    )
    cash_records = dataframe_to_records(
        statements.get("cash_report")
        if not df_empty(statements.get("cash_report"))
        else statements.get("sina_cash")
    )

    date_aliases = ["REPORT_DATE", "报告日", "report_date", "日期", "公告日期"]
    balance = extract_statement_metrics(
        balance_records,
        date_aliases,
        {
            "total_assets": ["TOTAL_ASSETS", "资产总计", "资产总额"],
            "total_liabilities": ["TOTAL_LIABILITIES", "负债合计", "负债总计"],
            "monetary_funds": ["MONETARYFUNDS", "MONETARY_FUNDS", "货币资金"],
            "receivables": ["ACCOUNTS_RECE", "ACCOUNTS_RECEIVABLE", "应收账款", "应收款项"],
            "inventory": ["INVENTORY", "存货"],
            "short_loan": ["SHORT_LOAN", "短期借款"],
            "noncurrent_liab_1year": ["NONCURRENT_LIAB_1YEAR", "一年内到期的非流动负债"],
            "short_bond_payable": ["SHORT_BOND_PAYABLE", "应付短期债券"],
        },
    )
    profit = extract_statement_metrics(
        profit_records,
        date_aliases,
        {
            "revenue": ["OPERATE_INCOME", "TOTAL_OPERATE_INCOME", "营业收入", "营业总收入"],
            "operating_cost": ["OPERATE_COST", "营业成本"],
            "gross_profit": ["GROSSPROFIT", "GROSS_PROFIT", "毛利"],
            "parent_net_profit": ["PARENT_NETPROFIT", "归属于母公司股东的净利润", "归母净利润"],
            "deduct_parent_net_profit": [
                "DEDUCT_PARENT_NETPROFIT",
                "扣除非经常性损益后的净利润",
                "扣非归母净利润",
            ],
            "net_profit": ["NETPROFIT", "净利润"],
        },
    )
    cash = extract_statement_metrics(
        cash_records,
        date_aliases,
        {
            "operating_cash_flow": [
                "NETCASH_OPERATE",
                "经营活动产生的现金流量净额",
                "经营活动现金流量净额",
            ],
            "investing_cash_flow": ["NETCASH_INVEST", "投资活动产生的现金流量净额"],
            "financing_cash_flow": ["NETCASH_FINANCE", "筹资活动产生的现金流量净额"],
        },
    )

    all_dates = sorted(set(balance) | set(profit) | set(cash))
    rows: List[Dict[str, Any]] = []
    for date_key in all_dates:
        report_date = to_date(date_key)
        if report_date is None or not (start_date <= report_date <= end_date):
            continue
        row: Dict[str, Any] = {
            "report_date": date_key,
            "year": report_date.year,
            "period_type": period_type(report_date),
        }
        row.update(balance.get(date_key, {}))
        row.update(profit.get(date_key, {}))
        row.update(cash.get(date_key, {}))
        row["short_debt"] = sum_present(
            [row.get("short_loan"), row.get("noncurrent_liab_1year"), row.get("short_bond_payable")]
        )
        if row["short_debt"] == 0:
            row["short_debt"] = None
        row["debt_ratio"] = ratio(row.get("total_liabilities"), row.get("total_assets"))
        row["cfo_to_profit"] = ratio(row.get("operating_cash_flow"), row.get("parent_net_profit"))
        row["gross_margin"] = derive_gross_margin(row)
        row["net_margin"] = ratio(row.get("parent_net_profit"), row.get("revenue"))
        row["short_debt_cash_ratio"] = ratio(row.get("short_debt"), row.get("monetary_funds"))
        rows.append(row)

    rows.sort(key=lambda item: item["report_date"])
    add_yoy_metrics(rows)
    if not rows:
        warnings.append("未能从三大报表中归一化出指定时间范围内的财务指标。")
    return rows, warnings


def sum_present(values: Iterable[Optional[float]]) -> Optional[float]:
    total = 0.0
    found = False
    for value in values:
        if value is not None:
            total += value
            found = True
    return total if found else None


def derive_gross_margin(row: Mapping[str, Any]) -> Optional[float]:
    revenue = row.get("revenue")
    if revenue is None or revenue == 0:
        return None
    gross_profit = row.get("gross_profit")
    if gross_profit is not None:
        return ratio(gross_profit, revenue)
    operating_cost = row.get("operating_cost")
    if operating_cost is None:
        return None
    return ratio(revenue - operating_cost, revenue)


def add_yoy_metrics(rows: List[Dict[str, Any]]) -> None:
    by_date = {row["report_date"]: row for row in rows}
    metrics = [
        "revenue",
        "parent_net_profit",
        "deduct_parent_net_profit",
        "operating_cash_flow",
        "receivables",
        "inventory",
        "total_assets",
        "total_liabilities",
    ]
    for row in rows:
        report_date = to_date(row["report_date"])
        if report_date is None:
            continue
        previous_key = subtract_years(report_date, 1).isoformat()
        previous = by_date.get(previous_key)
        for metric in metrics:
            row[f"{metric}_yoy"] = pct_change(row.get(metric), previous.get(metric) if previous else None)
        if previous:
            for metric in ["gross_margin", "net_margin", "debt_ratio"]:
                current = row.get(metric)
                old = previous.get(metric)
                row[f"{metric}_change"] = None if current is None or old is None else current - old


def classify_industry(profile: str, company_info: Mapping[str, Any]) -> str:
    if profile != "auto":
        return profile
    profile_text = " ".join(
        str(company_info.get(key) or "")
        for key in ["行业", "所属行业", "股票简称", "名称", "SECURITY_NAME_ABBR"]
    )
    if any(keyword in profile_text for keyword in FINANCIAL_INDUSTRY_KEYWORDS):
        return "financial"
    if any(keyword in profile_text for keyword in REAL_ESTATE_KEYWORDS):
        return "real-estate"
    return "general"


def infer_company_profile(
    company_info: Mapping[str, Any],
    statements: Mapping[str, Any],
    disclosures: List[Dict[str, Any]],
) -> Dict[str, Any]:
    profile = dict(company_info)
    if not (profile.get("股票简称") or profile.get("名称")):
        name = first_statement_text(statements, ["SECURITY_NAME_ABBR", "股票简称", "简称", "名称"])
        if name is None:
            name = first_disclosure_text(disclosures, ["简称", "名称"])
        if name:
            profile["股票简称"] = name
    if not profile.get("行业"):
        industry = first_statement_text(statements, ["INDUSTRY", "INDUSTRY_NAME", "所属行业", "行业"])
        if industry:
            profile["行业"] = industry
    return profile


def first_statement_text(statements: Mapping[str, Any], aliases: Sequence[str]) -> Optional[str]:
    for df in statements.values():
        for row in dataframe_to_records(df):
            text = get_text(row, aliases)
            if text:
                return text
    return None


def first_disclosure_text(records: List[Dict[str, Any]], aliases: Sequence[str]) -> Optional[str]:
    for row in records:
        text = get_text(row, aliases)
        if text:
            return text
    return None


def make_signal(
    level: str,
    topic: str,
    period: str,
    metric: str,
    evidence: str,
    suggested_check: str,
) -> Dict[str, str]:
    return {
        "level": level,
        "topic": topic,
        "period": period,
        "metric": metric,
        "evidence": evidence,
        "suggested_check": suggested_check,
    }


def assess_financial_signals(
    rows: List[Dict[str, Any]],
    industry_profile: str = "general",
) -> List[Dict[str, str]]:
    signals: List[Dict[str, str]] = []
    for row in rows:
        period = str(row.get("report_date", "-"))
        add_yoy_signal(
            signals,
            row,
            "revenue_yoy",
            "收入增长",
            "营业收入同比",
            -0.4,
            -0.2,
            "核对主营业务量价变化、一次性因素和收入确认政策。",
        )
        add_yoy_signal(
            signals,
            row,
            "parent_net_profit_yoy",
            "利润增长",
            "归母净利润同比",
            -0.5,
            -0.3,
            "拆分毛利率、费用率、减值、公允价值变动和非经常性损益。",
        )
        add_yoy_signal(
            signals,
            row,
            "deduct_parent_net_profit_yoy",
            "利润质量",
            "扣非归母净利润同比",
            -0.5,
            -0.3,
            "确认主营盈利是否同步恶化，避免只看归母净利润。",
        )

        profit = row.get("parent_net_profit")
        cfo = row.get("operating_cash_flow")
        cfo_to_profit = row.get("cfo_to_profit")
        if profit is not None and profit > 0 and cfo is not None and cfo < 0:
            signals.append(
                make_signal(
                    "red",
                    "现金流质量",
                    period,
                    "经营现金流/归母净利润",
                    f"归母净利润为 {money(profit)}，但经营现金流为 {money(cfo)}。",
                    "重点看应收、存货、预付款、经营性应付款和收入回款质量。",
                )
            )
        elif profit is not None and profit > 0 and cfo_to_profit is not None and cfo_to_profit < 0.5:
            signals.append(
                make_signal(
                    "yellow",
                    "现金流质量",
                    period,
                    "经营现金流/归母净利润",
                    f"经营现金流/归母净利润为 {cfo_to_profit:.2f}。",
                    "核对现金利润转化率是否连续偏低。",
                )
            )

        revenue_yoy = row.get("revenue_yoy")
        if industry_profile != "financial":
            add_working_capital_signal(
                signals,
                row,
                "receivables_yoy",
                revenue_yoy,
                "应收账款",
                "应收增速明显高于收入增速，需核对客户信用和坏账准备。",
            )
            if industry_profile != "real-estate":
                add_working_capital_signal(
                    signals,
                    row,
                    "inventory_yoy",
                    revenue_yoy,
                    "存货",
                    "存货增速明显高于收入增速，需核对跌价准备和去化压力。",
                )

        debt_ratio = row.get("debt_ratio")
        if industry_profile != "financial" and debt_ratio is not None:
            if debt_ratio >= 0.85:
                level = "red"
            elif debt_ratio >= 0.70:
                level = "yellow"
            else:
                level = ""
            if level:
                signals.append(
                    make_signal(
                        level,
                        "杠杆水平",
                        period,
                        "资产负债率",
                        f"资产负债率为 {percent(debt_ratio)}。",
                        "结合行业特征、债务期限、利息保障倍数和再融资能力复核。",
                    )
                )

        short_debt_cash = row.get("short_debt_cash_ratio")
        if industry_profile != "financial" and short_debt_cash is not None:
            if short_debt_cash >= 2.0:
                level = "red"
            elif short_debt_cash >= 1.0:
                level = "yellow"
            else:
                level = ""
            if level:
                signals.append(
                    make_signal(
                        level,
                        "短债压力",
                        period,
                        "短债/货币资金",
                        f"短债/货币资金为 {short_debt_cash:.2f}。",
                        "核对受限货币资金、短期借款到期安排和经营现金流覆盖。",
                    )
                )

        margin_change = row.get("gross_margin_change")
        if margin_change is not None and margin_change <= -0.10:
            level = "red"
        elif margin_change is not None and margin_change <= -0.05:
            level = "yellow"
        else:
            level = ""
        if level:
            signals.append(
                make_signal(
                    level,
                    "盈利能力",
                    period,
                    "毛利率同比变化",
                    f"毛利率同比下降 {abs(margin_change) * 100:.1f} 个百分点。",
                    "核对产品结构、原材料成本、价格竞争和会计口径变化。",
                )
            )

        net_margin_change = row.get("net_margin_change")
        if net_margin_change is not None and net_margin_change <= -0.05:
            signals.append(
                make_signal(
                    "yellow",
                    "盈利能力",
                    period,
                    "净利率同比变化",
                    f"净利率同比下降 {abs(net_margin_change) * 100:.1f} 个百分点。",
                    "拆分毛利率、期间费用率、减值损失和非经常性损益。",
                )
            )

        deduct = row.get("deduct_parent_net_profit")
        if profit is not None and profit > 0 and deduct is not None:
            deduct_ratio = deduct / profit
            if deduct < 0:
                level = "red"
            elif deduct_ratio < 0.7:
                level = "yellow"
            else:
                level = ""
            if level:
                signals.append(
                    make_signal(
                        level,
                        "利润质量",
                        period,
                        "扣非归母/归母",
                        f"扣非归母/归母为 {deduct_ratio:.2f}。",
                        "核对政府补助、处置收益、公允价值变动等非经常项目。",
                    )
                )

    signals.sort(key=lambda item: (SEVERITY_ORDER.get(item["level"], 9), item["period"], item["topic"]))
    return signals


def add_yoy_signal(
    signals: List[Dict[str, str]],
    row: Mapping[str, Any],
    metric_key: str,
    topic: str,
    metric_name: str,
    red_threshold: float,
    yellow_threshold: float,
    suggested_check: str,
) -> None:
    value = row.get(metric_key)
    if value is None:
        return
    level = ""
    if value <= red_threshold:
        level = "red"
    elif value <= yellow_threshold:
        level = "yellow"
    if level:
        signals.append(
            make_signal(
                level,
                topic,
                str(row.get("report_date", "-")),
                metric_name,
                f"{metric_name}为 {percent(value)}。",
                suggested_check,
            )
        )


def add_working_capital_signal(
    signals: List[Dict[str, str]],
    row: Mapping[str, Any],
    metric_key: str,
    revenue_yoy: Optional[float],
    metric_name: str,
    suggested_check: str,
) -> None:
    value = row.get(metric_key)
    if value is None:
        return
    gap = value - (revenue_yoy if revenue_yoy is not None else 0.0)
    if value >= 0.5 and gap >= 0.3:
        level = "red"
    elif value >= 0.3 and gap >= 0.2:
        level = "yellow"
    else:
        return
    signals.append(
        make_signal(
            level,
            "营运资本",
            str(row.get("report_date", "-")),
            f"{metric_name}同比",
            f"{metric_name}同比 {percent(value)}，营业收入同比 {percent(revenue_yoy)}。",
            suggested_check,
        )
    )


def extract_audit_opinion_signals(statements: Mapping[str, Any]) -> List[Dict[str, str]]:
    signals: List[Dict[str, str]] = []
    for key, df in statements.items():
        if df_empty(df):
            continue
        for row in dataframe_to_records(df):
            report_date = to_date(get_text(row, ["REPORT_DATE", "报告日", "report_date"]))
            opinion = get_text(row, ["OPINION_TYPE", "OSOPINION_TYPE", "审计意见"])
            if not opinion:
                continue
            if "标准无保留" in opinion:
                continue
            level = "red" if any(word in opinion for word in ["否定", "无法", "保留"]) else "yellow"
            signals.append(
                make_signal(
                    level,
                    "审计意见",
                    report_date.isoformat() if report_date else "-",
                    "审计意见",
                    f"{key} 出现审计意见: {opinion}。",
                    "阅读审计报告全文，重点看形成意见的基础和持续经营不确定性。",
                )
            )
    return dedupe_signals(signals)


def disclosure_risk_signals(disclosure_records: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    signals: List[Dict[str, str]] = []
    for row in disclosure_records:
        title = str(row.get("公告标题") or row.get("title") or "")
        if not title:
            continue
        level, hits = classify_disclosure_title(title)
        if not hits:
            continue
        period = str(row.get("公告时间") or row.get("date") or "-")
        link = str(row.get("公告链接") or row.get("url") or "")
        evidence = f"公告《{title}》命中关键词: {', '.join(hits)}。"
        if link:
            evidence += f" 链接: {link}"
        signals.append(
            make_signal(
                level,
                "披露风险",
                period,
                "公告标题关键词",
                evidence,
                "打开公告链接核对事项性质、影响期间、是否涉及财报重述或监管风险。",
            )
        )
    signals.sort(key=lambda item: SEVERITY_ORDER.get(item["level"], 9))
    return signals


def classify_disclosure_title(title: str) -> Tuple[str, List[str]]:
    red_hits = [word for word in RISK_KEYWORDS["red"] if word in title]
    yellow_hits = [word for word in RISK_KEYWORDS["yellow"] if word in title]
    if red_hits:
        return "red", red_hits + yellow_hits
    if yellow_hits:
        return "yellow", yellow_hits
    return "info", []


def dedupe_signals(signals: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    result = []
    for signal in signals:
        key = (
            signal.get("level"),
            signal.get("topic"),
            signal.get("period"),
            signal.get("metric"),
            signal.get("evidence"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(signal)
    return result


def to_jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
    except TypeError:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return value


def clean_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {str(key): to_jsonable(value) for key, value in record.items()}


def write_dataframe_csv(df: Any, path: Path) -> None:
    if df_empty(df):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def write_records_csv(records: List[Mapping[str, Any]], path: Path) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    columns: List[str] = []
    for record in records:
        for key in record:
            if key not in columns:
                columns.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            writer.writerow({key: to_jsonable(record.get(key)) for key in columns})


def markdown_report(analysis: Mapping[str, Any]) -> str:
    company = analysis["company"]
    signals = analysis["signals"]
    limitations = analysis["limitations"]
    generated_at = analysis["generated_at"]
    lines = [
        f"# {company.get('name') or company.get('symbol')} 财报预警清单",
        "",
        f"- 股票代码: `{company.get('symbol')}`",
        f"- 行业: {company.get('industry') or '-'}",
        f"- 生成时间: {generated_at}",
        "- 定位: 财务风险/质量复核，不构成投资建议。",
        "",
        "## 结论概览",
        "",
    ]
    red_count = sum(1 for item in signals if item.get("level") == "red")
    yellow_count = sum(1 for item in signals if item.get("level") == "yellow")
    if not signals:
        lines.append("未发现脚本规则覆盖范围内的红黄旗；仍建议结合公告全文和行业口径人工复核。")
    else:
        lines.append(f"共发现 {red_count} 条红色信号、{yellow_count} 条黄色信号。优先处理红色信号。")
    lines += ["", "## 风险信号", ""]
    if signals:
        lines.append("| 等级 | 主题 | 期间 | 指标 | 证据 | 建议复核 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for signal in signals[:50]:
            lines.append(
                "| {level} | {topic} | {period} | {metric} | {evidence} | {check} |".format(
                    level=signal["level"],
                    topic=escape_md(signal["topic"]),
                    period=escape_md(signal["period"]),
                    metric=escape_md(signal["metric"]),
                    evidence=escape_md(signal["evidence"]),
                    check=escape_md(signal["suggested_check"]),
                )
            )
        if len(signals) > 50:
            lines.append(f"\n仅展示前 50 条；完整结果见 `analysis.json`。")
    else:
        lines.append("无。")

    latest = analysis.get("latest_metrics") or {}
    lines += ["", "## 最近一期关键指标", ""]
    if latest:
        rows = [
            ("报告期", latest.get("report_date")),
            ("营业收入", money(latest.get("revenue"))),
            ("归母净利润", money(latest.get("parent_net_profit"))),
            ("经营现金流", money(latest.get("operating_cash_flow"))),
            ("资产负债率", percent(latest.get("debt_ratio"))),
            ("经营现金流/归母净利润", format_number(latest.get("cfo_to_profit"))),
            ("毛利率", percent(latest.get("gross_margin"))),
            ("净利率", percent(latest.get("net_margin"))),
        ]
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        for key, value in rows:
            lines.append(f"| {key} | {value if value is not None else '-'} |")
    else:
        lines.append("无可用归一化指标。")

    lines += ["", "## 数据限制", ""]
    if limitations:
        for item in limitations:
            lines.append(f"- {item}")
    else:
        lines.append("- 无。")
    lines.append("")
    return "\n".join(lines)


def escape_md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def format_number(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "-"
    return f"{number:.2f}"


def build_output_dir(base_dir: Path, symbol: SymbolInfo, now: datetime) -> Path:
    return base_dir / f"{symbol.code}_{now.strftime('%Y%m%d_%H%M%S')}"


def save_outputs(
    output_dir: Path,
    formats: Sequence[str],
    analysis: Mapping[str, Any],
    statements: Mapping[str, Any],
    disclosures_df: Any,
    normalized_rows: List[Dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    normalized_dir = output_dir / "normalized"

    if "csv" in formats:
        for name, df in statements.items():
            write_dataframe_csv(df, raw_dir / f"{name}.csv")
        write_dataframe_csv(disclosures_df, raw_dir / "disclosures_cninfo.csv")
        write_records_csv(normalized_rows, normalized_dir / "financial_metrics.csv")
        write_records_csv(analysis.get("disclosures", []), normalized_dir / "disclosures.csv")

    if "json" in formats:
        with (output_dir / "analysis.json").open("w", encoding="utf-8") as handle:
            json.dump(analysis, handle, ensure_ascii=False, indent=2, default=to_jsonable)

    if "markdown" in formats:
        (output_dir / "report.md").write_text(markdown_report(analysis), encoding="utf-8")


def parse_formats(raw: str) -> List[str]:
    allowed = {"markdown", "json", "csv"}
    formats = [item.strip().lower() for item in raw.split(",") if item.strip()]
    invalid = sorted(set(formats) - allowed)
    if invalid:
        raise FriendlyError(f"--format 仅支持 markdown,json,csv; 不支持: {', '.join(invalid)}")
    return formats or ["markdown", "json", "csv"]


def run(args: argparse.Namespace) -> Path:
    deps = import_runtime_dependencies()
    pd = deps.pandas
    ak = deps.akshare
    symbol = normalize_symbol(args.symbol)

    end_date = parse_yyyymmdd(args.end_date, "--end-date") or date.today()
    start_date = parse_yyyymmdd(args.start_date, "--start-date") or subtract_years(end_date, args.years)
    if start_date > end_date:
        raise FriendlyError("--start-date 不能晚于 --end-date。")

    formats = parse_formats(args.format)
    limitations: List[str] = []

    company_info, warnings = fetch_company_info(ak, symbol)
    limitations.extend(warnings)
    disclosures_df, warnings = fetch_disclosures(ak, pd, symbol, start_date, end_date)
    limitations.extend(warnings)
    statements, warnings = fetch_statements(ak, pd, symbol)
    limitations.extend(warnings)
    normalized_rows, warnings = normalize_statements(statements, start_date, end_date)
    limitations.extend(warnings)

    disclosures = [clean_record(row) for row in dataframe_to_records(disclosures_df)]
    company_profile = infer_company_profile(company_info, statements, disclosures)
    industry_profile = classify_industry(args.industry_profile, company_profile)
    signals = assess_financial_signals(normalized_rows, industry_profile=industry_profile)
    signals.extend(extract_audit_opinion_signals(statements))
    signals.extend(disclosure_risk_signals(disclosures))
    signals = dedupe_signals(signals)
    signals.sort(key=lambda item: (SEVERITY_ORDER.get(item["level"], 9), item["period"], item["topic"]))

    if not normalized_rows and not disclosures:
        raise FriendlyError("未获取到可用财报或公告数据；请检查股票代码、日期范围、网络和 AkShare 数据源状态。")

    latest_metrics = normalized_rows[-1] if normalized_rows else {}
    now = datetime.now()
    analysis = {
        "generated_at": now.isoformat(timespec="seconds"),
        "company": {
            "symbol": symbol.code,
            "exchange": symbol.exchange,
            "name": company_profile.get("股票简称") or company_profile.get("名称"),
            "industry": company_profile.get("行业") or company_profile.get("所属行业"),
            "industry_profile": industry_profile,
            "raw_info": clean_record(company_profile),
        },
        "date_range": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "latest_metrics": clean_record(latest_metrics),
        "normalized_metrics": [clean_record(row) for row in normalized_rows],
        "disclosures": disclosures,
        "signals": signals,
        "limitations": limitations,
    }

    output_dir = build_output_dir(Path(args.out_dir).expanduser(), symbol, now)
    save_outputs(output_dir, formats, analysis, statements, disclosures_df, normalized_rows)
    return output_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull A-share financial statements with AkShare and generate a Chinese red-flag checklist.",
    )
    parser.add_argument("--symbol", required=True, help="A-share code, e.g. 600519, SH600519, 000001.SZ")
    parser.add_argument("--years", type=int, default=5, help="Lookback years when --start-date is omitted.")
    parser.add_argument("--start-date", help="Start date in YYYYMMDD.")
    parser.add_argument("--end-date", help="End date in YYYYMMDD.")
    parser.add_argument("--out-dir", default="./reports", help="Output directory.")
    parser.add_argument(
        "--format",
        default="markdown,json,csv",
        help="Comma-separated output formats: markdown,json,csv.",
    )
    parser.add_argument(
        "--industry-profile",
        choices=["auto", "general", "financial", "real-estate"],
        default="auto",
        help="Adjust checks for industry profile.",
    )
    parser.add_argument("--refresh", action="store_true", help="Reserved for future cache refresh behavior.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output_dir = run(args)
    except FriendlyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: 用户中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # keep CLI readable for data-source instability
        print(f"ERROR: 执行失败: {exc}", file=sys.stderr)
        return 1
    print(f"OK: 输出已生成: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
