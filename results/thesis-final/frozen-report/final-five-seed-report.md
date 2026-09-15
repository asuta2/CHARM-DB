# Protocol-v2 final five-seed primary comparison report

- Analysis payload SHA-256: `769f5a0eabdebe272a0a8ae4b7578fd54f72d4a95cd53822e1cb993466a36fb2`
- Benchmark profile: `scale500-c32-w600-f3-600-v1`
- Hypervolume reference: `(0.0, -40.0)`
- Outcome: **COMPLETE_WITH_DRIFT_FLAGS**

This is the unified 5-seed primary cohort. Seed is the independent unit; candidate rows are not independent replicates. The PostgreSQL default is an interleaved reference, not a method row, and consumes no candidate slot.

## Method outcomes

| label | logical_slots | valid_candidate_observations | candidate_failed_slots | infrastructure_exhausted_slots | retained_infrastructure_attempts | mean_final_hypervolume | mean_seed_best_throughput_tps | mean_seed_minimum_p99_ms | mean_control_relative_tps | mean_control_relative_p99_ms |
|---|---|---|---|---|---|---|---|---|---|---|
| Random | 150 | 150 | 0 | 0 | 150 | 40228.2259 | 3003.9302 | 26.6118 | -0.0832 | 1.3036 |
| Sobol | 150 | 150 | 0 | 0 | 150 | 40426.2366 | 3049.6245 | 26.7372 | -0.0741 | 1.3639 |
| qLogNEI (throughput) | 150 | 150 | 0 | 0 | 152 | 43053.6255 | 3059.5359 | 25.9258 | -0.0027 | -1.3952 |
| qLogNParEGO (multi-objective) | 150 | 150 | 0 | 0 | 152 | 43343.3857 | 3064.2302 | 25.8519 | -0.0027 | -1.5303 |
| qLogNEHVI (multi-objective) | 150 | 150 | 0 | 0 | 152 | 42322.6535 | 3053.6929 | 26.1378 | -0.0095 | -1.2321 |

## Seed-level endpoint summaries

