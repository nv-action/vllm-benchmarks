=== "A3"

    #### Pull the image

    === "Ubuntu"

        ```bash
        export IMAGE=quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-a3
        docker pull "$IMAGE"
        ```

    === "openEuler"

        ```bash
        export IMAGE=quay.io/ascend/vllm-ascend:{{ vllm_ascend_version }}-a3-openeuler
        docker pull "$IMAGE"
        ```

    #### Start the container {: #quick-start-atlas-a3-container }

    ???+ note "A3 device mapping"

        A3 uses a dual-DIE design. This quick start exposes two Ascend device nodes for one selected device, such as `/dev/davinci0` and `/dev/davinci1`.

        Exposing two device nodes does not mean that the quick start automatically uses `tensor_parallel_size=2`. Whether parallelism is enabled depends on the model and deployment method.

    === "Ubuntu"

        ```bash
        export DEVICE0=/dev/davinci0
        export DEVICE1=/dev/davinci1
        export MODEL_CACHE="${HOME}/.cache"

        mkdir -p "$MODEL_CACHE"

        docker run --rm \
            --name vllm-ascend \
            --shm-size=1g \
            --device "$DEVICE0" \
            --device "$DEVICE1" \
            --device /dev/davinci_manager \
            --device /dev/devmm_svm \
            --device /dev/hisi_hdc \
            -v /usr/local/dcmi:/usr/local/dcmi \
            -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
            -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
            -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
            -v /etc/ascend_install.info:/etc/ascend_install.info \
            -v "$MODEL_CACHE:/root/.cache" \
            -p 8000:8000 \
            -it "$IMAGE" bash
        ```

    === "openEuler"

        ```bash
        export DEVICE0=/dev/davinci0
        export DEVICE1=/dev/davinci1
        export MODEL_CACHE="${HOME}/.cache"

        mkdir -p "$MODEL_CACHE"

        docker run --rm \
            --name vllm-ascend \
            --shm-size=1g \
            --device "$DEVICE0" \
            --device "$DEVICE1" \
            --device /dev/davinci_manager \
            --device /dev/devmm_svm \
            --device /dev/hisi_hdc \
            -v /usr/local/dcmi:/usr/local/dcmi \
            -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
            -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
            -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
            -v /etc/ascend_install.info:/etc/ascend_install.info \
            -v "$MODEL_CACHE:/root/.cache" \
            -p 8000:8000 \
            -it "$IMAGE" bash
        ```
