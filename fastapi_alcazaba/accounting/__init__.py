"""Accounting: what each subject uses of each meter, per period, held to its quotas, with a
usage record per charge for billing; and what each user may cost the app at a provider it pays
for (`FastAPIProviderBudget`).

`SQLBudgetLimits` and `provider_budgets_table` need the `sqlalchemy` extra: they're imported when
first used.
"""

from typing import TYPE_CHECKING, Any

from fastapi_alcazaba.accounting.budget import (
    BudgetLimits,
    BudgetRefused,
    BudgetUsage,
    FastAPIProviderBudget,
    ProviderCall,
)
from fastapi_alcazaba.accounting.meter import (
    PERIODS,
    FastAPIAccounting,
    Metering,
    Period,
    Quota,
    UsageReport,
    period_of,
)
from fastapi_alcazaba.accounting.records import (
    LoggingUsageSink,
    MemoryUsageSink,
    UsageRecord,
    UsageSink,
)

if TYPE_CHECKING:
    from fastapi_alcazaba.accounting.budget_sql import SQLBudgetLimits, provider_budgets_table

_LAZY = {"SQLBudgetLimits", "provider_budgets_table"}

__all__ = [
    "PERIODS",
    "BudgetLimits",
    "BudgetRefused",
    "BudgetUsage",
    "FastAPIAccounting",
    "FastAPIProviderBudget",
    "LoggingUsageSink",
    "MemoryUsageSink",
    "Metering",
    "Period",
    "ProviderCall",
    "Quota",
    "SQLBudgetLimits",
    "UsageRecord",
    "UsageReport",
    "UsageSink",
    "period_of",
    "provider_budgets_table",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from fastapi_alcazaba.accounting import budget_sql

        return getattr(budget_sql, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
