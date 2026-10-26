# Top30软件 功能测试报告

## 1. 概览

- 数据来源：鲲鹏920 取自 `FdeCad/boostkit-performance-evaluation` 的 `functional-test` 历史运行；鲲鹏950 取自 `nv-action/vllm-benchmarks` 的 `boostkit-performance-evaluation` 分支运行，以及 `FdeCad/boostkit-performance-evaluation` 的 `functional-test-temporary` 工作流运行。
- 统计口径：每个“软件 × 版本 × 测试OS”单元格取最近一次产出终态结果的运行；`未完成` 表示最近一次运行被取消或中断，没有产出最终结论。
- 统计时间：2026-10-26。
- 鲲鹏920 · openeuler-24.03-sp4：共 45 个单元格，通过 44，失败 0，未完成 0，未开始 1。
- 鲲鹏920 · ubuntu-26.04.1：共 45 个单元格，通过 39，失败 3，未完成 2，未开始 1。
- 鲲鹏950 · openeuler-24.03-sp4：共 45 个单元格，通过 33，失败 0，未完成 0，未开始 12。
- 鲲鹏950 · ubuntu-26.04.1：共 45 个单元格，通过 7，失败 2，未完成 2，未开始 34。

## 2. 测试结果

### 2.1 openeuler-24.03-sp4

