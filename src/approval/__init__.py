"""Slack approval surface: interaction webhook, approver allowlist, token minting. Owner: Sai. See docs/INTERFACES.md §4.

Layout:
  slack_signature.py  verify a request came from Slack (signing secret, 5-minute replay window)
  messages.py         the Block Kit approval message; buttons carry only incident_id + action_id
  handler.py          parse a click, run the checks, mint the token, resume the paused graph
  web.py              FastAPI POST /slack/interactions: verify, ack within 3 s, resume in background
  slack_client.py     post the approval message (chat.postMessage) behind a SlackPoster interface

Not built yet: escalating approvals that expire without a click, updating the Slack message after
a decision, and locking across Cloud Run instances.

Shared shapes come from `contracts`; never redefine them here.
"""
