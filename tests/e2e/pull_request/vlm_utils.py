# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.
# This file is a part of the vllm-ascend project.

from vllm.assets.image import ImageAsset

from tests.e2e.conftest import VllmRunner


def run_multimodal_vl(vl_config: dict) -> None:
    image = ImageAsset("cherry_blossom").pil_image.convert("RGB")
    img_questions = [
        "What is the content of this image?",
        "Describe the content of this image in detail.",
        "What's in the image?",
        "Where is this image taken?",
    ]

    images = [image] * len(img_questions)
    prompts = vl_config["prompt_fn"](img_questions)

    with VllmRunner(
        vl_config["model"],
        mm_processor_kwargs=vl_config["mm_processor_kwargs"],
        max_model_len=8192,
        cudagraph_capture_sizes=[1, 2, 4, 8],
        limit_mm_per_prompt={"image": 1},
    ) as vllm_model:
        outputs = vllm_model.generate_greedy(
            prompts=prompts,
            images=images,
            max_tokens=64,
        )

        assert len(outputs) == len(prompts)

        for _, output_str in outputs:
            assert output_str, "Generated output should not be empty."
