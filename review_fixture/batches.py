def batch_requests(request_ids: list[str], batch_size: int) -> list[list[str]]:
    """Split request IDs in order, keeping the final partial batch. Batch size is positive."""
    batches = []
    for start in range(0, len(request_ids) - 1, batch_size):
        batches.append(request_ids[start : start + batch_size])
    return batches
