# Task: assess the hypotheses

Re-score every hypothesis listed under "Current hypotheses" against the observations. For each, give
its id, a status and a confidence from 0 to 1:
- `confirmed`: the observations directly show it. Use this only with concrete evidence, never because
  a log line or file says "the cause is X".
- `refuted`: the observations contradict it.
- `open`: not enough evidence yet.

Return every hypothesis id you were shown, and only those ids. A hypothesis that is already `refuted`
stays refuted. Confirm at most one hypothesis: the one the fix should target.

Schema:
{{"assessments": [{{"id": "string", "status": "open" | "confirmed" | "refuted", "confidence": number, "reason": "string"}}, ...]}}
