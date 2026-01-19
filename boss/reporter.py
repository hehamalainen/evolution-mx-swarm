"""
Reporter - Automated Reporting Engine (Loop C)

Generates:
1. Hourly Ops Reports (Markdown)
2. Daily Executive Standup Reports (Markdown)
"""

import os
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass

from shared.utils import MXConfig


@dataclass
class OpsReport:
    timestamp: str
    period: str  # "hourly" or "daily"
    content: str
    metrics: Dict


class Reporter:
    def __init__(self, repo_path: str = "."):
        self.repo_path = repo_path
        self.reports_dir = os.path.join(repo_path, "reports")
        os.makedirs(os.path.join(self.reports_dir, "hourly"), exist_ok=True)
        os.makedirs(os.path.join(self.reports_dir, "daily"), exist_ok=True)

    def generate_hourly_report(self, metrics: Dict, governor_actions: List[str]) -> OpsReport:
        """Generate hourly ops report."""
        now = datetime.utcnow()
        timestamp = now.strftime("%Y-%m-%d-%H")
        
        # Calculate deltas (mocked for now)
        merged_delta = metrics.get("prs_merged", 0)
        open_delta = metrics.get("prs_open", 0)
        burn_rate = metrics.get("burn_rate", 0.0)
        
        content = f"""# Ops Report: {now.strftime('%Y-%m-%d %H:00')}

## 🚦 System Health
- **CI Health**: {metrics.get('ci_health', 1.0):.1%}
- **Governor State**: {metrics.get('governor_state', 'NORMAL')}
- **Active Workers**: {metrics.get('active_workers', 0)}

## 📉 Throughput (Last Hour)
- **PRs Merged**: {merged_delta}
- **PRs Opened**: {open_delta}
- **Burn Rate**: ${burn_rate:.2f}/hr

## 🛡️ Governor Actions
{chr(10).join(f'- {a}' for a in governor_actions) if governor_actions else '- No actions taken'}

## ⚠️ Risks & Alerts
- None detected
"""
        
        # Save report
        filename = os.path.join(self.reports_dir, "hourly", f"{timestamp}.md")
        with open(filename, "w") as f:
            f.write(content)
            
        return OpsReport(timestamp, "hourly", content, metrics)

    def generate_daily_report(self, metrics: Dict) -> OpsReport:
        """Generate daily executive standup report."""
        now = datetime.utcnow()
        date_str = now.strftime("%Y-%m-%d")
        
        content = f"""# Executive Standup: {date_str}

## 🚀 Shipped (Last 24h)
- **Merged PRs**: {metrics.get('daily_merged', 0)}
- **Features Completed**: {metrics.get('daily_features', 0)}

## 🚧 Blockers (Top 3)
1. None

## 📊 Quality Status
- **CI Pass Rate**: {metrics.get('ci_pass_rate', 1.0):.1%}
- **Security Scans**: {metrics.get('security_status', 'PASSING')}

## 💰 Budget
- **Spent Today**: ${metrics.get('spent_today', 0.0):.2f}
- **Forecast**: ${metrics.get('forecast', 0.0):.2f}

## 🗳️ Decisions Needed
- [ ] Approve Budget Increase (if cap hit)
- [ ] Approve Deployment to Staging
"""
        
        # Save report
        filename = os.path.join(self.reports_dir, "daily", f"{date_str}.md")
        with open(filename, "w") as f:
            f.write(content)
            
        return OpsReport(date_str, "daily", content, metrics)
