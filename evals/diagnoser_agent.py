"""The real diagnoser behind the harness's `Agent` protocol (INTERFACES §1.1, §5).

WHAT: `DiagnoserAgent` runs Sai's whole diagnoser app (`diagnoser.service.create_service`) in this
      process and drives it the way real callers do, over HTTP:
        1. POST /incidents with the Alert and the bearer token             (like the monitor)
        2. poll GET /incidents/{id}                                        (like the monitor)
        3. when it waits for approval, read the approval message the app
           "posted to Slack" and click Approve with a Slack-signed request (like a human)
        4. stop when `outcome` is no longer "open"; report what the agent claims.
      The harness, not this class, decides whether the incident was resolved (oracle).

WHY in-process instead of a separate diagnoser server: the service has no module-level app yet
      (deps are injected), and in-process lets the harness swap the reasoner per run. Everything
      on the incident path is still real: intake auth, the LangGraph graph, Slack signature
      checking, the approval handler (the ONLY place that mints tokens, AGENTS.md rule 4), the
      A2A client, and the executor server with its allowlist, gates and token check. The only
      fake is the network call to Slack (`CapturingSlackPoster`).

Reasoners (one per episode, built by a factory):
  * `FaultScriptedReasoner` reads the fault's expected fix, like the `scripted` baseline. With
    it, `diagnoser-scripted` is the upper bound THROUGH THE REAL GRAPH: if it doesn't resolve a
    fault, the problem is in the pipeline (approval, A2A, health check timing), not the LLM.
  * The LLM reasoner (PR #14 + a provider adapter) plugs into the same factory later; it must
    ignore `fault` and see only the alert.

HOW the approver works: it gets the button value (incident_id + action_id) from the captured
Slack message, builds Slack's form-encoded `payload=` body, signs it with the signing secret
(`approval.slack_signature.compute_signature`) and POSTs it to /slack/interactions. The handler
then re-checks the approver allowlist, the current proposal and the TTL, and mints the token from
the checkpointed state, exactly as for a real click.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from approval.messages import APPROVE_ACTION
from approval.slack_signature import compute_signature
from approval.web import SLACK_INTERACTIONS_PATH
from config import Settings
from contracts import Alert, Hypothesis, IncidentState
from diagnoser.graph import thread_config
from diagnoser.ports import DiagnoserDeps, ExecutorClient, FixProposal, Reasoned, Reasoner
from diagnoser.service import create_service
from evals.agents import AgentReport
from evals.faults import Fault
from memory.store import IncidentMemory, InMemoryIncidentMemory
from obs.logging import get_logger

log = get_logger(__name__)

# The Slack user id the harness clicks as. Added to APPROVER_ALLOWLIST in harness_settings();
# Slack user ids are U + uppercase alphanumerics, so this can't collide with a real person.
HARNESS_APPROVER = "UEVALHARNESS"
HARNESS_CHANNEL = "CEVALHARNESS"

# A cheap read-only diagnostic the scripted reasoner runs once, so the diagnose -> A2A ->
# allowlist -> runner path is exercised on every episode (it's in allowlist.yaml).
SCRIPTED_DIAGNOSTIC = ["docker", "ps", "-a"]


# ---------------------------------------------------------------------------------------------
# Reasoner that knows the answer (baseline only)
# ---------------------------------------------------------------------------------------------


class FaultScriptedReasoner:
    """A Reasoner that cheats: it reads the fault's `expected_fix` / `expected_params`.

    It still reacts to the state like a real reasoner would, so the graph's loops behave
    normally: on a replan (the health check refuted the fix) it plans a fresh hypothesis and
    proposes the same fix again, which shows up as `replans > 0` in the episode record.
    """

    def __init__(self, fault: Fault) -> None:
        self.fault = fault

    async def plan(self, state: IncidentState) -> Reasoned[list[Hypothesis]]:
        statement = f"{self.fault.id}: fixed by {self.fault.spec.expected_fix}"
        return Reasoned([Hypothesis(id="new1", statement=statement)])

    async def next_command(self, state: IncidentState) -> Reasoned[list[str] | None]:
        # One diagnostic per hypothesis round, then "nothing more to check".
        open_ids = {h.id for h in state["hypotheses"] if h.status == "open"}
        already = sum(1 for o in state["observations"] if o.skill == "diagnose")
        return Reasoned(list(SCRIPTED_DIAGNOSTIC) if already < len(open_ids) else None)

    async def assess(self, state: IncidentState) -> Reasoned[list[Hypothesis]]:
        return Reasoned(
            [
                h.model_copy(update={"status": "confirmed", "confidence": 0.9})
                for h in state["hypotheses"]
                if h.status == "open"
            ][:1]
        )

    async def propose(self, state: IncidentState) -> Reasoned[FixProposal]:
        return Reasoned(
            FixProposal(
                action=self.fault.spec.expected_fix,
                params=dict(self.fault.spec.expected_params),
                rationale=f"scripted baseline for {self.fault.id}",
            )
        )


# ---------------------------------------------------------------------------------------------
# Slack stand-ins
# ---------------------------------------------------------------------------------------------


class CapturingSlackPoster:
    """SlackPoster that keeps messages instead of calling chat.postMessage."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def post_message(self, channel: str, message: dict[str, Any]) -> None:
        self.messages.append((channel, message))


