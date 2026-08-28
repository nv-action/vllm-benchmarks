=== "Prebuilt image"

    #### Use a prebuilt vLLM Ascend image {: #installation-prebuilt-image }

    The host needs only a working Ascend driver and firmware, plus Docker. The image includes the CANN user-space environment, PyTorch/TorchNPU, vLLM, and vLLM Ascend.

    ##### Select an image {: #installation-prebuilt-image-selection }

    | Hardware | Ubuntu | openEuler |
    | --- | --- | --- |
    | Ascend A2 series products | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}` | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-openeuler` |
    | Ascend A3 series products | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-a3` | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-a3-openeuler` |
    | Ascend 310P series products | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-310p` | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-310p-openeuler` |
    | Ascend 950DT series products | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-950dt` | `quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-950dt-openeuler` |

    ##### Pull and inspect the image {: #installation-prebuilt-image-pull }

    ```bash
    export IMAGE=quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}
    docker pull "$IMAGE"
    docker image inspect "$IMAGE" >/dev/null && echo "Image ready: $IMAGE"
    ```

    ??? tip "Build an image from a Dockerfile"

        ```bash
        git clone --depth 1 --branch {{ vllm_ascend_version }} \
            https://github.com/vllm-project/vllm-ascend.git
        cd vllm-ascend
        docker build -t vllm-ascend-dev:latest -f Dockerfile .
        ```

        The default `Dockerfile` targets A2. For other hardware, use the corresponding Dockerfile in the repository, such as the A3, 310P, or 950DT Dockerfile.
