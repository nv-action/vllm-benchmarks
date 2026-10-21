"""Short inference check for the private A2 controller-runner validation."""

from concurrent.futures import ThreadPoolExecutor

SMOKE_REQUESTS = 4
SMOKE_MAX_TOKENS = 32


def run_inference_smoke(client, model: str) -> None:
    def generate(index: int) -> None:
        response = client.completions.create(
            model=model,
            prompt=f"Question {index + 1}: What is the capital of France? Answer briefly.\nAnswer:",
            max_tokens=SMOKE_MAX_TOKENS,
            temperature=0,
        )
        assert response.choices, "Inference returned no choices"
        choice = response.choices[0]
        assert choice.text.strip(), "Inference returned empty text"
        assert choice.finish_reason in ("stop", "length"), f"Unexpected finish reason: {choice.finish_reason}"
        print(f"[Smoke] request={index + 1} finish={choice.finish_reason} text={choice.text!r}", flush=True)

    with ThreadPoolExecutor(max_workers=SMOKE_REQUESTS) as executor:
        list(executor.map(generate, range(SMOKE_REQUESTS)))
    print(f"[Smoke] PASS: {SMOKE_REQUESTS} inference requests completed", flush=True)
