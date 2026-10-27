"""The audit trail: who did what to which resource and whether it worked, signed and chained."""

from fastapi_alcazaba.audit.trail import (
    SENSITIVE_WORDS,
    AuditEntry,
    AuditEvent,
    AuditSink,
    FastAPIAudit,
    LoggingAuditSink,
    MemoryAuditSink,
    verify_audit_trail,
)

__all__ = [
    "AuditEntry",
    "AuditEvent",
    "AuditSink",
    "FastAPIAudit",
    "LoggingAuditSink",
    "MemoryAuditSink",
    "SENSITIVE_WORDS",
    "verify_audit_trail",
]
