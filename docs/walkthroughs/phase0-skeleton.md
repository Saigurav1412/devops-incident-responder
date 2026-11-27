# Walkthrough — Phase 0: repo skeleton, shared contracts, CI

**Built:** 2026-11-23 (Claude, cloud session, for Om) · **Branch:** `chore/skeleton-ci` · **Status:** tests + lint pass locally; CI result on the PR.

Read this once now, again after you've opened the files. Each section says what the piece is, why
it exists, and where to look in the code. The "Study" section at the end has docs to read alongside.

---

## 1. The big picture: what this PR adds

```
pyproject.toml          one uv project for the whole repo (deps, packaging, pytest, ruff)
uv.lock                 exact versions; both laptops and CI install the same thing
.python-version         3.12 (uv installs it for you)
.github/workflows/ci.yml  lint + tests on every PR
src/contracts/          the shared shapes from docs/INTERFACES.md, as code
  models.py             Pydantic models (§1–§5)
  approval_token.py     mint/verify the approval HMAC (§4.3), the main guardrail
  a2a_codec.py          Pydantic <-> A2A v1.0 protobuf Part
src/config.py           typed env config (§6)
src/obs/logging.py      JSON logs for Cloud Logging
src/<slice>/__init__.py empty packages for diagnoser, executor, security, memory, approval
tests/                  42 tests, no network needed
```

Nothing here talks to an LLM, Docker, GCP or Slack yet. This PR is the **ground both of you build on**:
once it's merged, Sai's diagnoser and your executor import the same `RemediateRequest`, so they can't
disagree about its fields.

## 2. Why contracts are code, not just a doc

In A2, handoff notes and docs drifted from the code and caused bugs. Here, `docs/INTERFACES.md` says
what a `RemediateRequest` is, and `src/contracts/models.py` *is* that model. Both services import it.
If Sai renames a field, your executor's tests fail in CI before the change is merged. That's the
point: a mismatch becomes a red X on a PR instead of a runtime failure during the demo.

**Look at:** `src/contracts/models.py`, top docstring, then `DiagnoseRequest` and `RemediateRequest`.

Three choices worth understanding there:

1. **`extra="forbid"`** (the `_Strict` base class). Pydantic ignores unknown fields by default. For
   anything the executor receives, an unknown field like `"shell": true` should be an error, because
   the diagnoser's output is partly written by an LLM that an attacker may have influenced through a
   log line. Test: `tests/test_contracts.py::test_extra_fields_are_rejected`.
2. **`Identifier`** type: ids are limited to `[A-Za-z0-9._:-]`, max 128 chars. An id ends up in log
   lines, file names, Firestore paths and the signed approval message; restricting it removes a whole
   class of injection/path tricks (`../etc/passwd`, `id;rm`). Test: `test_identifiers_are_restricted`.
3. **Discriminated union** `ExecutorRequest = DiagnoseRequest | RemediateRequest` keyed on `skill`.
   The executor gets a dict from the A2A data part and needs exactly one model. With a discriminator,
   Pydantic looks at `skill` first and validates against only that model, so errors are clear and an
   unknown skill (`"shell"`) fails outright. Test: `test_executor_request_dispatches_on_skill`.

What models deliberately **don't** do: decide whether `docker rm -f db` is allowed. That's policy, and
it lives in `src/security/` (your Phase 1 work). Shape here, policy there, so tightening a rule never
needs a contract change.

## 3. The approval token, step by step

**Look at:** `src/contracts/approval_token.py`, then `tests/test_approval_token.py`.

The problem it solves: a prompt-injected diagnoser gets a harmless action approved in Slack
(`restart_service api`), then sends the executor something else (`prune_path /`) with the same
`action_id`. The approval click must cover the *exact* request, not just an id.

How:

