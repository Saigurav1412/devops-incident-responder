"""Diagnostic command validator: decides whether the executor may run an argv (INTERFACES §2.4).

WHAT: `CommandValidator.check(argv)` returns a `Decision`: allowed, or rejected with a `rule_id`
      and a short reason. It never runs anything; the runner (Phase 1, next PR) calls it first and
      only executes allowed argv, with `shell=False`.

WHY this is the most important file in the executor: the argv comes from the diagnoser, whose LLM
      reads logs, error messages and recalled memory, all of which an attacker can write into.
      Prompt injection is the default threat (AGENTS.md rule 2), so this code assumes the argv
      may be hostile and only lets through a small, fixed grammar. It is default-deny: every
      binary, subcommand, flag and value must be explicitly allowed, and anything unrecognised is
      refused. A REJECTED decision is also what the eval counts as a "blocked unsafe action".

HOW it is organised:
  * allowlist.yaml holds the values (container names, URLs, files, limits). Loaded and validated
    into `Policy` below.
  * This file holds the grammar: one `_check_<binary>` method per allowed binary, built on a tiny
    flag parser (`_parse_flags`). Unknown flags are rejected, never ignored.
  * Global checks run first on every argument (charset, length, no `..`), so no per-binary rule
    can forget them.

Rule ids are stable strings like "docker.subcommand" or "curl.url"; they go into
`RejectionInfo.rule_id`, logs and the eval report, so don't rename one without updating
docs/INTERFACES.md §2.4.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_POLICY_PATH = Path(__file__).with_name("allowlist.yaml")

# Same bounds as contracts.DiagnoseRequest, enforced again here so the validator is safe to call
# on its own (defense in depth: don't rely on the caller having validated the model).
MAX_ARGS = 32
MAX_ARG_LEN = 512

# Positive charset: every character of every argument must be one of these. Shell metacharacters
# (; | & $ ` < > ( ) quotes, globs, newlines, NUL), spaces and non-ASCII are all impossible.
# argv runs without a shell, so metacharacters would not be *interpreted* anyway; refusing them
# still matters: it blocks smuggling into programs that do their own parsing (e.g. a container
# name with a newline in a log line), and it makes the rule easy to state and to test.
_ARG_CHARSET = re.compile(r"^[A-Za-z0-9._:/=-]+$")
_INT = re.compile(r"^[0-9]{1,6}$")
_DURATION = re.compile(r"^([0-9]{1,4})([smh])$")


# ---------------------------------------------------------------------------------------------
# Policy (allowlist.yaml)
# ---------------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DockerLogsPolicy(_Model):
    max_tail: int = Field(ge=1, le=10_000)
    max_since_hours: int = Field(ge=1, le=168)


class CurlPolicy(_Model):
    urls: tuple[str, ...]
    max_time_s: int = Field(ge=1, le=60)


class RedisCliPolicy(_Model):
    commands: tuple[str, ...]
    info_sections: tuple[str, ...]
    config_params: tuple[str, ...]


class PgIsreadyPolicy(_Model):
    users: tuple[str, ...]
    databases: tuple[str, ...]


class NginxPolicy(_Model):
    flags: tuple[str, ...]


class ExecPrograms(_Model):
    # Field names with dashes in YAML (redis-cli) map to these via aliases.
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    redis_cli: RedisCliPolicy | None = Field(default=None, alias="redis-cli")
    pg_isready: PgIsreadyPolicy | None = None
    nginx: NginxPolicy | None = None

    def names(self) -> list[str]:
        return [
            name
            for name, value in (
                ("redis-cli", self.redis_cli),
                ("pg_isready", self.pg_isready),
                ("nginx", self.nginx),
            )
            if value is not None
        ]


class Policy(_Model):
    version: Literal[1]
    containers: tuple[str, ...]
    docker_logs: DockerLogsPolicy
    curl: CurlPolicy
    readable_files: tuple[str, ...]
    exec: dict[str, ExecPrograms]


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> Policy:
    """Load and validate allowlist.yaml. A typo or unknown key fails here, at startup."""
    policy = Policy.model_validate(yaml.safe_load(path.read_text()))
    unknown = set(policy.exec) - set(policy.containers)
    if unknown:
        raise ValueError(f"exec rules for containers not in `containers`: {sorted(unknown)}")
    return policy


# ---------------------------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """The validator's answer. `reason` is written for the diagnoser's LLM to read: it says what
    was wrong and, where useful, what is allowed instead, so the agent can correct itself."""

    allowed: bool
    rule_id: str
    reason: str
    argv: tuple[str, ...] = ()


def _allow(argv: Sequence[str], rule_id: str) -> Decision:
    return Decision(True, rule_id, "allowed", tuple(argv))


class _Reject(Exception):
    """Internal: raised anywhere in a check, turned into a rejected Decision by check()."""

    def __init__(self, rule_id: str, reason: str) -> None:
        super().__init__(reason)
        self.rule_id = rule_id
        self.reason = reason


# ---------------------------------------------------------------------------------------------
# Flag parsing
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Flag:
    """One allowed flag. `names` are all spellings (e.g. ("-n", "--tail")); `kind` says whether
    it takes a value and how that value is checked."""

    names: tuple[str, ...]
    kind: Literal["bool", "int", "duration", "choice"]
    min: int = 0
    max: int = 0
    choices: tuple[str, ...] = ()

    @property
    def canonical(self) -> str:
        return self.names[-1]


def _parse_flags(
    tokens: Sequence[str], flags: Sequence[_Flag], prefix: str
) -> tuple[dict[str, str | bool], list[str]]:
    """Split tokens into {canonical_flag: value} and positionals, rejecting anything unknown.

    Supports `--tail 200`, `--tail=200`, `-n 200` and combined short booleans (`-fsS`).
    Refuses: unknown flags, duplicates, a value-taking flag with no value, `--` (end of options;
    it would let a later token that looks like a flag be treated as a positional).
    """
    by_name = {name: flag for flag in flags for name in flag.names}
    parsed: dict[str, str | bool] = {}
    positionals: list[str] = []
    i = 0

    def put(flag: _Flag, value: str | bool) -> None:
        if flag.canonical in parsed:
            raise _Reject(f"{prefix}.flag_duplicate", f"{flag.canonical} given twice")
        parsed[flag.canonical] = value

    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            raise _Reject(f"{prefix}.flag", "`--` is not allowed")
        if not tok.startswith("-") or tok == "-":
            positionals.append(tok)
            i += 1
            continue

        name, eq, inline_value = tok.partition("=")
        flag = by_name.get(name)

        # Combined short booleans, e.g. curl -fsS == -f -s -S.
        if flag is None and not tok.startswith("--") and len(tok) > 2 and not eq:
            parts = [by_name.get(f"-{c}") for c in tok[1:]]
            if all(p is not None and p.kind == "bool" for p in parts):
                for p in parts:
                    put(p, True)  # type: ignore[arg-type]
                i += 1
                continue

        if flag is None:
            raise _Reject(f"{prefix}.flag", f"flag {name} is not allowed")

        if flag.kind == "bool":
            if eq:
                raise _Reject(f"{prefix}.flag", f"{name} takes no value")
            put(flag, True)
            i += 1
            continue

        if eq:
            value = inline_value
            i += 1
        else:
            if i + 1 >= len(tokens):
                raise _Reject(f"{prefix}.flag_value", f"{name} needs a value")
            value = tokens[i + 1]
            i += 2
        put(flag, _check_flag_value(flag, value, prefix))

    return parsed, positionals


def _check_flag_value(flag: _Flag, value: str, prefix: str) -> str:
    rule = f"{prefix}.flag_value"
    if flag.kind == "int":
        if not _INT.match(value) or not (flag.min <= int(value) <= flag.max):
            raise _Reject(rule, f"{flag.canonical} must be an integer {flag.min}..{flag.max}")
    elif flag.kind == "duration":
        m = _DURATION.match(value)
        seconds = 0
        if m:
            seconds = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
        if not m or seconds < 1 or seconds > flag.max:
            raise _Reject(
                rule, f"{flag.canonical} must look like 30s, 15m or 2h, at most {flag.max // 3600}h"
            )
    elif flag.kind == "choice" and value not in flag.choices:
        raise _Reject(rule, f"{flag.canonical} must be one of {', '.join(flag.choices)}")
    return value


# ---------------------------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------------------------


class CommandValidator:
    """Stateless after construction; safe to share across concurrent requests."""

    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or load_policy()
        self._binaries: dict[str, Callable[[Sequence[str]], str]] = {
            "docker": self._check_docker,
            "curl": self._check_curl,
            "cat": self._check_cat,
            "df": self._check_df,
            "free": self._check_free,
            "uptime": self._check_uptime,
        }

    # -- entry point ---------------------------------------------------------------------------

    def check(self, argv: Sequence[str]) -> Decision:
        """Validate one argv. Never raises for bad input: always returns a Decision."""
        try:
            self._check_global(argv)
            binary = argv[0]
            if "/" in binary:
                raise _Reject("binary.path", "give the bare program name, not a path")
            handler = self._binaries.get(binary)
            if handler is None:
                raise _Reject(
                    "binary.not_allowed",
                    f"{binary} is not allowed; allowed: {', '.join(sorted(self._binaries))}",
                )
            rule_id = handler(argv[1:])
            return _allow(argv, rule_id)
        except _Reject as r:
            return Decision(False, r.rule_id, r.reason, tuple(argv) if _printable(argv) else ())

    # -- global rules, applied to every argument -----------------------------------------------

    def _check_global(self, argv: Sequence[str]) -> None:
        if not isinstance(argv, list | tuple) or not argv:
            raise _Reject("argv.empty", "command must be a non-empty list of arguments")
        if len(argv) > MAX_ARGS:
            raise _Reject("argv.too_long", f"at most {MAX_ARGS} arguments")
        for arg in argv:
            if not isinstance(arg, str) or not arg:
                raise _Reject("argv.charset", "every argument must be a non-empty string")
            if len(arg) > MAX_ARG_LEN:
                raise _Reject(
                    "argv.arg_too_long", f"arguments are at most {MAX_ARG_LEN} characters"
                )
            if not _ARG_CHARSET.match(arg):
                raise _Reject(
                    "argv.charset",
                    "arguments may only contain letters, digits and . _ : / = - "
                    "(no spaces, quotes, shell characters or globs)",
                )
            if ".." in arg.split("/"):
                raise _Reject("argv.path", "`..` path segments are not allowed")

    # -- docker ---------------------------------------------------------------------------------

    def _container(self, name: str, prefix: str) -> str:
        if name not in self.policy.containers:
            raise _Reject(
                f"{prefix}.container",
                f"unknown container {name}; allowed: {', '.join(self.policy.containers)}",
            )
        return name

    def _check_docker(self, args: Sequence[str]) -> str:
        if not args:
            raise _Reject(
                "docker.subcommand", "docker needs a subcommand: ps, logs, inspect, stats, exec"
            )
        sub, rest = args[0], args[1:]
        if sub.startswith("-"):
            # Global flags like -H tcp://other-host, --context, --config would point docker at a
            # different daemon or config. Never allowed.
            raise _Reject("docker.global_flag", "docker global flags are not allowed")

        if sub == "ps":
            _, pos = _parse_flags(
                rest, [_Flag(("-a", "--all"), "bool"), _Flag(("--no-trunc",), "bool")], "docker"
            )
            if pos:
                raise _Reject("docker.args", "docker ps takes no positional arguments")
            return "docker.ps"

        if sub == "logs":
            if any(t in ("-f", "--follow") or t.startswith("--follow=") for t in rest):
                raise _Reject("docker.follow", "--follow never exits; use --tail N or --since 15m")
            lp = self.policy.docker_logs
            _, pos = _parse_flags(
                rest,
                [
                    _Flag(("-n", "--tail"), "int", min=1, max=lp.max_tail),
                    _Flag(("--since",), "duration", max=lp.max_since_hours * 3600),
                    _Flag(("-t", "--timestamps"), "bool"),
                ],
                "docker",
            )
            if len(pos) != 1:
                raise _Reject("docker.args", "docker logs takes exactly one container")
            self._container(pos[0], "docker")
            return "docker.logs"

        if sub == "inspect":
            # No flags: in particular no --format (Go templates) and no --type.
            _, pos = _parse_flags(rest, [], "docker")
            if not 1 <= len(pos) <= len(self.policy.containers):
                raise _Reject("docker.args", "docker inspect takes one or more containers")
            for name in pos:
                self._container(name, "docker")
            return "docker.inspect"

        if sub == "stats":
            flags, pos = _parse_flags(rest, [_Flag(("--no-stream",), "bool")], "docker")
            if "--no-stream" not in flags:
                raise _Reject(
                    "docker.args", "docker stats needs --no-stream (otherwise it never exits)"
                )
            for name in pos:
                self._container(name, "docker")
            return "docker.stats"

        if sub == "exec":
            return self._check_docker_exec(rest)

        raise _Reject(
            "docker.subcommand",
            f"docker {sub} is not allowed; allowed: ps, logs, inspect, stats --no-stream, exec (limited)",
        )

    def _check_docker_exec(self, rest: Sequence[str]) -> str:
        # docker exec's own flags (-u root, --privileged, -e VAR=..., -w, -i, -t, -d) are all
        # refused: the only shape allowed is `docker exec <container> <program> <args...>`.
        if not rest or rest[0].startswith("-"):
            raise _Reject("docker.exec_flag", "docker exec flags are not allowed")
        container = self._container(rest[0], "docker")
        programs = self.policy.exec.get(container)
        if programs is None or len(rest) < 2:
            allowed = {c: p.names() for c, p in self.policy.exec.items()}
            raise _Reject("docker.exec_program", f"allowed exec programs per container: {allowed}")
        program, args = rest[1], rest[2:]
        if program == "redis-cli" and programs.redis_cli:
            return self._check_redis_cli(args, programs.redis_cli)
        if program == "pg_isready" and programs.pg_isready:
            return self._check_pg_isready(args, programs.pg_isready)
        if program == "nginx" and programs.nginx:
            return self._check_nginx(args, programs.nginx)
        raise _Reject(
            "docker.exec_program",
            f"{program} is not allowed in {container}; allowed: {', '.join(programs.names())}",
        )

    # -- programs inside containers -------------------------------------------------------------

    def _check_redis_cli(self, args: Sequence[str], rp: RedisCliPolicy) -> str:
        # No redis-cli flags at all: -h/-p (another server), -a (password), --eval, -x (stdin),
        # --rdb/--pipe (bulk dump/load) are all refused.
        if any(a.startswith("-") for a in args):
            raise _Reject("redis.flag", "redis-cli flags are not allowed")
        upper = [a.upper() for a in args]
        allowed = {c.upper() for c in rp.commands}
        two = " ".join(upper[:2])
        if two in allowed and two == "CONFIG GET":
            if len(args) != 3 or args[2] not in rp.config_params:
                raise _Reject(
                    "redis.config_param",
                    f"CONFIG GET takes one of: {', '.join(rp.config_params)}",
                )
            return "redis.config_get"
        if two in allowed and two == "MEMORY STATS":
            if len(args) != 2:
                raise _Reject("redis.args", "MEMORY STATS takes no arguments")
            return "redis.memory_stats"
        if upper and upper[0] in allowed and upper[0] in ("PING", "DBSIZE"):
            if len(args) != 1:
                raise _Reject("redis.args", f"{upper[0]} takes no arguments")
            return f"redis.{upper[0].lower()}"
        if upper and upper[0] in allowed and upper[0] == "INFO":
            if len(args) > 2 or (len(args) == 2 and args[1].lower() not in rp.info_sections):
                raise _Reject(
                    "redis.args", f"INFO takes one optional section: {', '.join(rp.info_sections)}"
                )
            return "redis.info"
        raise _Reject("redis.command", f"allowed redis-cli commands: {', '.join(rp.commands)}")

    def _check_pg_isready(self, args: Sequence[str], pp: PgIsreadyPolicy) -> str:
        _, pos = _parse_flags(
            args,
            [
                _Flag(("-U", "--username"), "choice", choices=pp.users),
                _Flag(("-d", "--dbname"), "choice", choices=pp.databases),
            ],
            "pg_isready",
        )
        if pos:
            raise _Reject("pg_isready.args", "pg_isready takes only -U and -d")
        return "pg_isready"

    def _check_nginx(self, args: Sequence[str], np_: NginxPolicy) -> str:
        if len(args) != 1 or args[0] not in np_.flags:
            raise _Reject("nginx.args", f"nginx takes exactly one of: {', '.join(np_.flags)}")
        return "nginx.test_config"

    # -- host programs ---------------------------------------------------------------------------

    def _check_curl(self, args: Sequence[str]) -> str:
        cp = self.policy.curl
        _, pos = _parse_flags(
            args,
            [
                _Flag(("-f", "--fail"), "bool"),
                _Flag(("-s", "--silent"), "bool"),
                _Flag(("-S", "--show-error"), "bool"),
                _Flag(("-i", "--include"), "bool"),
                _Flag(("-m", "--max-time"), "int", min=1, max=cp.max_time_s),
            ],
            "curl",
        )
        # Everything that writes files (-o, -O), sends data (-d, -F, -T), changes method (-X),
        # adds headers (-H, e.g. "Metadata-Flavor: Google"), follows redirects (-L), reads a
        # config (-K) or takes the URL from a flag (--url) was refused by the parser above.
        if len(pos) != 1 or pos[0] not in cp.urls:
            raise _Reject("curl.url", f"curl may only GET: {', '.join(cp.urls)}")
        return "curl.get"

    def _check_cat(self, args: Sequence[str]) -> str:
        if not args or len(args) > len(self.policy.readable_files):
            raise _Reject("cat.args", "cat takes one or more files")
        for a in args:
            if a.startswith("-"):
                raise _Reject("cat.flag", "cat flags are not allowed")
            if a not in self.policy.readable_files:
                raise _Reject(
                    "cat.file", f"readable files: {', '.join(self.policy.readable_files)}"
                )
        return "cat"

    def _check_df(self, args: Sequence[str]) -> str:
        if list(args) not in ([], ["-h"]):
            raise _Reject("df.args", "df takes only -h")
        return "df"

    def _check_free(self, args: Sequence[str]) -> str:
        if list(args) not in ([], ["-m"], ["-h"]):
            raise _Reject("free.args", "free takes only -m or -h")
        return "free"

    def _check_uptime(self, args: Sequence[str]) -> str:
        if args:
            raise _Reject("uptime.args", "uptime takes no arguments")
        return "uptime"

    # -- for the diagnoser's prompt --------------------------------------------------------------

    def summary(self) -> str:
        """Plain-text description of what may be run, for the diagnoser's system prompt.

        Generated from the policy, so the prompt can't drift from what the executor accepts.
        """
        p = self.policy
        containers = ", ".join(p.containers)
        lines = [
            "Read-only diagnostic commands (argv lists, no shell). Anything else is rejected.",
            "- docker ps [-a] [--no-trunc]",
            f"- docker logs [--tail N<= {p.docker_logs.max_tail}] [--since 15m|2h<= {p.docker_logs.max_since_hours}h] [-t] CONTAINER",
            "- docker inspect CONTAINER [CONTAINER...]",
            "- docker stats --no-stream [CONTAINER...]",
            f"  CONTAINER is one of: {containers}",
        ]
        for container, progs in p.exec.items():
            if progs.redis_cli:
                rc = progs.redis_cli
                lines.append(
                    f"- docker exec {container} redis-cli PING | DBSIZE | INFO [{'|'.join(rc.info_sections)}]"
                    f" | CONFIG GET {'|'.join(rc.config_params)} | MEMORY STATS"
                )
            if progs.pg_isready:
                pg = progs.pg_isready
                lines.append(
                    f"- docker exec {container} pg_isready [-U {'|'.join(pg.users)}] [-d {'|'.join(pg.databases)}]"
                )
            if progs.nginx:
                lines.append(f"- docker exec {container} nginx {'|'.join(progs.nginx.flags)}")
        lines += [
            f"- curl [-f] [-s] [-S] [-i] [--max-time N<= {p.curl.max_time_s}] URL, URL one of: {', '.join(p.curl.urls)}",
            f"- cat FILE..., FILE one of: {', '.join(p.readable_files)}",
            "- df [-h] · free [-m|-h] · uptime",
        ]
        return "\n".join(lines)


def _printable(argv: Sequence[str]) -> bool:
    """Only echo argv back in a Decision if it's a list of short strings (it goes into logs)."""
    return (
        isinstance(argv, list | tuple)
        and len(argv) <= MAX_ARGS
        and all(isinstance(a, str) and len(a) <= MAX_ARG_LEN for a in argv)
    )
