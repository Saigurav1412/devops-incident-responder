---
description: End-of-session handoff — update docs/contracts, write a handoff note, self-check, commit
---

Follow `docs/HANDOFF.md` Part A exactly. Steps:

1. Run `git status`, `git diff --stat`, and `git log --oneline origin/main..HEAD` to see everything
   this session changed. Read the diffs; don't rely on memory of the session.
2. Part A §1: move durable knowledge into `docs/INTERFACES.md` + `src/contracts/`,
   `docs/DECISIONS.md`, tests, `AGENTS.md` or `.env.example` as the table says. Show me those edits
   before continuing.
3. Run `uv run pytest` and record the actual result.
4. Get my name from `git config user.name` (lowercase first name) and the time from `date`.
   Write `handoffs/YYYY-MM-DD-HHMM-<name>.md` with every heading from Part A §2.
   - Under **Verified**, list only things you actually checked in this session, each with how.
   - Put everything else you believe but did not check under **NOT verified / assumed**. When unsure
     which section something belongs in, it goes under NOT verified.
5. Run the Part A §3 self-check and print each item with pass/fail. Fix any failure.
6. Append 3–6 lines to `notes/<name>.md`: which code was AI-generated this session, how it was
   verified, what went wrong.
7. Show me the note and the list of files to commit. Commit and push only after I confirm.
   If a PR is open for this branch (`gh pr view`), offer to paste the note into its description.

$ARGUMENTS
