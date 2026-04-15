import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "tools" / "bisect_helper.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bisect_helper", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _entry_for_path(module, test_path: str):
    matrix = module.build_batch_matrix(f"pytest -sv {test_path}")
    include = matrix["include"]
    assert len(include) == 1
    return include[0]


def test_load_yaml_reads_workflow_with_block_scalars():
    module = load_module()

    data = module._load_yaml(Path(__file__).resolve().parents[2] / ".github" / "workflows" / "_e2e_test.yaml")

    config_step = next(step for step in data["jobs"]["e2e-full"]["steps"] if step["name"] == "Config mirrors")
    assert "pip config set global.index-url" in config_step["run"]


def test_build_batch_matrix_uses_unit_runtime_workflow_config():
    module = load_module()

    entry = _entry_for_path(module, "tests/ut/spec_decode/test_eagle_proposer.py::test_x")

    assert entry["group"] == "ut"
    assert entry["runner"] == "linux-amd64-cpu-8-hk"
    assert entry["image"] == "quay.nju.edu.cn/ascend/cann:8.5.1-910b-ubuntu22.04-py3.11"
    assert entry["cenv_SOC_VERSION"] == "ascend910b1"
    assert entry["cenv_UV_PYTHON"] == "python3"
    assert "python3-pip git vim wget net-tools" in entry["sys_deps"]
    assert "uv pip install . --extra-index-url https://download.pytorch.org/whl/cpu/" in entry["vllm_install"]
    assert (
        "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/Ascend/ascend-toolkit/latest/x86_64-linux/devlib"
        in entry["ascend_install"]
    )
    assert '"TORCH_DEVICE_BACKEND_AUTOLOAD": "0"' in entry["runtime_env"]
    assert '"SOC_VERSION": "ascend910b1"' not in entry["runtime_env"]
    assert '"UV_PYTHON": "python3"' not in entry["runtime_env"]


def test_build_batch_matrix_uses_singlecard_full_runtime_workflow_config():
    module = load_module()

    entry = _entry_for_path(module, "tests/e2e/singlecard/test_sampler.py::test_x")

    assert entry["group"] == "e2e-singlecard"
    assert entry["runner"] == "linux-aarch64-a2-1"
    assert (
        entry["image"] == "swr.cn-southwest-2.myhuaweicloud.com/base_image/ascend-ci/cann:8.5.1-910b-ubuntu22.04-py3.11"
    )
    assert entry["cenv_MODELSCOPE_HUB_FILE_LOCK"] == "False"
    assert entry["cenv_UV_INDEX_URL"] == "http://cache-service.nginx-pypi-cache.svc.cluster.local/pypi/simple"
    assert (
        "pip config set global.index-url http://cache-service.nginx-pypi-cache.svc.cluster.local/pypi/simple"
        in entry["sys_deps"]
    )
    assert "update-alternatives --install /usr/bin/clang clang /usr/bin/clang-15 20" in entry["sys_deps"]
    assert "uv pip install -r requirements-dev.txt" in entry["ascend_install"]
    assert '"PYTORCH_NPU_ALLOC_CONF": "max_split_size_mb:256"' in entry["runtime_env"]
    assert (
        '"UV_INDEX_URL": "http://cache-service.nginx-pypi-cache.svc.cluster.local/pypi/simple"'
        not in entry["runtime_env"]
    )
    assert '"VLLM_USE_MODELSCOPE": "True"' not in entry["runtime_env"]


def test_build_batch_matrix_uses_310p_multicard_runtime_workflow_config():
    module = load_module()

    entry = _entry_for_path(module, "tests/e2e/310p/multicard/test_dense_model_multicard.py::test_x")

    assert entry["group"] == "e2e-310p-4cards"
    assert entry["runner"] == "linux-aarch64-310p-4"
    assert (
        entry["image"] == "swr.cn-southwest-2.myhuaweicloud.com/base_image/ascend-ci/cann:8.5.1-310p-ubuntu22.04-py3.11"
    )
    assert entry["cenv_HF_HUB_OFFLINE"] == "1"
    assert "clang-15" not in entry["sys_deps"]
    assert "tests/e2e/310p/multicard/test_dense_model_multicard.py::test_x" in entry["test_cmds"]
    assert '"HF_HUB_OFFLINE": "1"' not in entry["runtime_env"]


def test_build_batch_matrix_uses_2cards_default_image_from_workflow_call_inputs():
    module = load_module()

    entry = _entry_for_path(module, "tests/e2e/multicard/2-cards/test_data_parallel.py::test_x")

    assert entry["group"] == "e2e-2cards"
    assert entry["image"] == "swr.cn-southwest-2.myhuaweicloud.com/base_image/ascend-ci/cann:8.5.1-a3-ubuntu22.04-py3.11"


def test_build_batch_matrix_uses_4cards_default_image_from_workflow_call_inputs():
    module = load_module()

    entry = _entry_for_path(module, "tests/e2e/multicard/4-cards/test_pipeline_parallel.py::test_x")

    assert entry["group"] == "e2e-4cards"
    assert entry["image"] == "m.daocloud.io/quay.io/ascend/cann:8.5.1-a3-ubuntu22.04-py3.11"


def test_build_batch_matrix_uses_distinct_groups_for_singlecard_and_310p_singlecard():
    module = load_module()

    matrix = module.build_batch_matrix(
        "pytest -sv tests/e2e/singlecard/test_sampler.py::test_x; "
        "pytest -sv tests/e2e/310p/singlecard/test_dense_model_singlecard.py::test_y"
    )

    groups = [entry["group"] for entry in matrix["include"]]
    assert "e2e-singlecard" in groups
    assert "e2e-310p-singlecard" in groups
    assert len(groups) == len(set(groups))


def test_build_batch_matrix_deduplicates_identical_commands_within_same_group():
    module = load_module()

    matrix = module.build_batch_matrix(
        "pytest -sv tests/e2e/multicard/2-cards/test_offline_inference_distributed.py::test_qwen3_dense_fc1_tp2; "
        "pytest -sv tests/e2e/multicard/2-cards/test_offline_inference_distributed.py::test_qwen3_dense_fc1_tp2"
    )

    include = matrix["include"]
    assert len(include) == 1
    assert include[0]["group"] == "e2e-2cards"
    assert include[0]["test_cmds"] == (
        "pytest -sv tests/e2e/multicard/2-cards/test_offline_inference_distributed.py::test_qwen3_dense_fc1_tp2"
    )
