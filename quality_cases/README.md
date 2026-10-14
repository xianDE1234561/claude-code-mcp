# Initial finite-contract quality benchmark

The four cases cover two reproducible defects, a correct implementation, and a documentation/unsupported-input false-positive trap. Each case declares a finite acceptance contract and a trusted built-in Python oracle. Oracles execute only the bundled synthetic examples in isolated temporary directories.

`quality_eval.py` validates expected failing/passing oracles offline. It does not measure the model. `quality_eval.py --live` explicitly invokes the locally registered MCP and existing provider. Raw reports can contain private runtime paths and must remain local.

Candidate matching checks the declared file/line and trigger, actual behavior and expected-behavior phrases. This is case-level matching, not semantic proof: inspect candidate reports and reproduce behavior with the oracle. Incomplete or failed model cases are recorded separately and excluded from the completed-case confusion matrix. The evidence validity rate counts accepted source-backed candidates versus rejected evidence in complete reports.

An initial live run completed all four cases: two known defects matched; both negative cases produced no candidate defects; no evidence was rejected. Median case time was about 28.6 seconds. This is one small run against the existing local provider, not a general model accuracy estimate or a controlled latency comparison. The same code can perform subsequent runs without shipping credentials or prior raw outputs.
