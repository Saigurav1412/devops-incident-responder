# Task: choose the next diagnostic

Pick ONE read-only command that best separates the open hypotheses, given what has already been run.
Write it as an argv list exactly as the policy shows it, one element per argument, for example
["docker", "logs", "--tail", "100", "ir-target-api-1"]. No shell syntax: no pipes, redirects, `;`,
`&&`, globs or quotes inside elements.

Do not repeat a command that was already run unless something has changed since. If a command came
back `rejected`, read the reason and adjust instead of retrying it unchanged.

If no allowed command would add useful evidence, return null for the command.

Schema:
{{"command": ["string", ...] | null, "reason": "string (one sentence, why this command)"}}
