# Historical diagnostics

`scale10` preserves the old scale-10 sanity report generator and custom SQL workloads. It reads historic campaign IDs and can take explicit `--artifact-root` and `--output-dir`; its output is descriptive diagnostic evidence, not the frozen primary comparison. `tests` retains historical durable-benchmark tests that are no longer part of the canonical worker.
