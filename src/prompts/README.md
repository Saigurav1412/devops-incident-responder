# Prompts

One versioned file per agent role. Prompts are files, not inline strings, so a prompt change shows
up as a reviewable diff and each eval run can record which version it used. Owner: Sai.

| File | Used by | Placeholders |
|---|---|---|
| `diagnoser_common.v1.md` | every diagnoser call (system message) | `{diagnose_policy}`, `{remediate_policy}` |
| `diagnoser_planner.v1.md` | `Reasoner.plan` | `{max_hypotheses}` |
| `diagnoser_commander.v1.md` | `Reasoner.next_command` | none |
| `diagnoser_assessor.v1.md` | `Reasoner.assess` | none |
| `diagnoser_proposer.v1.md` | `Reasoner.propose` | none |

Rules:
- Placeholders use `str.format`, so a literal `{` or `}` is written `{{` / `}}`.
- Never edit a released version in place: copy to `.v2.md`, change it, and switch the version in
  `diagnoser/reasoner.py`. `prompts.load()` returns a short sha256 so traces and eval results name
  the exact text that ran.
- The prompt is not a guardrail (AGENTS.md rule 1). Everything it asks for is also enforced in code:
  output schemas in `diagnoser/reasoner.py`, the merge rules in `apply_assessment`, the executor's
  allowlist and approval token.
