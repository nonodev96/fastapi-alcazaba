"""The audit trail: who did what to which resource and whether it worked, signed and chained."""

from fastapi_alcazaba.audit.keys import generate_audit_key_pair
from fastapi_alcazaba.audit.trail import (
    CHECKPOINT,
    SENSITIVE_WORDS,
    AuditEntry,
    AuditEvent,
    AuditSink,
    FastAPIAudit,
    LoggingAuditSink,
    MemoryAuditSink,
    last_checkpoint,
    redacted,
    verify_audit_trail,
)

__all__ = [
    "CHECKPOINT",
    "AuditEntry",
    "AuditEvent",
    "AuditSink",
    "FastAPIAudit",
    "LoggingAuditSink",
    "MemoryAuditSink",
    "SENSITIVE_WORDS",
    "generate_audit_key_pair",
    "last_checkpoint",
    "redacted",
    "verify_audit_trail",
]
