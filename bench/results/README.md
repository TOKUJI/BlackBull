# `bench/results/` — where every bench run writes

Each run of a bench tool gets its own directory:

```
bench/results/<tool>/<UTC time>-<revision>/
    manifest.json   tool, start time, git revision (and whether the tree was dirty),
                    host, command line, settings
    tmp/            the run's temporary files (also exported as TMPDIR)
    ...             the tool's output
```

Tools get this directory from `bench/lib/results.sh` (`bench_run_dir <tool>`)
or `bench/lib/results.py` (`run_dir(tool)`), and never write to `/tmp`.  A
tool started by another tool writes into its caller's run (`BENCH_RUN_DIR`).
`BENCH_RESULTS_ROOT` moves the whole tree.

Nothing here is committed.  Attach the results a decision rests on to its
YouTrack issue; the tool that produced them stays in git.
`tests/architecture/test_bench_outputs.py` enforces all of this.
