"""Names of the remediation actions the executor offers (docs/INTERFACES.md §2.4).

Only the *names* are a contract: the diagnoser proposes them, the Slack message shows them, the
approval token signs them, the eval's fault.yaml files reference them. Each action's parameter
schema and behaviour belong to the executor (src/executor/, Phase 1) and can change without a
contract change, as long as the name stays.
"""

REMEDIATION_ACTIONS: frozenset[str] = frozenset(
    {
        "restart_service",
        "restore_config",
        "set_env",
        "scale_memory_limit",
        "rollback_image",
        "prune_path",
    }
)
