=== "Existing CANN environment"

    #### Install in an existing CANN environment {: #installation-existing-cann }

    This path applies to an official CANN base image or to CANN that is already installed on the host or in an existing container.

    ##### Prepare the CANN environment {: #installation-existing-cann-prepare }

    === "CANN base image"

        | Hardware | Recommended Ubuntu CANN base image |
        | --- | --- |
        | Ascend A2 series products | `quay.io/ascend/cann:{{ release_cann_version }}-910b-ubuntu22.04-py3.12` |
        | Ascend A3 series products | `quay.io/ascend/cann:{{ release_cann_version }}-a3-ubuntu22.04-py3.12` |
        | Ascend 310P series products | `quay.io/ascend/cann:{{ release_cann_version }}-310p-ubuntu22.04-py3.12` |
        | Ascend 950DT series products | `quay.io/ascend/cann:{{ release_cann_version }}-950-ubuntu22.04-py3.12` |

        The CANN base image already includes the Toolkit, the operator package for the target hardware, and NNAL. You do not need to reinstall CANN in the container. For other operating systems and tags, see the [CANN Container Images Overview](https://github.com/Ascend/cann-container-image/blob/main/OVERVIEW.md).

        ???+ warning "The container command below uses A2 as an example"

            The device nodes and host driver mounts in the following example apply to A2.

            A3, 310P, and 950DT must use device nodes and host driver mounts that match the hardware. Refer to the container startup commands for [A3](quick_start.md#quick-start-atlas-a3-container), [Atlas 300I DUO](quick_start.md#quick-start-atlas-300i-duo-container), [310P SOC](quick_start.md#quick-start-310p-soc-container), and [950DT](quick_start.md#quick-start-atlas-950dt-container) in the Quick Start.

        ```bash
        export DEVICE=/dev/davinci0
        export IMAGE=quay.io/ascend/cann:{{ release_cann_version }}-910b-ubuntu22.04-py3.12
        export MODEL_CACHE="${HOME}/.cache"

        mkdir -p "$MODEL_CACHE"
        docker pull "$IMAGE"

        docker run --rm \
            --name vllm-ascend-cann \
            --shm-size=4g \
            --net=host \
            --device "$DEVICE" \
            --device /dev/davinci_manager \
            --device /dev/devmm_svm \
            --device /dev/hisi_hdc \
            -v /usr/local/dcmi:/usr/local/dcmi \
            -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
            -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
            -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
            -v /etc/ascend_install.info:/etc/ascend_install.info \
            -v "$MODEL_CACHE:/root/.cache" \
            -it "$IMAGE" bash
        ```

    === "CANN already installed"

        ???+ warning "Verify the NNAL environment"

            Confirm that `/usr/local/Ascend/nnal/atb/set_env.sh` and `libatb.so` are available. If CANN is installed elsewhere, source the corresponding `set_env.sh`. If a "libatb.so not found" error occurs at runtime, make sure that the manual installation steps installed NNAL correctly.

        ```bash
        source /usr/local/Ascend/ascend-toolkit/set_env.sh

        if [ -f /usr/local/Ascend/nnal/atb/set_env.sh ]; then
            source /usr/local/Ascend/nnal/atb/set_env.sh
        fi

        export ASCEND_TOOLKIT_HOME="${ASCEND_TOOLKIT_HOME:-/usr/local/Ascend/ascend-toolkit/latest}"
        npu-smi info
        ```

    ##### Install vLLM and vLLM Ascend {: #installation-existing-cann-install }

{% filter indent(4, true) %}{% include "getting_started/installation/install_vllm_ascend.inc.md" %}{% endfilter %}
