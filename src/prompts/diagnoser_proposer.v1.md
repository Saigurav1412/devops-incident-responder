# Task: propose one fix

Propose ONE fix from the policy's fix list for the confirmed hypothesis. Use the action name and the
parameter names and values exactly as the policy allows them, with compose SERVICE names. Prefer the
smallest fix that addresses the confirmed cause. Do not propose anything listed as not available.

`rationale` is shown to the human who approves the fix: one or two plain sentences linking the
evidence to the fix. `rollback` is the name of a policy action that undoes it, or null.

Schema:
{{"action": "string", "params": [{{"name": "string", "value": "string"}}, ...], "rationale": "string", "rollback": "string" | null}}
