"""Background work for a sign-up service: a task queue, scheduled jobs and a leader, on Redis.

    export REDIS_URL=redis://127.0.0.1:6379/0
    export CACHE_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
    export ADMIN_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
    uvicorn examples.background.main:app

Every process runs the three tools, started and stopped with the app, and they share the work
through Redis: in production, run as many processes as you need (`--workers`, more machines).
Here the database is a stand-in in each process's memory, so try it with one.

- FastAPITaskQueue: POST /users hands the welcome e-mail to the queue, which retries it while the
  e-mail provider is down. After its last retry it's a dead letter, which the admin endpoints
  list and put back in the queue once the provider is back.
- FastAPIScheduler: trials end on the hour, and the team gets a sign-up report on weekdays at
  8:00 in Madrid; each run happens in one process only.
- FastAPILeader: one process relays the outbox (the events saved with the users) to the message
  broker; if it stops, another one takes over.

The e-mail provider is a stand-in too: `PUT /admin/simulate/mailer?down=true` makes it fail, to
watch retries and dead letters. The admin routes take `Authorization: Bearer $ADMIN_TOKEN`. The
variables above can go in a .env file instead.
"""

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis

from fastapi_alcazaba import FastAPICache, FastAPILeader, FastAPIScheduler, FastAPITaskQueue
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.background import DeadLetter

logger = logging.getLogger("signup")


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    redis_url: str = "redis://127.0.0.1:6379/0"  # rediss://, with a password, in production
    # Every process needs the same key, or the tasks one seals are refused by the others. The
    # random default only suits a single process that you try out locally.
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)
    # Unset: a random token nobody knows, so the admin endpoints stay closed.
    admin_token: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()
REPORT_TO = "team@example.com"
TRIAL = timedelta(days=14)
RELAY_INTERVAL = 1.0  # seconds between looks at the outbox


def redis_client() -> Redis:
    """The connection to Redis or Valkey."""
    return Redis.from_url(settings.redis_url)


class MailerUnavailable(Exception):
    """The e-mail provider didn't answer."""


class Mailer:
    """Stands in for the e-mail provider's client."""

    def __init__(self) -> None:
        self.available = True
        self.sent: list[tuple[str, str]] = []  # (to, subject)

    async def send(self, to: str, subject: str) -> None:
        if not self.available:
            raise MailerUnavailable("503 Service Unavailable")
        self.sent.append((to, subject))


@dataclass
class User:
    id: int
    email: str
    name: str
    created_at: datetime
    plan: str = "trial"
    welcomed: bool = False


class Database:
    """Stands in for the database, which every process shares in a real app."""

    def __init__(self) -> None:
        self.users: dict[int, User] = {}
        self.outbox: list[dict[str, Any]] = []  # events, saved in the same transaction as the data

    def add_user(self, email: str, name: str, created_at: datetime | None = None) -> User:
        user = User(len(self.users) + 1, email, name, created_at or datetime.now(UTC))
        self.users[user.id] = user
        self.outbox.append({"type": "user.created", "user_id": user.id})
        return user


class Broker:
    """Stands in for the message broker (Kafka, RabbitMQ...)."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, event: dict[str, Any]) -> None:
        self.published.append(event)


db, mailer, broker = Database(), Mailer(), Broker()
# FastAPICache's backend, set in the lifespan.
queue = FastAPITaskQueue()
scheduler = FastAPIScheduler()
leader = FastAPILeader("outbox-relay")


# Six attempts over about five minutes (10 s, 20 s, 40 s... apart, with jitter), then a dead letter.
@queue.task(name="send-welcome-email", retries=5, backoff=10)
async def send_welcome_email(user_id: int) -> None:
    """Runs at least once, so it checks first whether the e-mail already went out."""
    user = db.users.get(user_id)
    if user is None or user.welcomed:  # deleted meanwhile, or a second run
        return
    await mailer.send(user.email, f"Welcome, {user.name}!")
    user.welcomed = True
    logger.info("Welcome e-mail sent to user %d", user_id)  # an ID, not the address


@scheduler.every(hours=1, name="end-trials")
async def end_trials() -> None:
    """On the hour (UTC): the users whose trial is over go to the free plan."""
    now = datetime.now(UTC)
    for user in db.users.values():
        if user.plan == "trial" and user.created_at + TRIAL <= now:
            user.plan = "free"


@scheduler.cron("0 8 * * mon-fri", tz="Europe/Madrid", name="signup-report")
async def signup_report() -> None:
    """Weekdays at 8:00 in Madrid, in summer time and in winter time."""
    since = datetime.now(UTC) - timedelta(days=1)
    count = sum(user.created_at >= since for user in db.users.values())
    await mailer.send(REPORT_TO, f"{count} sign-ups in the last 24 hours")


@leader.run
async def relay_outbox() -> None:
    """In the leader only: two processes would publish every event twice. It's cancelled if this
    process stops leading, and starts again in the next leader."""
    while True:
        while db.outbox:
            await broker.publish(db.outbox[0])
            db.outbox.pop(0)  # once it's out: a crash in between publishes it again, never loses it
        await asyncio.sleep(RELAY_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    redis = redis_client()
    # Signed: a task planted or altered in Redis is refused and never runs.
    FastAPICache.init(RedisBackend(redis), prefix="signup", signing_key=settings.cache_signing_key)
    # Stopped in the reverse order: the jobs first, the queue last, giving its running tasks 10 s.
    async with queue, leader, scheduler:
        yield
    await redis.aclose()


app = FastAPI(lifespan=lifespan)


class SignUp(BaseModel):
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    name: str = Field(min_length=1, max_length=100)


@app.post("/users", status_code=status.HTTP_201_CREATED)
async def sign_up(signup: SignUp) -> User:
    """The response doesn't wait for the e-mail provider, nor fails while it's down."""
    user = db.add_user(signup.email, signup.name)
    # Only the ID goes through Redis: no personal data there, and the task reads the address then.
    await queue.enqueue(send_welcome_email, user.id)
    return user


@app.get("/users/{user_id}")
async def get_user(user_id: int) -> User:
    if (user := db.users.get(user_id)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return user


def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> None:
    if not secrets.compare_digest(
        credentials.credentials.encode(), settings.admin_token.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN)


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class Background(BaseModel):
    leader: bool
    pending_tasks: int
    next_runs: dict[str, datetime | None]


@admin.get("/background")
async def background() -> Background:
    """Whether this process leads, the tasks not done yet and when the jobs run next (UTC)."""
    return Background(
        leader=leader.is_leader,
        pending_tasks=await queue.pending(),
        next_runs={name: scheduler.next_run(name) for name in ("end-trials", "signup-report")},
    )


@admin.get("/dead-letters")
async def dead_letters() -> list[DeadLetter]:
    """The tasks out of retries, newest first: their name and error type, never their arguments."""
    return await queue.dead_letters()


@admin.post("/dead-letters/requeue")
async def requeue_dead_letters() -> dict[str, int]:
    """Once the cause is fixed: every dead letter back in the queue, with its attempts reset."""
    return {"requeued": await queue.requeue_dead_letters()}


@admin.put("/simulate/mailer", status_code=status.HTTP_204_NO_CONTENT)
async def simulate_mailer(down: bool) -> None:
    """Only in this example: makes the e-mail provider fail, to watch retries and dead letters."""
    mailer.available = not down


app.include_router(admin)

if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