| 软件 | 软件版本 | 测试用例 | 鲲鹏920 | 鲲鹏950 | 920 日志 | 950 日志 |
|---|---|---|---|---|---|---|
| faiss | 1.8.0 | version、index-flat-l2、index-ivf-pq | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191815238 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39812116980/job/119941988761 |
| faiss | 1.15.1 | version、index-flat-l2、index-ivf-pq | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191775847 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39906178119/job/120289646040 |
| hnswlib | 0.8.0 | version、hnsw-knn-roundtrip、hnsw-save-reload | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191775755 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39882723383/job/120183874629 |
| hnswlib | 0.9.0 | version、hnsw-knn-roundtrip、hnsw-save-reload | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191775789 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39906178119/job/120289700486 |
| openviking | 0.5.0 | version、agfs-readwrite、agfs-operations | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776468 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39906178119/job/120289758057 |
| pytorch | 2.14.1 | version、matmul-autograd、nn-training-loop | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776450 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39906178119/job/120289657750 |
| scann | 1.4.2 | version、scann-dot-product、scann-squared-l2 | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776395 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39906178119/job/120289671627 |
| bolt | 2d01261 | version、upstream-unit-test、link-smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39723027653/job/119640638442 | https://github.com/nv-action/vllm-benchmarks/actions/runs/40278794677/job/121427484929 |
| flink | 1.16.3 | version、upstream-example、cluster-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776684 | — |
| flink | 1.17.1 | version、upstream-example、cluster-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776824 | — |
| flink | 2.3.0 | version、upstream-example、cluster-smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776608 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39906178119/job/120289670836 |
| spark | 3.3.1 | version、upstream-example、pyspark-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191777624 | — |
| spark | 3.4.3 | version、upstream-example、pyspark-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778153 | — |
| spark | 3.5.2 | version、upstream-example、pyspark-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778100 | — |
| spark | 4.2.0 | version、upstream-example、pyspark-smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776751 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39906178119/job/120289581413 |
| mysql | 26.7.0 | version、upstream-test、sql-roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778019 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39906178119/job/120289431333 |
| oceanbase | 5.0.1 | version、upstream-test、sql-roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781671 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39906178119/job/120289431226 |
| redis | 6.0.20 | version、upstream-test、cli-roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781629 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39772730483/job/119804284863 |
| redis | 8.10.2 | version、upstream-test、cli-roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781592 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39931548689/job/120340836855 |
| rocksdb | 7.9.2 | version、upstream-test、api-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191779067 | — |
| rocksdb | 8.3.3 | version、upstream-test、api-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191779017 | — |
| rocksdb | 11.8.1 | version、upstream-test、api-smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778514 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39925701611/job/120322519163 |
| folly | 2026.10.12.00 | version、upstream-test、link-smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778909 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39933036899/job/120352163638 |
| lz4 | 1.9.3 | version、upstream-test、cli-roundtrip | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781583 | — |
| lz4 | 1.10.0 | version、upstream-test、cli-roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781644 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39930663799/job/120339038378 |
| protobuf | 36.2 | version、upstream-test、cpp-roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191779885 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39930465466/job/120337252029 |
| snappy | 1.1.7 | source-integrity、version、roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191779981 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39923102905/job/120315645731 |
| sonic-cpp | 1.0.2 | version、upstream-test、json-roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/40271028822/job/121403717469 | https://github.com/nv-action/vllm-benchmarks/actions/runs/40281697093/job/121436461479 |
| zstd | 1.5.7 | version、upstream-test、cli-roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/40265906007/job/121388209772 | https://github.com/nv-action/vllm-benchmarks/actions/runs/40279106424/job/121428413946 |
| x264 | 0480cb05 | version、upstream-checkasm、encode-smoke、decoder-dependencies、decode-check | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780247 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39921590280/job/120307924666 |
| x265 | 4.2 | version、quality-metrics、encode-smoke、decoder-dependencies、decode-check | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780667 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39922024879/job/120317544664 |
| brpc | 1.18.0 | version、upstream-test、cpp-smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780809 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39929802722/job/120335046305 |
| cloudwego | 0.16.4 | source-integrity、version、upstream-test、smoke、codegen、generated-build、kitex-rpc、eino-test | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780733 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39936019631/job/120356077024 |
| envoy | 1.37.1 | version、upstream-test、proxy-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/40283509141/job/121441864643 | — |
| envoy | 1.40.0 | version、upstream-test、proxy-smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/40283509141/job/121441864685 | https://github.com/nv-action/vllm-benchmarks/actions/runs/40282808541/job/121439881353 |
| kubernetes | 1.28.4 | version、upstream-test、kubectl-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781347 | — |
| kubernetes | 1.37.1 | version、upstream-test、kubectl-smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780936 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39924948769/job/120347431065 |
| ceph | 21.3.0 | version、upstream-test、cluster-smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/40265389196/job/121386606500 | https://github.com/nv-action/vllm-benchmarks/actions/runs/40271186484/job/121404247374 |
| ceph | 16.2.8 | version、upstream-test、cluster-smoke | 未开始 | 未开始 | — | — |
| gcc | 16.2.0 | version、upstream-test、smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781798 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39924948769/job/120347441812 |
| glibc | 2.34 | version、upstream-test、smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191784109 | — |
| glibc | 2.43 | version、upstream-test、smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781858 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39927884162/job/120331555747 |
| golang | 1.27.1 | version、upstream-test、smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191782286 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39927003596/job/120326354465 |
| openjdk | 28_19 | version、upstream-test、smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191783014 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39916091683/job/120295222013 |
| python | 3.15.0 | version、upstream-test、smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191782485 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39916091683/job/120295288679 |

### 2.2 ubuntu-26.04.1