| metric | direction | method | n | mean | median | sd | mad | bootstrap_ci_95 |
|---|---|---|---|---|---|---|---|---|
| hypervolume_at_0_negative_40 | higher-is-better | Random | 5 | 40228.2259 | 39899.5597 | 2521.2426 | 1747.505 | [38257.016,42285.2259] |
| hypervolume_at_0_negative_40 | higher-is-better | Sobol | 5 | 40426.2366 | 40436.3097 | 847.2542 | 606.7722 | [39867.35,41131.6803] |
| hypervolume_at_0_negative_40 | higher-is-better | qLogNEI (throughput) | 5 | 43053.6255 | 42502.7422 | 1183.3276 | 677.3009 | [42149.5245,43957.7264] |
| hypervolume_at_0_negative_40 | higher-is-better | qLogNParEGO (multi-objective) | 5 | 43343.3857 | 43586.258 | 658.7213 | 555.0164 | [42822.5389,43837.1608] |
| hypervolume_at_0_negative_40 | higher-is-better | qLogNEHVI (multi-objective) | 5 | 42322.6535 | 41912.8268 | 1733.2658 | 1672.9874 | [41048.1816,43706.7111] |
| best_throughput_tps | higher-is-better | Random | 5 | 3003.9302 | 2996.6229 | 59.5882 | 18.5332 | [2956.7675,3053.0467] |
| best_throughput_tps | higher-is-better | Sobol | 5 | 3049.6245 | 3057.9588 | 30.8045 | 24.5863 | [3024.2879,3072.8354] |
| best_throughput_tps | higher-is-better | qLogNEI (throughput) | 5 | 3059.5359 | 3056.0129 | 34.7608 | 20.7532 | [3035.1144,3088.8953] |
| best_throughput_tps | higher-is-better | qLogNParEGO (multi-objective) | 5 | 3064.2302 | 3053.5497 | 28.4554 | 8.1762 | [3047.5942,3089.6187] |
| best_throughput_tps | higher-is-better | qLogNEHVI (multi-objective) | 5 | 3053.6929 | 3033.1501 | 37.3152 | 8.6303 | [3028.6672,3086.4142] |
| minimum_p99_ms | lower-is-better | Random | 5 | 26.6118 | 26.767 | 0.6111 | 0.6613 | [26.1382,27.0639] |
| minimum_p99_ms | lower-is-better | Sobol | 5 | 26.7372 | 26.7664 | 0.3253 | 0.1126 | [26.4521,26.9601] |
| minimum_p99_ms | lower-is-better | qLogNEI (throughput) | 5 | 25.9258 | 25.997 | 0.2671 | 0.171 | [25.7157,26.1301] |
| minimum_p99_ms | lower-is-better | qLogNParEGO (multi-objective) | 5 | 25.8519 | 25.816 | 0.1631 | 0.1346 | [25.723,25.9808] |
| minimum_p99_ms | lower-is-better | qLogNEHVI (multi-objective) | 5 | 26.1378 | 26.168 | 0.4372 | 0.386 | [25.8344,26.4878] |
| mean_control_relative_tps | higher-is-better | Random | 5 | -0.0832 | -0.083 | 0.0167 | 0.0122 | [-0.0957,-0.0701] |
| mean_control_relative_tps | higher-is-better | Sobol | 5 | -0.0741 | -0.0756 | 0.0053 | 0.0045 | [-0.0783,-0.07] |
| mean_control_relative_tps | higher-is-better | qLogNEI (throughput) | 5 | -0.0027 | -0.0058 | 0.0071 | 0.0037 | [-0.0081,0.0028] |
| mean_control_relative_tps | higher-is-better | qLogNParEGO (multi-objective) | 5 | -0.0027 | -0.004 | 0.0092 | 0.0087 | [-0.01,0.0046] |
| mean_control_relative_tps | higher-is-better | qLogNEHVI (multi-objective) | 5 | -0.0095 | -0.0041 | 0.0123 | 0.0032 | [-0.0205,-0.002] |
| mean_control_relative_p99_ms | lower-is-better | Random | 5 | 1.3036 | 1.3679 | 0.7126 | 0.286 | [0.7336,1.8612] |
| mean_control_relative_p99_ms | lower-is-better | Sobol | 5 | 1.3639 | 1.3495 | 0.6335 | 0.4554 | [0.8779,1.85] |
| mean_control_relative_p99_ms | lower-is-better | qLogNEI (throughput) | 5 | -1.3952 | -1.106 | 0.8066 | 0.2019 | [-2.1201,-0.9246] |
| mean_control_relative_p99_ms | lower-is-better | qLogNParEGO (multi-objective) | 5 | -1.5303 | -1.2545 | 0.8494 | 0.1617 | [-2.2753,-1.027] |
| mean_control_relative_p99_ms | lower-is-better | qLogNEHVI (multi-objective) | 5 | -1.2321 | -1.2437 | 0.8619 | 0.1318 | [-1.9671,-0.5353] |

## Pairwise method contrasts

