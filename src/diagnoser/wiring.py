"""Build the real diagnoser from config: `build_deps_from_settings()` and `create_app_from_settings()`.

WHAT: turns Settings (+ the EXECUTOR_DEV_TOKEN env var) into the production objects:
        reasoner   LLMReasoner over OpenAIChatModel (LLM_MODEL, LLM_BASE_URL, OPENAI_API_KEY)
        executor   A2AExecutorClient(EXECUTOR_URL) with a bearer-token provider
        poster     HttpxSlackPoster(SLACK_BOT_TOKEN)
        memory     InMemoryIncidentMemory (Firestore later)
      plus the FastAPI app (service.create_service) that closes every HTTP client on shutdown.
WHY:  until now only tests built the diagnoser. This is the one place where config becomes
      objects, so `python -m diagnoser` (and later the Cloud Run image) and the tests share it.
HOW:  `startup_problems()` lists EVERYTHING that's missing or unsafe at once (never the secret
      values), and `build_deps_from_settings()` refuses to build while the list isn't empty.
      Failing at startup is better than failing on the first incident or the first Slack click.

Executor auth today: only the local dev mode. EXECUTOR_DEV_TOKEN (same variable and >= 32 chars
rule as Om's `python -m executor`), sent only to a localhost EXECUTOR_URL, the client-side mirror
of the executor's "dev token only on 127.0.0.1" rule. Google ID tokens (Cloud Run -> VM) are not
built yet: they need GCP, which is on hold.

Not persistent yet: incident memory and graph checkpoints live in this process (Firestore is a
GCP item). A restart loses paused incidents; the startup log says so.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI

from approval.slack_client import HttpxSlackPoster, SlackPoster
from config import Settings
from diagnoser.a2a_client import A2AExecutorClient
from diagnoser.openai_chat import chat_model_from_settings
from diagnoser.ports import DiagnoserDeps
from diagnoser.reasoner import LLMReasoner
from diagnoser.service import create_service
from memory.store import InMemoryIncidentMemory
from obs.logging import get_logger

log = get_logger(__name__)

DEV_TOKEN_ENV = "EXECUTOR_DEV_TOKEN"  # noqa: S105 (the variable NAME, not a secret)
DEV_TOKEN_MIN_CHARS = 32  # same rule as the executor's dev mode
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class StartupError(RuntimeError):
    """Config isn't good enough to run. `problems` names each issue (never a secret value)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("diagnoser can't start:\n  - " + "\n  - ".join(problems))
        self.problems = problems


def _is_local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in LOCAL_HOSTS


def startup_problems(settings: Settings, env: Mapping[str, str]) -> list[str]:
    """Every reason the real diagnoser can't run with this config. Empty list = OK."""
    problems: list[str] = []
    # LLM
    if not settings.llm_model:
        problems.append("LLM_MODEL is not set")
    if not settings.openai_api_key and not settings.llm_base_url:
        problems.append("OPENAI_API_KEY is not set (needed unless LLM_BASE_URL is set)")
    # Incident intake and Slack approval
    if not settings.incident_api_token:
        problems.append("INCIDENT_API_TOKEN is not set (POST /incidents would refuse everything)")
    if not settings.slack_bot_token:
        problems.append("SLACK_BOT_TOKEN is not set (approval requests can't be posted)")
    if not settings.slack_signing_secret:
        problems.append("SLACK_SIGNING_SECRET is not set (every click would be refused)")
    if not settings.slack_channel_id:
        problems.append("SLACK_CHANNEL_ID is not set")
    if not settings.approver_ids:
        problems.append("APPROVER_ALLOWLIST is empty (nobody could approve a fix)")
    try:
        settings.approval_key_bytes()
    except ValueError as exc:
        problems.append(str(exc))  # the message names the variable, never the value
    # Executor auth
    dev_token = env.get(DEV_TOKEN_ENV, "")
    if not dev_token:
        problems.append(
            f"{DEV_TOKEN_ENV} is not set. Google ID tokens to a remote executor aren't "
            "implemented yet (GCP on hold); for a local executor, export the same "
            f"{DEV_TOKEN_ENV} the executor was started with"
        )
    elif len(dev_token) < DEV_TOKEN_MIN_CHARS:
        problems.append(f"{DEV_TOKEN_ENV} must be at least {DEV_TOKEN_MIN_CHARS} characters")
    elif not _is_local(settings.executor_url):
        problems.append(
            f"{DEV_TOKEN_ENV} is only sent to a localhost executor; EXECUTOR_URL points elsewhere"
        )
    return problems


@dataclass
class Built:
    """What create_service needs, plus the cleanups to run on shutdown."""

    deps: DiagnoserDeps
    poster: SlackPoster
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def _static_token(token: str) -> Callable[[], Awaitable[str]]:
    async def provider() -> str:
        return token

    return provider


def build_deps_from_settings(
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    executor_http: httpx.AsyncClient | None = None,
    llm_http: httpx.AsyncClient | None = None,
    slack_http: httpx.AsyncClient | None = None,
) -> Built:
    """The real objects for `settings`. The `*_http` clients are for tests (MockTransport /
    ASGITransport); in production leave them None and each component gets its own client.
    Raises StartupError listing every problem if the config isn't usable."""
    env = os.environ if env is None else env
    problems = startup_problems(settings, env)
    if problems:
        raise StartupError(problems)

    executor = A2AExecutorClient(
        settings.executor_url,
        token_provider=_static_token(env[DEV_TOKEN_ENV]),
        http_client=executor_http,
        max_output_bytes=settings.max_output_bytes,
    )
    chat = chat_model_from_settings(settings, http_client=llm_http)
    reasoner = LLMReasoner(
        chat,
        executor,  # the executor's Agent Card supplies the policy text for the prompt
        max_hypotheses=settings.max_hypotheses,
        max_iterations=settings.max_iterations,
        max_tool_calls=settings.max_tool_calls,
        max_replans=settings.max_replans,
    )
    bot_token = settings.slack_bot_token  # checked by startup_problems; re-checked for mypy/-O
    if bot_token is None:
        raise StartupError(["SLACK_BOT_TOKEN is not set (approval requests can't be posted)"])
    slack_client = slack_http or httpx.AsyncClient()
    poster = HttpxSlackPoster(bot_token.get_secret_value(), slack_client)
    deps = DiagnoserDeps(
        reasoner=reasoner,
        executor=executor,
        memory=InMemoryIncidentMemory(),
        settings=settings,
    )
    log.info(
        "diagnoser built",
        extra={
            "executor_url": settings.executor_url,
            "llm_model": settings.llm_model,
            "llm_base_url": settings.llm_base_url or "openai",
            "prompts": reasoner.prompt_refs,
            "memory": "in-process (not persistent)",
            "checkpoints": "in-process (paused incidents are lost on restart)",
        },
    )
    return Built(deps, poster, closers=[executor.aclose, chat.aclose, slack_client.aclose])


def create_app_from_settings(settings: Settings, **kwargs) -> FastAPI:
    """The full diagnoser app for `settings`; closes its HTTP clients on shutdown."""
    built = build_deps_from_settings(settings, **kwargs)
    return create_service(built.deps, built.poster, on_shutdown=built.closers)
