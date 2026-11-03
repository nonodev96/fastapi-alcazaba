"""Background work: scheduled jobs, the task queue, leader election, locks shared by every
process, and messages to all of them (broadcast)."""

from fastapi_alcazaba.background.broadcast import FastAPIBroadcast, Message
from fastapi_alcazaba.background.leader import FastAPILeader
from fastapi_alcazaba.background.locks import Lock, LockNotAcquired
from fastapi_alcazaba.background.scheduler import FastAPIScheduler
from fastapi_alcazaba.background.tasks import DeadLetter, FastAPITaskQueue, QueueFull, TaskStatus

__all__ = [
    "DeadLetter",
    "FastAPIBroadcast",
    "FastAPILeader",
    "FastAPIScheduler",
    "FastAPITaskQueue",
    "Lock",
    "LockNotAcquired",
    "Message",
    "QueueFull",
    "TaskStatus",
]