| metric | baseline | treatment | mean_difference | cliffs_delta | permutation_p | holm_p |
|---|---|---|---|---|---|---|
| hypervolume_at_0_negative_40 | Random | Sobol | 198.0106 | 0.12 | 1.0 | 1.0 |
| hypervolume_at_0_negative_40 | Random | qLogNEI (throughput) | 2825.3995 | 0.76 | 0.0625 | 0.625 |
| hypervolume_at_0_negative_40 | Random | qLogNParEGO (multi-objective) | 3115.1598 | 0.84 | 0.0625 | 0.625 |
| hypervolume_at_0_negative_40 | Random | qLogNEHVI (multi-objective) | 2094.4275 | 0.68 | 0.0625 | 0.625 |
| hypervolume_at_0_negative_40 | Sobol | qLogNEI (throughput) | 2627.3889 | 1.0 | 0.0625 | 0.625 |
| hypervolume_at_0_negative_40 | Sobol | qLogNParEGO (multi-objective) | 2917.1491 | 1.0 | 0.0625 | 0.625 |
| hypervolume_at_0_negative_40 | Sobol | qLogNEHVI (multi-objective) | 1896.4169 | 0.76 | 0.0625 | 0.625 |
| hypervolume_at_0_negative_40 | qLogNEI (throughput) | qLogNParEGO (multi-objective) | 289.7602 | 0.2 | 0.625 | 1.0 |
| hypervolume_at_0_negative_40 | qLogNEI (throughput) | qLogNEHVI (multi-objective) | -730.972 | -0.2 | 0.75 | 1.0 |
| hypervolume_at_0_negative_40 | qLogNParEGO (multi-objective) | qLogNEHVI (multi-objective) | -1020.7322 | -0.44 | 0.1875 | 0.75 |
| best_throughput_tps | Random | Sobol | 45.6942 | 0.52 | 0.25 | 1.0 |
| best_throughput_tps | Random | qLogNEI (throughput) | 55.6057 | 0.68 | 0.0625 | 0.625 |
| best_throughput_tps | Random | qLogNParEGO (multi-objective) | 60.3 | 0.68 | 0.0625 | 0.625 |
| best_throughput_tps | Random | qLogNEHVI (multi-objective) | 49.7627 | 0.68 | 0.0625 | 0.625 |
| best_throughput_tps | Sobol | qLogNEI (throughput) | 9.9115 | 0.04 | 0.625 | 1.0 |
| best_throughput_tps | Sobol | qLogNParEGO (multi-objective) | 14.6057 | 0.12 | 0.4375 | 1.0 |
| best_throughput_tps | Sobol | qLogNEHVI (multi-objective) | 4.0685 | 0.04 | 0.8125 | 1.0 |
| best_throughput_tps | qLogNEI (throughput) | qLogNParEGO (multi-objective) | 4.6943 | 0.08 | 0.5 | 1.0 |
| best_throughput_tps | qLogNEI (throughput) | qLogNEHVI (multi-objective) | -5.843 | -0.2 | 0.75 | 1.0 |
| best_throughput_tps | qLogNParEGO (multi-objective) | qLogNEHVI (multi-objective) | -10.5373 | -0.32 | 0.25 | 1.0 |
| minimum_p99_ms | Random | Sobol | 0.1255 | 0.12 | 1.0 | 1.0 |
| minimum_p99_ms | Random | qLogNEI (throughput) | -0.686 | -0.6 | 0.125 | 0.875 |
| minimum_p99_ms | Random | qLogNParEGO (multi-objective) | -0.7599 | -0.84 | 0.0625 | 0.625 |
| minimum_p99_ms | Random | qLogNEHVI (multi-objective) | -0.474 | -0.44 | 0.25 | 1.0 |
| minimum_p99_ms | Sobol | qLogNEI (throughput) | -0.8114 | -1.0 | 0.0625 | 0.625 |
| minimum_p99_ms | Sobol | qLogNParEGO (multi-objective) | -0.8853 | -1.0 | 0.0625 | 0.625 |
| minimum_p99_ms | Sobol | qLogNEHVI (multi-objective) | -0.5994 | -0.76 | 0.1875 | 1.0 |
| minimum_p99_ms | qLogNEI (throughput) | qLogNParEGO (multi-objective) | -0.0739 | -0.2 | 0.75 | 1.0 |
| minimum_p99_ms | qLogNEI (throughput) | qLogNEHVI (multi-objective) | 0.212 | 0.4 | 0.75 | 1.0 |
| minimum_p99_ms | qLogNParEGO (multi-objective) | qLogNEHVI (multi-objective) | 0.2859 | 0.52 | 0.1875 | 1.0 |
| mean_control_relative_tps | Random | Sobol | 0.0091 | 0.36 | 0.375 | 0.75 |
| mean_control_relative_tps | Random | qLogNEI (throughput) | 0.0806 | 1.0 | 0.0625 | 0.625 |
| mean_control_relative_tps | Random | qLogNParEGO (multi-objective) | 0.0805 | 1.0 | 0.0625 | 0.625 |
| mean_control_relative_tps | Random | qLogNEHVI (multi-objective) | 0.0738 | 1.0 | 0.0625 | 0.625 |
| mean_control_relative_tps | Sobol | qLogNEI (throughput) | 0.0715 | 1.0 | 0.0625 | 0.625 |
| mean_control_relative_tps | Sobol | qLogNParEGO (multi-objective) | 0.0714 | 1.0 | 0.0625 | 0.625 |
| mean_control_relative_tps | Sobol | qLogNEHVI (multi-objective) | 0.0647 | 1.0 | 0.0625 | 0.625 |
| mean_control_relative_tps | qLogNEI (throughput) | qLogNParEGO (multi-objective) | -0.0 | -0.04 | 0.9375 | 0.9375 |
| mean_control_relative_tps | qLogNEI (throughput) | qLogNEHVI (multi-objective) | -0.0068 | -0.28 | 0.125 | 0.625 |
| mean_control_relative_tps | qLogNParEGO (multi-objective) | qLogNEHVI (multi-objective) | -0.0067 | -0.2 | 0.1875 | 0.625 |
| mean_control_relative_p99_ms | Random | Sobol | 0.0604 | -0.04 | 0.8125 | 1.0 |
| mean_control_relative_p99_ms | Random | qLogNEI (throughput) | -2.6987 | -1.0 | 0.0625 | 0.625 |
| mean_control_relative_p99_ms | Random | qLogNParEGO (multi-objective) | -2.8339 | -1.0 | 0.0625 | 0.625 |
| mean_control_relative_p99_ms | Random | qLogNEHVI (multi-objective) | -2.5357 | -1.0 | 0.0625 | 0.625 |
| mean_control_relative_p99_ms | Sobol | qLogNEI (throughput) | -2.7591 | -1.0 | 0.0625 | 0.625 |
| mean_control_relative_p99_ms | Sobol | qLogNParEGO (multi-objective) | -2.8942 | -1.0 | 0.0625 | 0.625 |
| mean_control_relative_p99_ms | Sobol | qLogNEHVI (multi-objective) | -2.596 | -1.0 | 0.0625 | 0.625 |
| mean_control_relative_p99_ms | qLogNEI (throughput) | qLogNParEGO (multi-objective) | -0.1351 | -0.2 | 0.375 | 1.0 |
| mean_control_relative_p99_ms | qLogNEI (throughput) | qLogNEHVI (multi-objective) | 0.1631 | -0.04 | 0.625 | 1.0 |
| mean_control_relative_p99_ms | qLogNParEGO (multi-objective) | qLogNEHVI (multi-objective) | 0.2982 | 0.12 | 0.375 | 1.0 |