def approve_button_values(message: dict[str, Any]) -> list[dict[str, str]]:
    """The Approve button values in one Slack message (searched recursively, so the exact block
    layout in approval/messages.py can change without breaking the harness)."""
    found: list[dict[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("action_id") == APPROVE_ACTION and isinstance(node.get("value"), str):
                found.append(json.loads(node["value"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(message)
    return found


def signed_click(
    signing_secret: str, value: dict[str, str], *, user_id: str, now: float
) -> tuple[bytes, dict[str, str]]:
    """Body + headers for an Approve click, as Slack sends it (form-encoded `payload=<json>`)."""
    payload = {
        "type": "block_actions",
        "user": {"id": user_id},
        "actions": [{"action_id": APPROVE_ACTION, "value": json.dumps(value, sort_keys=True)}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = str(int(now))
    return body, {
        "content-type": "application/x-www-form-urlencoded",
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": compute_signature(signing_secret, timestamp, body),
    }


# ---------------------------------------------------------------------------------------------
# Settings for a harness-run diagnoser
# ---------------------------------------------------------------------------------------------


def harness_settings(base: Settings) -> Settings:
    """`base` plus fresh per-process secrets for the parts the harness plays itself.

    The intake token and Slack signing secret only travel inside this process, so they are
    random each run (never from .env, never logged). The approval key is NOT replaced: the
    approval handler mints with it and the executor (a separate process) verifies with it, so
    both must read the same APPROVAL_SIGNING_KEY from .env.
    """
    base.approval_key_bytes()  # raises ValueError with a clear message if the key is unusable
    approvers = ",".join(sorted(base.approver_ids | {HARNESS_APPROVER}))
    # model_validate (not model_copy) so the overrides go through the same field validation.
    return Settings.model_validate(
        {
            **base.model_dump(),
            "incident_api_token": SecretStr(secrets.token_urlsafe(32)),
            "slack_signing_secret": SecretStr(secrets.token_urlsafe(32)),
            "slack_channel_id": HARNESS_CHANNEL,
            "approver_allowlist": approvers,
        }
    )


# ---------------------------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------------------------


class ApprovalClickIgnoredError(RuntimeError):
    """The harness clicked Approve but the incident is still waiting on that proposal.

    The episode is then recorded as `error` (the harness catches agent exceptions), which is
    honest: the pipeline under test broke, the agent didn't fail at diagnosis."""


class DiagnoserAgent:
    """Drive one incident through the real diagnoser service. See the module docstring."""

    def __init__(
        self,
        *,
        name: str,
        settings: Settings,
        executor: ExecutorClient,
        reasoner_factory: Callable[[Fault], Reasoner],
        memory: IncidentMemory | None = None,
        poll_s: float = 0.5,
        click_ignored_after_s: float = 10.0,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self.settings = settings
        self.executor = executor  # one A2A client for the whole run (fetches the card once)
        self.reasoner_factory = reasoner_factory
        # Shared across episodes on purpose: memory is what's supposed to help on repeats.
        # The `memory=false` ablation gets a fresh, empty store every episode instead.
        # `is None`, not `or`: an empty InMemoryIncidentMemory has __len__ == 0, so it is falsy,
        # and `memory or ...` would silently swap the caller's store for a new one.
        self.memory = memory if memory is not None else InMemoryIncidentMemory()
        self.poll_s = poll_s
        self.click_ignored_after_s = click_ignored_after_s
        self.wall_clock = wall_clock

    async def handle(self, alert: Alert, fault: Fault, config: dict) -> AgentReport:
        # Ablation switches (INTERFACES §5), recorded with every episode by the harness:
        #   memory=false      a fresh, empty store for this episode
        #   max_hypotheses=N  how many hypotheses the graph keeps per plan (Settings bounds 1-10)
        memory = self.memory if config.get("memory", True) else InMemoryIncidentMemory()
        settings = self.settings
        if "max_hypotheses" in config:
            settings = Settings.model_validate(
                {**settings.model_dump(), "max_hypotheses": config["max_hypotheses"]}
            )
        deps = DiagnoserDeps(
            reasoner=self.reasoner_factory(fault),
            executor=self.executor,
            memory=memory,
            settings=settings,
        )
        poster = CapturingSlackPoster()
        app = create_service(deps, poster)
        token = self.settings.incident_api_token.get_secret_value()  # set by harness_settings
        secret = self.settings.slack_signing_secret.get_secret_value()
        auth = {"Authorization": f"Bearer {token}"}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://diagnoser") as http:
            r = await http.post("/incidents", json=alert.model_dump(mode="json"), headers=auth)
            r.raise_for_status()
            incident_id = r.json()["incident_id"]
            ctx = {"incident_id": incident_id, "fault_id": fault.id}

            clicked: dict[str, float] = {}  # action_id -> when the harness clicked it
            approval_wait = 0.0
            waiting_since: float | None = None
            while True:
                s = await http.get(f"/incidents/{incident_id}", headers=auth)
                s.raise_for_status()
                status = s.json()
                if status["outcome"] != "open":
                    break
                if status["waiting_for_approval"]:
                    waiting_since = waiting_since or time.monotonic()
                    pending = [
                        v
                        for _, m in poster.messages
                        for v in approve_button_values(m)
                        if v["incident_id"] == incident_id and v["action_id"] not in clicked
                    ]
                    # The proposal we already approved is still the one waiting: the handler
                    # ignored our click (approver not allowed, stale, past the TTL). Say so
                    # instead of polling until the episode times out as a misleading "timeout".
                    stuck = [
                        a
                        for a, at in clicked.items()
                        if time.monotonic() - at > self.click_ignored_after_s
                    ]
                    if not pending and stuck:
                        raise ApprovalClickIgnoredError(
                            f"approval click for {stuck[-1]} was not accepted "
                            "(see the 'slack interaction processed' log line for the reason)"
                        )
                    if pending:
                        value = pending[-1]  # newest proposal; older ones are stale by design
                        body, headers = signed_click(
                            secret, value, user_id=HARNESS_APPROVER, now=self.wall_clock()
                        )
                        c = await http.post(SLACK_INTERACTIONS_PATH, content=body, headers=headers)
                        c.raise_for_status()
                        clicked[value["action_id"]] = time.monotonic()
                        approval_wait += time.monotonic() - waiting_since
                        waiting_since = None
                        log.info("harness approved", extra={**ctx, "action_id": value["action_id"]})
                        continue
                await asyncio.sleep(self.poll_s)

            # Counts the status API deliberately doesn't expose (raw observations stay
            # in-process). Read straight from the checkpoint: the harness is trusted here.
            snapshot = await app.state.service.graph.aget_state(thread_config(incident_id))
            observations = snapshot.values.get("observations", [])

        budget = status["budget"]
        return AgentReport(
            outcome=status["outcome"],
            replans=budget["replans"],
            tool_calls=budget["tool_calls"],
            # The graph keeps one token total (Budget.tokens); there's no in/out split yet.
            # Recorded as tokens_in so totals are right; cost is left at 0 until it's split.
            tokens_in=budget["tokens"],
            blocked_actions=sum(1 for o in observations if o.state == "rejected"),
            approval_wait_s=approval_wait,
        )
