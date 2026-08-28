# Installation Guide

This guide helps you prepare the host and software environment so that you can run your first model by following the [Quick Start](quick_start.md).

## Requirements {: #installation-requirements }

- Operating system: Linux
- Python: {{ release_python_version }}
- Hardware equipped with Ascend NPUs. This guide supports the following devices:

| Type | Common products |
| --- | --- |
| Ascend A2 series products | Atlas 800T A2, Atlas 900 A2 PoD, Atlas 200T A2 Box16, Atlas 300T A2, Atlas 800I A2, and others |
| Ascend A3 series products | Atlas 800T A3, Atlas 900 A3 SuperPoD, Atlas 9000 A3 SuperPoD, Atlas 800I A3, and others |
| Ascend 310P series products | Atlas 300I DUO and 310P SOC |
| Ascend 950DT series products | Atlas 950DT |

- Software:

=== "A2, A3, and 950DT"

    | Software | Supported version | Description |
    |---------------|----------------------------------|-------------------------------------------|
    | Ascend HDK | See the [CANN {{ release_cann_version }} documentation](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/{{ release_cann_version.replace('.', '') }}/softwareinst/releasenote/{{ release_cann_version }}/release-notes.md) | Required by CANN |
    | CANN | == {{ release_cann_version }} | Required by vLLM Ascend and TorchNPU |
    | TorchNPU | == {{ release_torch_npu_version }} | Required by vLLM Ascend; installed automatically in a later step |
    | PyTorch | == {{ release_pytorch_version }} | Required by TorchNPU and vLLM; installed automatically in a later step |
    | NNAL | == {{ release_nnal_version }} | Provides `libatb.so` and advanced tensor operations |
    | Triton Ascend | == {{ release_triton_ascend_version }} | Triton implementation for the Ascend platform |

=== "310P"

    | Software | Supported version | Description |
    |---------------|----------------------------------|-------------------------------------------|
    | Ascend HDK | See the [CANN {{ release_cann_version }} documentation](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/{{ release_cann_version.replace('.', '') }}/softwareinst/releasenote/{{ release_cann_version }}/release-notes.md) | Required by CANN |
    | CANN | == {{ release_cann_version }} | Required by vLLM Ascend and TorchNPU |
    | TorchNPU | == {{ release_torch_npu_version }} | Required by vLLM Ascend; installed automatically in a later step |
    | PyTorch | == {{ release_pytorch_version }} | Required by TorchNPU and vLLM; installed automatically in a later step |
    | NNAL | == {{ release_nnal_version }} | Provides `libatb.so` and advanced tensor operations |
    | Triton / Triton Ascend | Not supported | Triton implementation for the Ascend platform; not used by the 310P series |

