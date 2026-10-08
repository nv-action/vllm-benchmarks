"""Profiling setup for multi-node internal DP test servers."""

from tools.profile import ProfileSpec, ServeInstance, install_manifest, make_instance, with_profiler_config


def configure_profiling(config, spec: ProfileSpec) -> None:
    if not spec.enabled:
        return

    instances: dict[int, ServeInstance] = {}
    leader = next((node for node in config.nodes if not node.headless), None)
    leader_port = (leader.envs or {}).get("SERVER_PORT", config.server_port) if leader else None
    for node in config.nodes:
        if node.headless and (spec.scope != "all" or config.disagg_cfg or leader is None):
            continue
        if config.disagg_cfg and config.disagg_cfg.is_prefiller(node.index):
            role = "prefill"
            rank = config.disagg_cfg.prefiller_indices.index(node.index)
        elif config.disagg_cfg and config.disagg_cfg.is_decoder(node.index):
            role = "decode"
            rank = config.disagg_cfg.decoder_indices.index(node.index)
        else:
            role, rank = "standalone", node.index
        name = f"{role}-{rank}" if role != "standalone" else f"dp-{rank}"
        port = (node.envs or {}).get("SERVER_PORT", config.server_port)
        # Internal DP's headless rank has no HTTP API; the leader controls both engines.
        endpoint = f"http://{leader.ip}:{leader_port}" if node.headless else f"http://{node.ip}:{port}"
        instances[node.index] = make_instance(name, endpoint, role, rank, node.index)

    if config.is_master:
        install_manifest(list(instances.values()))
    current = instances.get(config.cur_node.index)
    if current:
        config.server_cmd = with_profiler_config(config.server_cmd, current, spec)
