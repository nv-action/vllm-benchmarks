import json
import logging
import os
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

PROFILE_ENABLED_ENV = "VLLM_ASCEND_FORCE_PROFILE"
PROFILE_DIR_ENV = "VLLM_ASCEND_PROFILE_DIR"
PROFILE_WITH_STACK_ENV = "VLLM_TORCH_PROFILER_WITH_STACK"

PROFILER_CONFIG_OPTION = "--profiler-config"
PROFILE_START_TIMEOUT_SECONDS = 120
PROFILE_STOP_TIMEOUT_SECONDS = 900
PROFILE_MAX_ITERATIONS = 20


def profiling_enabled() -> bool:
    """Return whether the nightly run explicitly requested profiling."""
    return os.environ.get(PROFILE_ENABLED_ENV, "").lower() in ("true", "1")


def inject_profiler_config(server_args: Sequence[str], *, output_subdir: str | None = None) -> list[str]:
    """Return server arguments with the nightly torch profiler configured.

    Existing ``--profiler-config VALUE`` and ``--profiler-config=VALUE`` forms
    are removed so repeated injection remains deterministic.
    """
    args = list(server_args)
    if not profiling_enabled():
        return args

    profile_root = os.environ.get(PROFILE_DIR_ENV, "")
    if not profile_root:
        raise ValueError(f"{PROFILE_ENABLED_ENV} is set but {PROFILE_DIR_ENV} is empty")

    profile_dir = Path(profile_root)
    if output_subdir:
        profile_dir /= output_subdir

    with_stack = os.environ.get(PROFILE_WITH_STACK_ENV, "0").lower() not in ("0", "false", "f")
    profiler_config = {
        "profiler": "torch",
        "torch_profiler_dir": str(profile_dir),
        "torch_profiler_with_stack": with_stack,
        # Bound the trace size so a full benchmark cannot generate an
        # unmanageably large artifact.
        "ignore_frontend": True,
        "max_iterations": PROFILE_MAX_ITERATIONS,
    }

    filtered_args: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == PROFILER_CONFIG_OPTION:
            index += 1
            if index < len(args) and not args[index].startswith("--"):
                index += 1
            continue
        if arg.startswith(f"{PROFILER_CONFIG_OPTION}="):
            index += 1
            continue
        filtered_args.append(arg)
        index += 1

    filtered_args.extend((PROFILER_CONFIG_OPTION, json.dumps(profiler_config)))
    logger.info("Injected %s into serve command: %s", PROFILER_CONFIG_OPTION, profiler_config)
    return filtered_args


def _profile_control_url(base_url: str, action: str) -> str:
    return f"{base_url.rstrip('/')}/{action}"


def _post_profile_control(base_url: str, action: str, timeout: int) -> None:
    url = _profile_control_url(base_url, action)
    logger.info("Calling profiler control endpoint: POST %s", url)
    response = requests.post(url, timeout=timeout)
    response.raise_for_status()


@contextmanager
def profiling_session(base_urls: Iterable[str]) -> Iterator[None]:
    """Profile a workload and always flush traces before servers terminate."""
    if not profiling_enabled():
        yield
        return

    targets = list(dict.fromkeys(url.rstrip("/") for url in base_urls))
    if not targets:
        raise ValueError("Profiling was requested but no vLLM profiler endpoints were provided")

    started_targets: list[str] = []
    workload_error: BaseException | None = None
    try:
        for target in targets:
            _post_profile_control(target, "start_profile", PROFILE_START_TIMEOUT_SECONDS)
            started_targets.append(target)
        yield
    except BaseException as exc:
        workload_error = exc
        raise
    finally:
        stop_errors: list[str] = []
        for target in reversed(started_targets):
            try:
                _post_profile_control(target, "stop_profile", PROFILE_STOP_TIMEOUT_SECONDS)
            except Exception as exc:  # keep stopping the remaining profiler instances
                logger.exception("Failed to stop profiler at %s", target)
                stop_errors.append(f"{target}: {exc}")

        if stop_errors and workload_error is None:
            raise RuntimeError("Failed to flush profiling output: " + "; ".join(stop_errors))