???+ important "Install a compatible software stack"

    Treat vLLM Ascend, vLLM, PyTorch, TorchNPU, CANN, and Triton Ascend when applicable as one complete compatibility set. When installing a release, select an entire row from the [release compatibility matrix](../community/versioning_policy.md#release-compatibility-matrix). When developing against the main branch, use the exact vLLM commit recorded in `.github/vllm-main-verified.commit`; arbitrary vLLM tags or PyPI releases may have different transitive dependencies.

## Installation {: #installation }

### Set up the hardware environment {: #installation-hardware-environment }

First, run the following command to confirm that the Ascend NPU firmware and driver are installed correctly:

```bash
npu-smi info
```

For more information, see the [CANN installation resources](https://www.hiascend.com/cann/download?versionId=735&ids=d806%2Ch0501%2Ch0601%2Ch0702).

### Set up the software environment {: #installation-software-environment }

Choose one complete path based on your requirements.

| Requirement | Recommended method | Intended users |
| --- | --- | --- |
| Get a working environment as quickly as possible | **Prebuilt vLLM Ascend image** | Most users; recommended |
| Use a CANN image or an existing CANN environment | **Install in an existing CANN environment** | Users familiar with Python and CANN |
| Manage the complete user-space software stack | **Build from a base environment** | Advanced users and developers |

???+ tip "Which components do I need to install?"

    The software table is a compatibility reference, not a checklist that you must install manually before starting. Choose one installation path below and follow it from top to bottom without mixing steps from different paths.

    The software layers depend on each other as follows:

    1. The Ascend firmware and driver on the host make the NPU available to CANN.
    2. CANN provides the Toolkit and operator runtime. NNAL is an additional CANN package required by the vLLM Ascend runtime.
    3. PyTorch provides the tensor framework, and TorchNPU connects PyTorch to CANN.
    4. vLLM provides the inference engine, and vLLM Ascend connects vLLM to the Ascend software stack.
    5. Triton Ascend provides Ascend Triton kernels for A2, A3, and 950DT. It is not installed on 310P.

    What you install depends on the selected path:

    - **Prebuilt vLLM Ascend image:** Install or verify only the host firmware, driver, and Docker. The image already contains the compatible CANN user-space packages, NNAL, Python packages, vLLM, and vLLM Ascend.
    - **Existing CANN environment:** Prepare CANN and NNAL first. Do not manually install PyTorch, TorchNPU, or Triton before continuing; the pip or uv commands in this path install the pinned Python packages and the applicable Triton Ascend version.
    - **Base environment:** Install the host firmware and driver, then install CANN Toolkit, the hardware-specific operator package, and NNAL in the documented order. After that, the pip or uv commands install the pinned Python packages, vLLM, vLLM Ascend, and the applicable Triton Ascend version.

    If PyTorch, TorchNPU or Triton already exist in the environment, the selected installation process may replace them with compatible versions. Please complete all operations for the corresponding path before verifying that the environment configuration is correct and consistent.

{% include "getting_started/installation/prebuilt_image.inc.md" %}

{% include "getting_started/installation/existing_cann_environment.inc.md" %}

{% include "getting_started/installation/base_environment.inc.md" %}

### Verify the installation {: #installation-verification }

Go to [Quick Start > Inference](quick_start.md#quick-start-inference) and run a simple inference test to verify the installation.

## Additional guides {: #installation-more }

### CPU-only build verification {: #installation-cpu-build }

CPU-only build verification checks whether the Python package can be built without a visible Ascend device. It does not verify NPU runtime loading, inference examples, custom kernels, or NPU-specific tests. The build process needs access to CANN Toolkit headers and libraries, so CANN Toolkit must still be installed.

First, install the Python build backend and native build tools. Editable installations use setuptools-scm directly. If no compatible wheel is available, `arctic-inference` also requires CMake and Ninja:

```bash
python -m pip install --upgrade \
    pip "setuptools>=64" "setuptools-scm>=8" wheel \
    attrs googleapis-common-protos \
    "cmake>=3.26" ninja
```

This workflow verifies only the build and therefore does not install vLLM. To continue testing vLLM and vLLM Ascend together on the main branch, use the exact vLLM commit recorded in `.github/vllm-main-verified.commit` and verify the combined environment as described below.

In an x86 environment, install the CPU version of PyTorch from the PyTorch CPU index before installing the remaining Ascend dependencies:

```bash
python -m pip install \
    --index-url https://download.pytorch.org/whl/cpu/ \
    torch=={{ main_pytorch_version }} torchvision=={{ main_torchvision_version }} torchaudio=={{ main_torchaudio_version }}
python -m pip install \
    --extra-index-url https://mirrors.huaweicloud.com/ascend/repos/pypi \
    torch-npu=={{ main_torch_npu_version }} triton-ascend=={{ main_triton_ascend_version }}
python -m pip install \
    --extra-index-url https://mirrors.huaweicloud.com/ascend/repos/pypi \
    -r requirements.txt
```

Before building vLLM Ascend, explicitly set the build target and disable automatic device backend loading:

???+ important "Set the correct SOC_VERSION when no NPU is visible"

    If `npu-smi` is unavailable in the current environment, set `SOC_VERSION` for the target hardware before running `pip install -e .`:

    - A2: `export SOC_VERSION=ascend910b1`
    - A3: `export SOC_VERSION=ascend910_9391`
    - 310P: `export SOC_VERSION=ascend310p1`
    - 950DT: `export SOC_VERSION=ascend950dt_9582`

???+ tip "Enable batch invariance"

    To enable batch invariance, set `VLLM_BATCH_INVARIANT=1` before building vLLM Ascend so that the custom operator library for batch invariance is installed during installation. For usage instructions, see [Batch Invariance](../user_guide/feature_guide/batch_invariance.md).

```bash
export ASCEND_TOOLKIT_HOME="${ASCEND_TOOLKIT_HOME:-/usr/local/Ascend/ascend-toolkit/latest}"
export TORCH_DEVICE_BACKEND_AUTOLOAD=0
export COMPILE_CUSTOM_KERNELS=0
export SOC_VERSION=ascend910b1  # A2
python -m pip install \
    --no-build-isolation \
    --no-deps \
    --extra-index-url https://mirrors.huaweicloud.com/ascend/repos/pypi \
    -e .
```

The explicit build dependencies above and `requirements.txt` provide the complete build-system dependencies before the non-isolated editable build starts. `--no-build-isolation` only reuses packages in the current build environment; it cannot make incompatible vLLM, PyTorch, and TorchNPU versions compatible. Before using the environment for actual workloads, run `python -m pip check` and resolve all reported conflicts. If no device is available, skip the inference examples and NPU-specific tests.

???+ note

    Building custom operators requires gcc/g++ later than version 8 and C++17 or later. If you encounter a TorchNPU version conflict when running `pip install -e .`, use `pip install --no-build-isolation -e .` instead to build in the system environment.

    If you encounter other compilation issues, an unexpected compiler may be in use. Before compiling, set `CXX_COMPILER` and `C_COMPILER` to the locations of g++ and gcc, respectively.

### Multi-node deployment {: #installation-multi-node }

Check the physical links, the status of each node, and inter-node connectivity in order.

#### Physical link requirements {: #installation-multi-node-physical }

- The physical machines must be on the same LAN and able to communicate with each other.
- All NPUs must be connected through optical modules, and all connections must be healthy.

???+ important "950DT server precheck"

    This precheck applies only to 950DT servers. Other server series can skip it.

    **Prepare the HiXLEP configuration paths**:

    - When deploying a 950DT inference service, confirm on each server that `/lib/route.conf`, `/etc/hccl_rootinfo.json`, and the `/etc/hixlep` directory that describes the UB link topology exist and are configured correctly. If any item is missing or misconfigured, follow the [HiXLEP configuration file generation guide](https://gitcode.com/cann/hixl/wiki/A5%20LocalCommRes%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md) to generate the required content. Select the "D2D scenario" when generating `/etc/hixlep`.

#### Check each node {: #installation-multi-node-node-check }

Run the following commands on each node in order. The command results should be `success`, and the link status should be `UP`:

=== "A2"

    ```bash
    # Check the remote switch ports
    for i in {0..7}; do hccn_tool -i $i -lldp -g | grep Ifname; done
    # Get the link status of the Ethernet ports (UP or DOWN)
    for i in {0..7}; do hccn_tool -i $i -link -g ; done
    # Check the network health status
    for i in {0..7}; do hccn_tool -i $i -net_health -g ; done
    # View the network detected IP configuration
    for i in {0..7}; do hccn_tool -i $i -netdetect -g ; done
    # View gateway configuration
    for i in {0..7}; do hccn_tool -i $i -gateway -g ; done
    # View NPU network configuration
    cat /etc/hccn.conf
    ```

=== "A3"

    ```bash
    # Check the remote switch ports
    for i in {0..15}; do hccn_tool -i $i -lldp -g | grep Ifname; done
    # Get the link status of the Ethernet ports (UP or DOWN)
    for i in {0..15}; do hccn_tool -i $i -link -g ; done
    # Check the network health status
    for i in {0..15}; do hccn_tool -i $i -net_health -g ; done
    # View the network detected IP configuration
    for i in {0..15}; do hccn_tool -i $i -netdetect -g ; done
    # View gateway configuration
    for i in {0..15}; do hccn_tool -i $i -gateway -g ; done
    # View NPU network configuration
    cat /etc/hccn.conf
    ```

=== "950DT"

    ```bash
    # Check the remote switch ports
    for i in {0..7}; do hccn_tool -i $i -lldp -g | grep Ifname; done
    # Get the link status of the Ethernet ports (UP or DOWN)
    for i in {0..7}; do hccn_tool -i $i -link -g ; done
    # Check the network health status
    for i in {0..7}; do hccn_tool -i $i -net_health -g ; done
    # View the network detected IP configuration
    for i in {0..7}; do hccn_tool -i $i -netdetect -g ; done
    # View gateway configuration
    for i in {0..7}; do hccn_tool -i $i -gateway -g ; done
    # View NPU network configuration
    cat /etc/hccn.conf
    ```

#### Verify inter-node connectivity {: #installation-multi-node-interconnect }

##### Obtain NPU IP addresses {: #installation-multi-node-npu-ip }

=== "A2"

    ```bash
    for i in {0..7}; do hccn_tool -i $i -ip -g | grep ipaddr; done
    ```

=== "A3"

    ```bash
    for i in {0..15}; do hccn_tool -i $i -ip -g | grep ipaddr; done
    ```

=== "950DT"

    ```bash
    for i in {0..7}; do hccn_tool -i $i -ip -g | grep ipaddr; done
    ```

##### Run a cross-node ping test {: #installation-multi-node-ping }

```bash
# Execute on the target node (replace with actual IP)
hccn_tool -i 0 -ping -g address x.x.x.x
```

#### Start containers on each node {: #installation-multi-node-container }

- Use the official vLLM Ascend containers described in [Quick Start > Installation](quick_start.md#quick-start-installation) to quickly prepare consistent multi-node runtime environments.

- Commands for multi-node model serving are outside the scope of this installation guide. Continue with the relevant [Feature Tutorial](../tutorials/features/index.md) or [Model Tutorial](../tutorials/models/index.md).
