# Background work

Scheduled jobs, the task queue, leader election and locks shared by every process, broadcast,
and the outbox. In `fastapi_alcazaba.background`; the outbox in `fastapi_alcazaba.background.outbox`.

In the guide: [Scheduled jobs](../guide/background.md#scheduled-jobs), [Task queue](../guide/background.md#task-queue), [Leader election](../guide/background.md#leader-election), [Locks](../guide/background.md#locks), [Broadcast](../guide/background.md#broadcast), [Outbox](../guide/background.md#outbox). In an app: [the example](../examples/background.md).

::: fastapi_alcazaba.background.FastAPIScheduler

::: fastapi_alcazaba.background.FastAPITaskQueue

::: fastapi_alcazaba.background.DeadLetter

::: fastapi_alcazaba.background.TaskStatus

::: fastapi_alcazaba.background.QueueFull

::: fastapi_alcazaba.background.FastAPILeader

::: fastapi_alcazaba.background.Lock

::: fastapi_alcazaba.background.LockNotAcquired

::: fastapi_alcazaba.background.FastAPIBroadcast

::: fastapi_alcazaba.background.Message

::: fastapi_alcazaba.background.outbox.SQLAlchemyOutbox

::: fastapi_alcazaba.background.outbox.outbox_table

::: fastapi_alcazaba.background.outbox.OutboxMessage

::: fastapi_alcazaba.background.outbox.OutboxRelay