1. When a human clicks Approve, Sai's Slack handler calls `mint(key, incident_id, action_id, action,
   params, ttl_s)`. It builds a message from all of those fields plus an expiry time and computes
   HMAC-SHA256 over it with a secret key only the two services know. Token = `"<expiry>.<hex tag>"`.
2. The executor receives a `RemediateRequest` carrying that token. It calls `verify(...)` with the
   fields **from the request it actually received**. If anything changed (action, any param,
   incident, action id, expiry), the recomputed HMAC differs and `verify` raises `approval_invalid`.
3. `hmac.compare_digest` compares in constant time, so response timing doesn't leak how close a
   forged tag was.
4. The signature is checked **before** the expiry, so a forged token always says `approval_invalid`,
   never `approval_expired` (which would confirm the signature was right).

Details that prevent subtle bugs:

- `canonical_json(params)` sorts keys: `{"a":1,"b":2}` and `{"b":2,"a":1}` sign the same. Test:
  `test_param_order_does_not_matter`.
- Fields are joined with byte `0x1F`, which can't appear in an `Identifier`, so `("ab","c")` and
  `("a","bc")` can't produce the same message.
- `"approval-v1"` at the start ("domain separation") means a signature made with this key can never
  be valid for some other purpose that uses the same key.
- Keys shorter than 32 bytes are refused.

Why it's in `contracts/` and not `security/`: the minting side (Sai) and verifying side (you) must
build byte-identical messages. One shared function is the only way to guarantee that.

What it does **not** cover: who may approve (Sai's `approval/` checks `APPROVER_ALLOWLIST`), and
replaying the same approved action twice (your executor's `idempotency_key` check, Phase 1).

## 4. The A2A codec, and why it's needed

**Look at:** `src/contracts/a2a_codec.py`, then `tests/test_a2a_codec.py`.

`a2a-sdk` 1.x (the official SDK for A2A v1.0) defines `Message`, `Part`, `Task` as **protobuf**
classes, not Pydantic. Our contracts are Pydantic. The codec converts between them:

```
DiagnoseRequest --model_dump(mode="json")--> dict --ParseDict--> protobuf Value --> Part(data=..., media_type="application/json")
Part --MessageToDict--> dict --DiagnoseRequest.model_validate--> DiagnoseRequest
```

v1.0 detail: a `Part` has no `kind` field anymore. It's a protobuf `oneof`: it carries exactly one of
`text`, `data`, `url`, `raw`. `part.WhichOneof("content")` tells you which. The codec refuses anything
that isn't a `data` part, so a `text` part saying "ignore previous instructions" never reaches model
validation. Test: `test_text_part_is_refused`.

`test_round_trip_through_a2a_message` builds a real v1.0 `Message`, serializes it to bytes (as the
network would), parses it back and checks the model is unchanged. That's the proof the codec works
with the actual SDK, not just with our assumptions about it.

## 5. Config and logging

- `src/config.py`: every env var from INTERFACES §6 as a typed field. Budget caps have bounds
  (`MAX_TOOL_CALLS=0` fails at startup) so a bad env value can't silently disable a guardrail. Secrets
  are `SecretStr` and print as `**********`. Test: `test_budget_caps_cannot_be_disabled`.
- `src/obs/logging.py`: one JSON object per log line with a `severity` field, which is what Google
  Cloud Logging reads. `log.info("msg", extra={"incident_id": ...})` adds searchable fields.

## 6. Packaging and tooling choices

- **One uv project, top-level packages under `src/`.** Code imports `from contracts import ...`. The
  `[tool.hatch.build.targets.wheel] packages` list in `pyproject.toml` declares them; `pythonpath =
  ["src"]` lets pytest import them directly.
- **`uv.lock` is committed.** `uv sync --locked` in CI fails if someone edits `pyproject.toml` without
  re-locking, so "works on my machine because I have a newer version" can't happen.
- **Ruff** lints and formats. The `S` rules are security checks (e.g. `subprocess(..., shell=True)` is
  flagged), useful for the executor. Markdown is excluded from formatting so ruff doesn't re-indent
  the code samples in `INTERFACES.md`.
- **CI** (`.github/workflows/ci.yml`): checkout → Python from `.python-version` → `uv sync --locked`
  → `ruff check` → `ruff format --check` → `pytest`. No secrets needed.

## 7. How to check it yourself (5 minutes)

```bash
cd ~/CSCI-599/Capstone_Project
git fetch origin && git switch chore/skeleton-ci
uv sync
uv run pytest -v          # expect 42 passed
uv run ruff check .       # expect "All checks passed!"
```

Then try breaking something on purpose, e.g. in `approval_token.py` change `sort_keys=True` to
`False` and run `uv run pytest tests/test_approval_token.py`. You should see
`test_param_order_does_not_matter` fail. That's the test doing its job.

## 8. Study (all links checked 2026-11-23)

| Topic | Read | Then look at |
|---|---|---|
| A2A v1.0 changes from v0.3 | [What's new in v1.0](https://raw.githubusercontent.com/a2aproject/A2A/main/docs/whats-new-v1.md) | `docs/INTERFACES.md` §2 table |
| A2A spec (operations, Task, states) | [A2A specification](https://a2a-protocol.org/latest/specification/), [protocol definitions (.proto)](https://a2a-protocol.org/latest/definitions/) | `src/contracts/a2a_codec.py` |
| A2A Python SDK | [a2a-sdk on PyPI](https://pypi.org/project/a2a-sdk/) | `pyproject.toml` dependency pin |
| Discriminated unions | [Pydantic: Unions → Discriminated unions](https://docs.pydantic.dev/latest/concepts/unions/) | `ExecutorRequest` in `models.py` |
| Env config + secrets | [Pydantic Settings Management](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | `src/config.py` |
| HMAC + constant-time compare | [Python `hmac` docs](https://docs.python.org/3/library/hmac.html) | `src/contracts/approval_token.py` |
| uv lockfile + `--locked` | [uv: Locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/), [uv in GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/) | `.github/workflows/ci.yml` |

## 9. Open questions this PR leaves for review

- **Sai:** the approval-token message format (§4.3) is now concrete. Sai's Slack handler will call
  `mint`; check the signature works for how the handler will get `params`.
- **Both:** `Observation` (§1) wasn't specified field-by-field in INTERFACES; the shape in
  `models.py` is a proposal.
- The project uses top-level packages (`contracts`, `executor`, ...) rather than one
  `incident_responder.*` namespace, to match the AGENTS.md layout. Generic names like `config` could
  clash with a third-party module; if that ever happens, move everything under one namespace.
