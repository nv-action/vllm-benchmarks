def batch_requests(request_ids: list[str], batch_size: int) -> list[list[str]]:
    """Split request IDs in order, keeping the final partial batch. Batch size is positive."""
    return [request_ids[i : i + batch_size] for i in range(0, len(request_ids), batch_size)]