| 软件 | 软件版本 | 测试用例 | 鲲鹏920 | 鲲鹏950 | 920 日志 | 950 日志 |
|---|---|---|---|---|---|---|
| faiss | 1.8.0 | version、index-flat-l2、index-ivf-pq | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191775553 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39812116980/job/119941988713 |
| faiss | 1.15.1 | version、index-flat-l2、index-ivf-pq | ✅ 通过 | ❌ 失败 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191812148 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39885835774/job/120203362322 |
| hnswlib | 0.8.0 | version、hnsw-knn-roundtrip、hnsw-save-reload | ✅ 通过 | ⚠️ 未完成 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191775848 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39882723383/job/120183874733 |
| hnswlib | 0.9.0 | version、hnsw-knn-roundtrip、hnsw-save-reload | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191775685 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39882723383/job/120183874514 |
| openviking | 0.5.0 | version、agfs-readwrite、agfs-operations | ✅ 通过 | ❌ 失败 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776462 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39889376699/job/120209924384 |
| pytorch | 2.14.1 | version、matmul-autograd、nn-training-loop | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776517 | — |
| scann | 1.4.2 | version、scann-dot-product、scann-squared-l2 | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191777082 | — |
| bolt | 2d01261 | version、upstream-unit-test、link-smoke | ❌ 失败 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39738849015/job/119690973898 | — |
| flink | 1.16.3 | version、upstream-example、cluster-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191777028 | — |
| flink | 1.17.1 | version、upstream-example、cluster-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191777207 | — |
| flink | 2.3.0 | version、upstream-example、cluster-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191776620 | — |
| spark | 3.3.1 | version、upstream-example、pyspark-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191777560 | — |
| spark | 3.4.3 | version、upstream-example、pyspark-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778144 | — |
| spark | 3.5.2 | version、upstream-example、pyspark-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778489 | — |
| spark | 4.2.0 | version、upstream-example、pyspark-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191777531 | — |
| mysql | 26.7.0 | version、upstream-test、sql-roundtrip | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191779358 | — |
| oceanbase | 5.0.1 | version、upstream-test、sql-roundtrip | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778805 | — |
| redis | 6.0.20 | version、upstream-test、cli-roundtrip | ✅ 通过 | ⚠️ 未完成 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778498 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39768421730/job/119789854128 |
| redis | 8.10.2 | version、upstream-test、cli-roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778185 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39775048983/job/119812216383 |
| rocksdb | 7.9.2 | version、upstream-test、api-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191779070 | — |
| rocksdb | 8.3.3 | version、upstream-test、api-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191779302 | — |
| rocksdb | 11.8.1 | version、upstream-test、api-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191779186 | — |
| folly | 2026.10.12.00 | version、upstream-test、link-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191778946 | — |
| lz4 | 1.9.3 | version、upstream-test、cli-roundtrip | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780409 | — |
| lz4 | 1.10.0 | version、upstream-test、cli-roundtrip | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191779311 | — |
| protobuf | 36.2 | version、upstream-test、cpp-roundtrip | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780594 | — |
| snappy | 1.1.7 | source-integrity、version、roundtrip | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780340 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39923102905/job/120315645883 |
| sonic-cpp | 1.0.2 | version、upstream-test、json-roundtrip | ⚠️ 未完成 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/38887050842/job/116927295878 | — |
| zstd | 1.5.7 | version、upstream-test、cli-roundtrip | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/38887050842/job/116927296126 | — |
| x264 | 0480cb05 | version、upstream-checkasm、encode-smoke、decoder-dependencies、decode-check | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780805 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39921590280/job/120307924589 |
| x265 | 4.2 | version、quality-metrics、encode-smoke、decoder-dependencies、decode-check | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781255 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39922024879/job/120317546386 |
| brpc | 1.18.0 | version、upstream-test、cpp-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781195 | — |
| cloudwego | 0.16.4 | source-integrity、version、upstream-test、smoke、codegen、generated-build、kitex-rpc、eino-test | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191780767 | — |
| envoy | 1.37.1 | version、upstream-test、proxy-smoke | ❌ 失败 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39622847513/job/119300574024 | — |
| envoy | 1.40.0 | version、upstream-test、proxy-smoke | ❌ 失败 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39622847513/job/119300574077 | — |
| kubernetes | 1.28.4 | version、upstream-test、kubectl-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191782210 | — |
| kubernetes | 1.37.1 | version、upstream-test、kubectl-smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781425 | — |
| ceph | 21.3.0 | version、upstream-test、cluster-smoke | ⚠️ 未完成 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/38887050842/job/116927298771 | — |
| ceph | 16.2.8 | version、upstream-test、cluster-smoke | 未开始 | 未开始 | — | — |
| gcc | 16.2.0 | version、upstream-test、smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781863 | — |
| glibc | 2.34 | version、upstream-test、smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781801 | — |
| glibc | 2.43 | version、upstream-test、smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191781508 | — |
| golang | 1.27.1 | version、upstream-test、smoke | ✅ 通过 | ✅ 通过 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191782495 | https://github.com/nv-action/vllm-benchmarks/actions/runs/39803138038/job/119909478849 |
| openjdk | 28_19 | version、upstream-test、smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191782279 | — |
| python | 3.15.0 | version、upstream-test、smoke | ✅ 通过 | 未开始 | https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39784091800/job/120191783044 | — |


