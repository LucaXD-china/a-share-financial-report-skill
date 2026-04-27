---
name: a-share-financial-report
description: Pulls public A-share financial statements and disclosure metadata through AkShare, then produces a Chinese financial red-flag checklist for annual, semiannual, and quarterly reports. Use when analyzing specific A股上市公司的年报、半年报、一季报、三季报, 财务质量, 现金流, 负债, 应收, 存货, 审计意见, or disclosure-risk signals.
---

# A 股财报分析

## Overview

Use this skill to fetch public A-share financial statement data with AkShare and turn it into a concise Chinese checklist of financial red flags. The bundled script is the preferred path because it keeps data fetching, normalization, and warning thresholds deterministic.

This skill is for financial quality review, not investment advice.

## Quick Start

Run the script from the Codex skill path:

```bash
SKILL_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
python "$SKILL_HOME/a-share-financial-report/scripts/analyze_a_share_report.py" \
  --symbol 600519 \
  --years 5 \
  --out-dir ./reports
```

The script accepts common A-share symbol forms such as `600519`, `SH600519`, `600519.SH`, and `sh600519`.

Useful options:

```bash
python "$SKILL_HOME/a-share-financial-report/scripts/analyze_a_share_report.py" \
  --symbol 000001 \
  --start-date 20200101 \
  --end-date 20260427 \
  --industry-profile auto \
  --format markdown,json,csv \
  --out-dir ./reports
```

## Data Sources

The script uses these AkShare interfaces:

- Company profile: `stock_individual_info_em`
- CNINFO disclosures: `stock_zh_a_disclosure_report_cninfo`
- Eastmoney statements first:
  - `stock_balance_sheet_by_report_em`
  - `stock_profit_sheet_by_report_em`
  - `stock_cash_flow_sheet_by_report_em`
  - `stock_profit_sheet_by_quarterly_em`
  - `stock_cash_flow_sheet_by_quarterly_em`
- Sina statement fallback:
  - `stock_financial_report_sina`

Do not download PDF full text unless the user explicitly asks for deeper qualitative review. The default output keeps announcement title, date, link, and risk-keyword hits.

## Review Focus

Default analysis checks:

- Revenue, net profit, and deducted net profit deterioration
- Positive profit with weak or negative operating cash flow
- Receivables and inventory growing faster than revenue
- High or rising debt ratio
- Short-term debt pressure versus cash
- Gross margin and net margin deterioration
- Large gap between reported net profit and deducted net profit
- Non-standard audit opinions
- Disclosure titles containing correction, supplement, risk warning, ST, delisting, regulatory, inquiry, investigation, or penalty keywords

When the company is a bank, insurer, broker, or other financial institution, weaken generic inventory and short-debt checks because those metrics are often not comparable.

## Output

Each run writes a timestamped folder under `--out-dir`:

```text
<out-dir>/<symbol>_<run_timestamp>/
├── report.md
├── analysis.json
├── raw/*.csv
└── normalized/*.csv
```

Read `report.md` first for the user-facing summary. Use `analysis.json` for downstream automation or follow-up analysis. Raw CSV files preserve AkShare's original columns; normalized CSV files expose stable metrics where fields are available.

## Failure Handling

If AkShare is missing, install it in the active Python environment:

```bash
python -m pip install -U akshare
```

If an AkShare endpoint returns empty data or fails, report which source failed and continue with available data. If no usable financial statements or disclosures are available, say so plainly and include the captured limitations.

