from enum import Enum


class FullDataServing(str, Enum):
    MEAN_TTFT_MS = 'mean_ttft_ms'
    MEDIAN_TTFT_MS = 'median_ttft_ms'
    P99_TTFT_MS = 'p99_ttft_ms'
    MEAN_ITL_MS = 'mean_itl_ms'
    MEDIAN_ITL_MS = 'median_itl_ms'
    P99_ITL_MS = 'p99_itl_ms'