## 3. 测试用例与描述

| 软件 | 测试用例 | 测试用例描述 |
|---|---|---|
| faiss | version | 已安装 faiss 版本必须等于请求版本 |
| faiss | index-flat-l2 | 构建 IndexFlatL2,add 向量,search 返回最近邻并校验与暴力计算一致 |
| faiss | index-ivf-pq | 训练 IndexIVFPQ 量化索引,add 向量并检索,校验训练/入库状态与邻居合法性 |
| hnswlib | version | 安装的 hnswlib 头文件必须是请求版本(整套头文件的聚合 SHA-256 指纹比对) |
| hnswlib | hnsw-knn-roundtrip | g++ 编译并运行 HNSW addPoint/searchKnn 回环,校验自查询全命中与插值查询标签 |
| hnswlib | hnsw-save-reload | 索引 saveIndex 落盘后重新加载,校验加载后的检索结果一致 |
| openviking | version | 已安装 openviking 版本必须等于请求版本 |
| openviking | agfs-readwrite | AGFS 挂载 memfs 后写入并回读固定负载,校验逐字节一致 |
| openviking | agfs-operations | AGFS stat/ls/mkdir/grep/rm 操作序列功能验证 |
| pytorch | version | 已安装 torch 版本(裁掉 +cpu 后缀)必须等于请求版本 |
| pytorch | matmul-autograd | 张量矩阵乘 + autograd 反向回环,校验梯度与解析解一致 |
| pytorch | nn-training-loop | 小型全连接网络 SGD 训练回环,校验损失下降 |
| scann | version | 已安装 scann 版本必须等于请求版本 |
| scann | scann-dot-product | dot_product 距离建索引并检索,校验返回形状与邻居合法性 |
| scann | scann-squared-l2 | squared_l2 距离建索引并检索,校验返回形状一致、邻居合法与距离升序 |
| bolt | version | 校验源码 commit / 镜像内 BUILD_VERSION 等于请求版本(短 sha 与完整 sha 互为前缀即可匹配) |
| bolt | upstream-unit-test | 运行上游自带 gtest 单测子集(ctest -R 过滤);build 模式经上游 release_with_test 目标编译测试后真实执行(0 测试视为构建异常),prebuilt 镜像无测试树时记 skipped |
| bolt | link-smoke | 最小可用性冒烟,校验构建产物 libbolt*.so 可被动态链接器加载 |
| flink | version | bin/flink --version 必须等于请求版本 |
| flink | upstream-example | 运行官方发行版自带的 WordCount 示例并断言作业成功结束 |
| flink | cluster-smoke | 启动本地 standalone mini-cluster(JobManager+TaskManager)并校验 TaskManager 已注册 |
| spark | version | bin/spark-submit --version 必须等于请求版本 |
| spark | upstream-example | 运行官方发行版自带的 SparkPi 示例并断言作业成功输出 Pi 估计值 |
| spark | pyspark-smoke | 用 pyspark 断言 spark.range(10).count() == 10;Spark<3.5 且容器 Python≥3.13 时跳过(3.3/3.4 的 pyspark 依赖 typing.io,Python 3.13 起移除,上游 v3.5.2 起不再使用),状态记为 skipped 不影响整体通过 |
| mysql | version | 被测 mysqld 版本必须等于请求版本 |
| mysql | upstream-test | 使用上游 mysqlslap 客户端施加并发 SQL 负载 |
| mysql | sql-roundtrip | mysqld 初始化 + 建库建表,INSERT/SELECT 回环校验 |
| oceanbase | version | observer 的 SELECT version() 必须等于请求版本 |
| oceanbase | upstream-test | 使用上游 sysbench 对 observer 执行 oltp_point_select 点查烟测 |
| oceanbase | sql-roundtrip | 建库建表,INSERT/SELECT 回环校验 |
| redis | version | 构建产物版本必须等于请求版本 |
| redis | upstream-test | 运行上游 Redis Tcl 测试套件 unit/type/string |
| redis | cli-roundtrip | redis-server/redis-cli SET/GET 回环功能验证 |
| rocksdb | version | rocksdb/version.h 中的版本必须等于请求版本 |
| rocksdb | upstream-test | 运行上游 db_bench 官方工具做写入 + 只读烟测 |
| rocksdb | api-smoke | 编译并运行最小 C++ 程序验证 Open/Put/Get/reopen |
| folly | version | 检出源码的 tag 必须等于请求版本(v<version>) |
| folly | upstream-test | 编译并运行使用 folly 公共 API 的程序 |
| folly | link-smoke | 最小头文件包含 + 链接 libfolly 的可用性验证 |
| lz4 | version | 被测 lz4 版本必须等于请求版本 |
| lz4 | upstream-test | 运行上游 lz4 tests/Makefile 的核心回归子集 |
| lz4 | cli-roundtrip | lz4 CLI 压缩/解压回环并按 sha256 校验内容一致 |
| protobuf | version | 分层获取后 protoc 版本必须严格等于请求版本 |
| protobuf | upstream-test | protoc 编译并回解上游 descriptor.proto(frontend 自洽) |
| protobuf | cpp-roundtrip | protoc 生成 C++ 代码后编译链接 libprotobuf 做序列化/反序列化回环 |
| snappy | source-integrity | 固定 tag 的源码归档 SHA-256 必须匹配 source-lock.json 基线 |
| snappy | version | 被测 SnappyConfigVersion.cmake 报告的版本必须等于请求版本,且带版本号的库文件存在 |
| snappy | roundtrip | 链接被测 libsnappy 的 C++ 程序做 Compress/Uncompress 回环校验 |
| sonic-cpp | version | 检出源码的提交号必须等于请求的提交 |
| sonic-cpp | upstream-test | 运行上游官方 CMake benchmark 目标(bench,短时) |
| sonic-cpp | json-roundtrip | 链接 libsonic 的 C++ 程序做 JSON 解析/序列化回环校验 |
| zstd | version | 被测 zstd 版本必须等于请求版本 |
| zstd | upstream-test | 运行上游 zstd tests/playTests.sh 短测套件 |
| zstd | cli-roundtrip | zstd CLI 压缩/解压回环并按 sha256 校验内容一致 |
| x264 | version | 校验固定源码归档及对应 CLI 版本，分别记录提交号和实际版本 |
| x264 | upstream-checkasm | 运行上游 checkasm 汇编一致性测试 |
| x264 | encode-smoke | 生成裸 YUV 并用 x264 CLI 编码成码流,校验码流含 NAL 起始码 |
| x264 | decoder-dependencies | 按需安装 FFmpeg，依赖不可用则失败 |
| x264 | decode-check | H.264 64x64，完整解码 3 帧，I420 输出 18432 字节 |
| x265 | version | x265 --version 的版本号必须等于请求版本 |
| x265 | quality-metrics | 编码 3 帧，校验 PSNR 为有限正数、SSIM 为 [0,1] 内有限数 |
| x265 | encode-smoke | 生成裸 YUV 并用 x265 CLI 编码成码流,校验码流含 NAL 起始码 |
| x265 | decoder-dependencies | 按需安装 FFmpeg，ffmpeg 和 ffprobe 必须可用，安装失败不跳过 |
| x265 | decode-check | 完整解码无错误，HEVC 64x64，3 帧，I420 输出 18432 字节 |
| brpc | version | 源码 RELEASE_VERSION/镜像内版本必须等于请求版本 |
| brpc | upstream-test | 编译并运行上游 example/echo_c++ 的 server/client 完成一次 RPC 回环 |
| brpc | cpp-smoke | 编译链接 libbrpc 的最小程序,验证头文件与静态库可用 |
| cloudwego | source-integrity | 四个固定 tag 的源码归档 SHA-256 必须匹配仓库内基线 |
| cloudwego | version | 检查实际 Kitex、hz、volo CLI 版本及 Eino 源码标记 |
| cloudwego | upstream-test | 实际运行 Kitex client 和 Hertz pkg 的上游测试，禁用测试缓存 |
| cloudwego | smoke | Hertz 最小 HTTP 服务回环，校验状态码及 JSON 响应 |
| cloudwego | codegen | 使用实际 Kitex、hz、volo 工具处理本地 Thrift IDL 并检查生成文件 |
| cloudwego | generated-build | Kitex、hz 生成代码执行 go build，Volo 生成项目执行 cargo build --locked |
| cloudwego | kitex-rpc | 使用生成的服务代码启动 Kitex TCP 服务并校验三种请求的精确响应 |
| cloudwego | eino-test | 运行 Eino schema 和 prompt 的离线单元测试 |
| envoy | version | envoy --version 的版本号必须等于请求版本 |
| envoy | upstream-test | 用 envoy --mode validate 校验一份最小静态配置 |
| envoy | proxy-smoke | 启动 envoy 实例,探活 admin /ready 与 listener 直答 200 ok |
| kubernetes | version | kubectl version --client 的客户端版本必须等于请求版本 |
| kubernetes | upstream-test | 用 kubectl create deployment --dry-run=client 离线渲染 Deployment 并校验字段 |
| kubernetes | kubectl-smoke | kubectl create 离线渲染 ConfigMap 与 Job 的 YAML 并校验类型和名称 |
| ceph | version | ceph --version 必须等于请求版本 |
| ceph | upstream-test | 运行上游自带离线自检工具 ceph-dencoder 与 monmaptool |
| ceph | cluster-smoke | 最小可用性冒烟,校验 ceph-mon/ceph-osd/ceph-mgr 二进制可执行(不引导完整集群) |
| gcc | version | 获取到的 gcc 版本必须等于请求版本(层1/2/2b/3 任一来源均须匹配) |
| gcc | upstream-test | 编译并运行一个 C 程序(gcc 功能验证) |
| gcc | smoke | 编译并运行一个 C++ 程序(g++ 功能验证) |
| glibc | version | 获取到的 glibc 版本必须等于请求版本(ldd/加载器输出,层1/2/2b/3 任一来源均须匹配) |
| glibc | upstream-test | 编译并运行一个使用 libc 的程序(memcpy/qsort/malloc) |
| glibc | smoke | getconf GNU_LIBC_VERSION 报告 glibc 版本 |
| golang | version | 安装的 Go 版本必须等于请求版本(ls `go version`,层1/2/3 任一来源均须匹配) |
| golang | upstream-test | 运行 GOROOT 自带 strings 标准库测试 |
| golang | smoke | go build 一个小模块并运行,校验 hello 输出 |
| openjdk | version | java -version 的 feature 号必须等于请求版本的 feature 号 |
| openjdk | upstream-test | javac 编译并 java 运行一个 Hello 程序 |
| openjdk | smoke | java 运行一个使用 java.util.stream 的程序 |
| python | version | 获取到的 CPython 解释器版本必须等于请求版本(层1/2/3/4 任一来源均须匹配) |
| python | upstream-test | 运行 CPython 官方回归测试子集(test_math / test_json) |
| python | smoke | compileall 临时目录 + 运行内建模块断言 |
