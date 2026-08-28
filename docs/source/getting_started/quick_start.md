# Quick Start

This guide helps you run your first inference workload or deploy an online service on a prepared Ascend host using a prebuilt vLLM Ascend container.

## Requirements {: #quick-start-requirements }

- Operating system: Linux
- Python: {{ release_python_version }}
- Hardware equipped with Ascend NPUs. This guide supports the following devices:

| Type | Common products |
| --- | --- |
| Ascend A2 series products | Atlas 800T A2, Atlas 900 A2 PoD, Atlas 200T A2 Box16, Atlas 300T A2, Atlas 800I A2, and others |
| Ascend A3 series products | Atlas 800T A3, Atlas 900 A3 SuperPoD, Atlas 9000 A3 SuperPoD, Atlas 800I A3, and others |
| Ascend 310P series products | Atlas 300I DUO and 310P SOC |
| Ascend 950DT series products | Atlas 950DT |

- Prebuilt vLLM Ascend containers:

| Hardware | Ubuntu | openEuler |
| --- | --- | --- |
| Ascend A2 series products | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}` | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-openeuler` |
| Ascend A3 series products | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-a3` | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-a3-openeuler` |
| Ascend 310P series products | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-310p` | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-310p-openeuler` |
| Ascend 950DT series products | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-950dt` | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-950dt-openeuler` |

??? note "Software versions included in the vLLM Ascend container"

    | Software | Default version used in this guide |
    | --- | --- |
    | Python | `{{ release_image_python_version }}` |
    | Ascend HDK | See the [CANN {{ release_cann_version }} documentation](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/{{ release_cann_version.replace('.', '') }}/softwareinst/releasenote/{{ release_cann_version }}/release-notes.md) |
    | CANN | `{{ release_cann_version }}` |
    | TorchNPU | `{{ release_torch_npu_version }}` |
    | PyTorch | `{{ release_pytorch_version }}` |
    | NNAL | `{{ release_nnal_version }}` |
    | Triton Ascend | `{{ release_triton_ascend_version }}` / Not supported on 310P |
    | vLLM | `{{ vllm_version }}` |
    | vLLM Ascend | `{{ vllm_ascend_version }}` |

## Installation {: #quick-start-installation }

??? tip "If image downloads are slow"

    vLLM Ascend images are downloaded from `quay.io` by default. If direct access is slow, use one of the following registry mirrors to accelerate the download.

    For example, the original image address is:

    ```text
    quay.io/ascend/vllm-ascend:<TAG>
    ```

    You can replace it with:

    ```text
    # Replace with tag you want to pull
    TAG={{ vllm_ascend_version }}
    # use
    docker pull m.daocloud.io/quay.io/ascend/vllm-ascend:$TAG
    # or
    docker pull quay.nju.edu.cn/ascend/vllm-ascend:$TAG
    ```

    Replace only the registry prefix and preserve the complete original image tag, including suffixes such as `-a3`, `-310p`, `-950dt`, and `-openeuler`.

{% include "getting_started/quick_start/installation/atlas-a2.inc.md" %}

{% include "getting_started/quick_start/installation/atlas-a3.inc.md" %}

{% include "getting_started/quick_start/installation/atlas-300i-duo.inc.md" %}

{% include "getting_started/quick_start/installation/310p-soc.inc.md" %}

{% include "getting_started/quick_start/installation/atlas-950dt.inc.md" %}

### Verify the container environment

Run the following commands in the container:

```bash
npu-smi info

python3 - <<'PY'
import torch
import vllm
import vllm_ascend

assert torch.npu.is_available(), "No available Ascend NPU detected in the container"
print("vLLM Ascend environment: OK")
PY
```

## Inference {: #quick-start-inference }

??? tip "If Hugging Face access is restricted"

    If your environment cannot reliably access Hugging Face, model downloads may fail due to connection timeouts, DNS errors, or other network issues. You can switch to ModelScope:

    ```bash
    export VLLM_USE_MODELSCOPE=True
    pip install "modelscope>=1.18.1,<1.38"
    ```

    If the model has already been downloaded locally, replace the model ID in the examples below with the local directory. You do not need to set this environment variable.

### Offline inference {: #quick-start-offline-inference }

{% include "getting_started/quick_start/offline/atlas-a2.inc.md" %}

{% include "getting_started/quick_start/offline/atlas-a3.inc.md" %}

{% include "getting_started/quick_start/offline/310p.inc.md" %}

{% include "getting_started/quick_start/offline/atlas-950dt.inc.md" %}

### Online serving {: #quick-start-online-serving }

{% include "getting_started/quick_start/online/atlas-a2.inc.md" %}

{% include "getting_started/quick_start/online/atlas-a3.inc.md" %}

{% include "getting_started/quick_start/online/310p.inc.md" %}

{% include "getting_started/quick_start/online/atlas-950dt.inc.md" %}

## Next steps {: #quick-start-next-steps }

- See [Supported Models](../user_guide/support_matrix/supported_models.md) to choose another model.
- See [Model Tutorials](../tutorials/models/index.md) for deployment instructions for specific models.
- See [Installation Guide > Set up the software environment](installation.md#installation-software-environment) for pip, CANN, and source installation methods.
- See [Feature Tutorials](../tutorials/features/index.md) for distributed deployment and advanced features.
- See [FAQ](../faqs.md) to troubleshoot common deployment issues.