## Default-control drift

| seed | valid_controls | fitted_tps_change | absolute_fitted_tps_change_relative_to_control_mean | fitted_p99_change_ms | flagged | campaign_killing |
|---|---|---|---|---|---|---|
| 88408573 | 5 | -165.0558 | 0.0575 | 3.4286 | true | false |
| 642754166 | 5 | -121.3495 | 0.0421 | 1.8463 | false | false |
| 740267717 | 5 | -107.902 | 0.0383 | 9.0024 | true | false |
| 1418705027 | 5 | 35.6929 | 0.0124 | -1.8803 | false | false |
| 1902413987 | 5 | -45.7703 | 0.0159 | 2.2677 | false | false |

## Safety and failure accounting

| method | logical_slots | valid_candidate_observations | completed_but_invalid_slots | candidate_failed_slots | infrastructure_exhausted_slots | retained_infrastructure_attempts | slots_with_infrastructure_retry |
|---|---|---|---|---|---|---|---|
| random | 150 | 150 | 0 | 0 | 0 | 150 | 150 |
| sobol | 150 | 150 | 0 | 0 | 0 | 150 | 150 |
| bo_qlognei_throughput | 150 | 150 | 0 | 0 | 0 | 152 | 150 |
| bo_qlognparego_multiobjective | 150 | 150 | 0 | 0 | 0 | 152 | 150 |
| bo_qlognehvi_multiobjective | 150 | 150 | 0 | 0 | 0 | 152 | 150 |
| postgresql_default | 0 | 25 | 0 | 0 | 0 | 25 | 25 |

## Nondominated candidates

