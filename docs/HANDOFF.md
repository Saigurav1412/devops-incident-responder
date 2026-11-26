# Handoff Protocol

Why this exists: in earlier assignments, end-of-session summaries left out exactly what later broke
(edge cases, unverified assumptions, the reasons behind a choice). A summary can't be trusted to be
complete, so this protocol does two things: it forces the author to list what they are **not** sure
about, and it makes the reader **verify against the code** instead of trusting the note.

Any coding agent can follow this file. Claude Code users can run `/handoff`, which follows it.

---

## Part A — Writing a handoff (end of every session that changed code or decisions)

### 1. Put durable knowledge where it belongs first

A handoff note is the last place knowledge should live. Before writing it:

| If the session produced… | It goes in… |
|---|---|
| A changed data shape, payload, env var, or rule between slices | `docs/INTERFACES.md` **and** `src/contracts/` (same commit) + bump `CONTRACT_VERSION` |
| A design choice and its reason | `docs/DECISIONS.md` (one row) |
| A rule that must always hold (guardrail, limit) | a test in `tests/` that fails if it's broken |
| A new command to set up/run/test | `AGENTS.md` → Commands |
| A new env var | `.env.example` + INTERFACES §6 |

If something in the note below could have gone in one of these places instead, move it there.

### 2. Write the note

Create `handoffs/YYYY-MM-DD-HHMM-<name>.md` (one new file per session, so two people never edit the
same file and never get a merge conflict). Use exactly these headings. "None" is a valid answer;
leaving a heading out is not.

```markdown
# Handoff — <name> — <YYYY-MM-DD HH:MM> — branch <branch>

## Goal of this session
One or two sentences.

## Changed
- `path/to/file.py` — what changed and why (one line each). Include deleted/renamed files.

## Verified (and how)
- <claim> — <how it was checked: test name, command + output, trace URL, manual step>

## NOT verified / assumed
- <every assumption that wasn't checked, every "should work", every mocked dependency>

## Known broken / skipped
- <failing tests, TODOs left in code with file:line, features stubbed>

## Contracts and decisions touched
- INTERFACES.md sections: <§ numbers or "none">  · CONTRACT_VERSION: <old → new or "unchanged">
- DECISIONS.md rows added: <or "none">

## Next step
The single next thing to do, specific enough to start without asking.

## Open questions for the other person
- <@name: question> (or "none")
```

### 3. Self-check before committing

- [ ] Every file in `git diff --stat` for this session appears under **Changed**
- [ ] Every claim under **Verified** names how it was checked; anything without a "how" moves to **NOT verified**
- [ ] `uv run pytest` was run; its result is stated under Verified or Known broken
- [ ] If `src/contracts/` changed, `docs/INTERFACES.md` changed in the same commit (and vice versa)
- [ ] No secrets, tokens or `.env` values in the note

### 4. Commit and push

Commit the note with the code it describes. If a PR is open for the branch, paste the note into the
PR description too.

Also append a few lines to your own `notes/<name>.md` (what AI tools generated, how you checked it).
That file feeds your individual PROCESS_LOG; the handoff note does not.

---

## Part B — Reading a handoff (start of every session)

1. `git pull`. Read `AGENTS.md`, `docs/INTERFACES.md`, `docs/PHASES.md`, `docs/DECISIONS.md`.
2. Read the newest handoffs that touch the area you're about to work in
   (`ls -t handoffs/ | head`).
3. **Treat every statement in a handoff as a claim, not a fact.** Before building on one:
   - check it against the code (open the file, read the function),
   - re-run the check it lists under **Verified** if your work depends on it,
   - treat everything under **NOT verified** as unknown until you test it.
4. If the code disagrees with a handoff, the code wins. Note the mismatch in your own handoff under
   **Open questions** so the author sees it.
5. If a contract test fails after pulling, stop and resolve it before writing new code.
