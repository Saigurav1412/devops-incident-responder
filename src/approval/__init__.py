"""Slack approval surface: interaction webhook, approver allowlist, token minting. Owner: Sai. See docs/INTERFACES.md §4.

Layout:
  slack_signature.py  verify a request came from Slack (signing secret, 5-minute replay window)
  messages.py         the Block Kit approval message; buttons carry only incident_id + action_id
  handler.py          parse a click, run the checks, mint the token, resume the paused graph

Not built yet: posting the message (chat.postMessage), the HTTP endpoint, the 3-second Slack ack,
and escalating approvals that expire without a click.

Shared shapes come from `contracts`; never redefine them here.
"""
