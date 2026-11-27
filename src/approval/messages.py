"""Build the Slack approval message for a paused incident.

WHAT: turns the graph's interrupt payload (`{"kind": "approval_request", "incident_id",
      "proposed": ProposedAction as JSON}`, INTERFACES §1) into a Slack Block Kit message with an
      Approve and a Reject button.
WHY it's careful about what goes where:
  * `rationale` and `params` values were written by the LLM, which may have read attacker-controlled
    log lines. They are shown ONLY in `plain_text` objects (Slack applies no mrkdwn formatting to
    those, so the text can't make itself bold, add links, or look like a system message) and are
    length-capped. They never go into the notification `text` fallback, which Slack does format.
  * The button `value` carries ONLY incident_id and action_id. The handler looks the action and
    params up in the graph's checkpointed state; it never trusts an action/params string that
    came back from Slack (INTERFACES §4.2, my note on PR #2).
  * Approve has a Slack confirm dialog, so a mis-click doesn't run a fix.
"""

from __future__ import annotations

import json
from typing import Any

from contracts import ProposedAction

# Slack action_ids for the two buttons; the handler maps them to ApprovalDecision.decision.
APPROVE_ACTION = "approve"
REJECT_ACTION = "reject"
APPROVAL_BLOCK_ID = "incident_approval"

# Slack's limit for a section's text is 3000 chars; keep well under and say it was cut.
_MAX_TEXT = 1500


def _clip(text: str, limit: int = _MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 15] + " …[truncated]"


def _plain(text: str) -> dict[str, Any]:
    """A plain_text object. `emoji: False` so ':rotating_light:' in LLM text stays literal."""
    return {"type": "plain_text", "text": _clip(text) or " ", "emoji": False}


def button_value(incident_id: str, action_id: str) -> str:
    """What the buttons carry back. IDs only; see module docstring."""
    return json.dumps({"incident_id": incident_id, "action_id": action_id}, sort_keys=True)


def approval_message(incident_id: str, proposed: ProposedAction) -> dict[str, Any]:
    """Arguments for Slack's chat.postMessage (minus `channel`) asking a human to decide."""
    params = "\n".join(f"  {k} = {v}" for k, v in sorted(proposed.params.items())) or "  (none)"
    details = (
        f"Action: {proposed.action}\n"
        f"Params:\n{params}\n"
        f"Rollback: {proposed.rollback or 'none'}\n"
        f"Incident: {incident_id}\n"
        f"Proposal: {proposed.action_id}"
    )
    value = button_value(incident_id, proposed.action_id)
    return {
        # Notification / fallback text. Built only from constrained identifiers (catalog action
        # name, uuid incident id), never from LLM prose.
        "text": f"Approval needed: {proposed.action} for incident {incident_id}",
        "blocks": [
            {"type": "header", "text": _plain("Fix proposed — approval needed")},
            {"type": "section", "text": _plain(details)},
            {
                "type": "section",
                # Labelled, so a reader knows this paragraph is the model's claim, not a fact.
                "text": _plain(f"Agent's rationale (unverified):\n{proposed.rationale}"),
            },
            {
                "type": "actions",
                "block_id": APPROVAL_BLOCK_ID,
                "elements": [
                    {
                        "type": "button",
                        "action_id": APPROVE_ACTION,
                        "style": "primary",
                        "text": _plain("Approve"),
                        "value": value,
                        "confirm": {
                            "title": _plain("Run this fix?"),
                            "text": _plain(f"{proposed.action} will change the system."),
                            "confirm": _plain("Approve"),
                            "deny": _plain("Cancel"),
                        },
                    },
                    {
                        "type": "button",
                        "action_id": REJECT_ACTION,
                        "style": "danger",
                        "text": _plain("Reject"),
                        "value": value,
                    },
                ],
            },
        ],
    }
