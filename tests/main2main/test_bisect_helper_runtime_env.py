import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts" / "bisect_helper.py"


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


def test_build_batch_matrix_uses_unit_runtime_workflow_config():
    module = load_module()

    entry = _entry_for_path(module, "tests/ut/spec_decode/test_eagle_proposer.py::test_x")

    assert entry["runner"] == "linux-amd64-cpu-8-hk"
    assert entry["image"] == "quay.nju.edu.cn/ascend/cann:8.5.1-910b-ubuntu22.04-py3.11"
    assert entry["cenv_SOC_VERSION"] == "ascend910b1"
    assert entry["cenv_UV_PYTHON"] == "python3"
    assert "python3-pip git vim wget net-tools" in entry["sys_deps"]
    assert "uv pip install . --extra-index-url https://download.pytorch.org/whl/cpu/" in entry["vllm_install"]
    assert "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/Ascend/ascend-toolkit/latest/x86_64-linux/devlib" in entry["ascend_install"]
    assert '"TORCH_DEVICE_BACKEND_AUTOLOAD": "0"' in entry["runtime_env"]


def test_build_batch_matrix_uses_singlecard_full_runtime_workflow_config():
    module = load_module()

    entry = _entry_for_path(module, "tests/e2e/singlecard/test_sampler.py::test_x")

    assert entry["runner"] == "linux-aarch64-a2-1"
    assert entry["image"] == "swr.cn-southwest-2.myhuaweicloud.com/base_image/ascend-ci/cann:8.5.1-910b-ubuntu22.04-py3.11"
    assert entry["cenv_MODELSCOPE_HUB_FILE_LOCK"] == "False"
    assert entry["cenv_UV_INDEX_URL"] == "http://cache-service.nginx-pypi-cache.svc.cluster.local/pypi/simple"
    assert "pip config set global.index-url http://cache-service.nginx-pypi-cache.svc.cluster.local/pypi/simple" in entry["sys_deps"]
    assert "update-alternatives --install /usr/bin/clang clang /usr/bin/clang-15 20" in entry["sys_deps"]
    assert "uv pip install -r requirements-dev.txt" in entry["ascend_install"]
    assert '"PYTORCH_NPU_ALLOC_CONF": "max_split_size_mb:256"' in entry["runtime_env"]


def test_build_batch_matrix_uses_310p_multicard_runtime_workflow_config():
    module = load_module()

    entry = _entry_for_path(module, "tests/e2e/310p/multicard/test_dense_model_multicard.py::test_x")

    assert entry["runner"] == "linux-aarch64-310p-4"
    assert entry["image"] == "swr.cn-southwest-2.myhuaweicloud.com/base_image/ascend-ci/cann:8.5.1-310p-ubuntu22.04-py3.11"
    assert entry["cenv_HF_HUB_OFFLINE"] == "1"
    assert "clang-15" not in entry["sys_deps"]
    assert "tests/e2e/310p/multicard/test_dense_model_multicard.py::test_x" in entry["test_cmds"]