| seed | physical_method | throughput_tps | p99_ms | requested_configuration |
|---|---|---|---|---|
| 1902413987 | bo_qlognei_throughput | 3036.1592 | 25.5573 | {"checkpoint_completion_target":"0.946227","checkpoint_timeout":"348","effective_cache_size":"410553","max_parallel_workers_per_gather":"4","max_wal_size":"1017","random_page_cost":"3.206898","shared_buffers":"178159","work_mem":"8918"} |
| 740267717 | bo_qlognparego_multiobjective | 3049.0802 | 25.6814 | {"checkpoint_completion_target":"0.902683","checkpoint_timeout":"1154","effective_cache_size":"467620","max_parallel_workers_per_gather":"4","max_wal_size":"1024","random_page_cost":"3.265555","shared_buffers":"173158","work_mem":"27848"} |
| 1418705027 | bo_qlognehvi_multiobjective | 3050.2808 | 25.74 | {"checkpoint_completion_target":"0.924237","checkpoint_timeout":"1715","effective_cache_size":"165689","max_parallel_workers_per_gather":"2","max_wal_size":"922","random_page_cost":"2.03964","shared_buffers":"157851","work_mem":"19376"} |
| 1418705027 | bo_qlognei_throughput | 3056.1217 | 25.748 | {"checkpoint_completion_target":"0.937762","checkpoint_timeout":"1026","effective_cache_size":"452243","max_parallel_workers_per_gather":"0","max_wal_size":"757","random_page_cost":"3.252703","shared_buffers":"145031","work_mem":"19171"} |
| 1418705027 | bo_qlognparego_multiobjective | 3057.0183 | 25.816 | {"checkpoint_completion_target":"0.919279","checkpoint_timeout":"1354","effective_cache_size":"425171","max_parallel_workers_per_gather":"0","max_wal_size":"981","random_page_cost":"3.061263","shared_buffers":"141672","work_mem":"16656"} |
| 1418705027 | bo_qlognehvi_multiobjective | 3067.5718 | 25.994 | {"checkpoint_completion_target":"0.936696","checkpoint_timeout":"1430","effective_cache_size":"465506","max_parallel_workers_per_gather":"3","max_wal_size":"633","random_page_cost":"3.655316","shared_buffers":"182848","work_mem":"18022"} |
| 1418705027 | bo_qlognei_throughput | 3092.4414 | 26.135 | {"checkpoint_completion_target":"0.942997","checkpoint_timeout":"1759","effective_cache_size":"503619","max_parallel_workers_per_gather":"0","max_wal_size":"713","random_page_cost":"3.759143","shared_buffers":"160298","work_mem":"3017"} |
| 1418705027 | bo_shared_initial | 3113.6647 | 26.242 | {"checkpoint_completion_target":"0.871126","checkpoint_timeout":"1583","effective_cache_size":"416530","max_parallel_workers_per_gather":"1","max_wal_size":"799","random_page_cost":"2.733019","shared_buffers":"146454","work_mem":"23329"} |

## Figures

- `figures/figure-best-throughput-by-slot.svg`
- `figures/figure-default-drift-by-position.svg`
- `figures/figure-hypervolume-by-slot.svg`
- `figures/figure-minimum-p99-by-slot.svg`
- `figures/figure-pareto-tps-vs-p99.svg`
- `figures/figure-safety-outcomes-by-method.svg`

## Inference guards

- Seed is the independent unit (n=5). The exact two-sided sign-flip p-value cannot be below 0.0625; p-values are supplementary to per-seed direction and practical effect size.
- The five endpoints are correlated views of TPS and p99; endpoint wins are not independent confirmations.
- Lead with reliable budget allocation and degradation avoidance, report p99 in milliseconds with modest scale context, and do not rank qLogNParEGO against qLogNEHVI.
- Drift flags are transparency markers; they never terminate or discard final five-seed.
- Retained infrastructure attempts consume no candidate slot and never train a GP.
- No post-result change to endpoints, the reference point, validity rules, retry treatment, drift adjustment, or the multiplicity family is permitted without an append-only deviation record and a separate sensitivity analysis.
