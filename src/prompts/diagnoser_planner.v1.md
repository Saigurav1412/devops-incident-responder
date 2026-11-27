# Task: plan hypotheses

Propose up to {max_hypotheses} hypotheses that could explain the alert, most likely first. Each one is a
single, specific, testable statement about the cause ("the api container's DATABASE_URL points at the
wrong host"), not a symptom ("the api is down"). For each, list the evidence that would confirm or
refute it, phrased as things a read-only command from the policy can show.

Hypotheses marked `refuted` were already ruled out by evidence. Do not propose them again, reworded or
not. If past incidents are given, you may turn their root cause into a hypothesis, but only if it fits
this alert.

If nothing in the input supports any hypothesis you can test with the allowed commands, return an
empty list: a human will take over.

Schema:
{{"hypotheses": [{{"statement": "string", "evidence_needed": ["string", ...]}}, ...]}}
