---
name: codex-claude-review
description: Run a Codex implementation and independent Claude review workflow when the user requests Codex+Claude collaboration, review after implementation, or Claude-assisted diagnosis of failing code. Use for code changes and debugging with the local claude_cli MCP.
---

# Codex + Claude review

Codex implements and verifies the task; Claude supplies independent review using the configured `claude_cli` MCP. Preserve the user's requested scope and existing authorization.

## Choose the call

- After implementation and initial tests, use `review_diff` with the actual `project_root`, requirements/acceptance conditions, and relevant project-relative `files`.
- For Git projects, `scope=working_tree` compares HEAD with the working tree (staged, unstaged and untracked files). Choose `staged` or `unstaged` only when that is the intended review boundary. Set `base_ref` when the user/task has a different verified baseline.
- For ordinary directories, use `scope=files` and list the edited files plus the small set of dependencies/tests needed to understand them.
- When tests fail, use `diagnose_failure` with relevant `files`, the actual `log` or relative `log_file`, and the failed `command`. This tool does not execute the command.
- Use `ask_claude` for open-ended advisory discussion; pass needed context explicitly.

Do not send the entire project by default. The bridge can skip sensitive paths, oversized files and excerpts; inspect its coverage report. It adds at most four local imports/tests within the project; set `include_related=false` to disable that context. If the needed path is outside its allowed roots, report the limitation rather than automatically widening access. Unborn Git repositories are supported; `verdict=no_changes` returns without a model request.

## Verify and fix

1. Parse the MCP result's `structuredContent`, or JSON in its text content. `isError=true` means the review/diagnosis did not succeed; do not treat it as an empty finding list.
2. Check `coverage.complete`, `coverage.skipped`, excerpt ranges, `chunks.complete`, `chunks.unreviewed` and `freshness_verified`. `freshness.changed`, `freshness.unreadable`, `freshness.verification_timeout` and `freshness.verification_cancelled` distinguish changes from unknown freshness. Recollect changed sources and resolve failed verification before applying advice. A successful tool response can contain a partial review: completed findings remain candidates and unreviewed ranges must be disclosed.
3. Findings contain file/side, line range, evidence, source SHA-256, trigger, expected/actual behavior, an exact requirement/source-contract basis and verification steps. Confirm current hashes and inspect the citations. `location_verified` and `basis_verified` prove matching quotations; `verification_status=candidate` still requires host verification. Duplicate matching behaviors merge into `related_locations`.
4. Reproduce or substantiate each candidate issue. Fix only confirmed issues within the authorized task. `suggestions` contains documentation/style/optional-test advice, separate from defects. Treat `hypotheses` and `tests_to_run` as suggestions, not executed tests or confirmed root causes.
5. Run the relevant tests after fixes. One further Claude review may check changed areas. Default to at most two review calls per task, unless the user requests more or new evidence justifies additional work. Do not loop merely to reach model agreement.
6. Report actual changes, test evidence, confirmed/rejected review findings and any partial review. A successful review does not prove project correctness.

Claude does not receive this chat automatically and cannot edit files through these tools. Do not enable bypass-permission flags or nested model delegation to complete this workflow. This review skill does not itself authorize external publishing; follow any separate explicit user authorization.

The timeout is a total collection/model/verification budget (10–600 seconds). Progress notifications and `timing` show actual stages and elapsed time; they do not reset the deadline. Failures include `failed_phase`. The helper displays progress on stderr; `--quiet-progress` suppresses it. Client UI progress support can vary.

V4 observes filtered CLI stream events: initialization, bounded API retry status/category, output activity and final-result presence. Raw text, thinking, session IDs and paths are never progress events. Activity does not mean a final report exists. `stream_events=false` uses legacy final JSON for compatible clients/providers. A persistent temporary-capture cleanup failure is explicitly flagged; it must not replace the original timeout.

Default `chunked=true` reviews bounded module/file windows sequentially under one global deadline. `chunk_chars` is 6000–30000 (default 18000); `max_chunks` is 1–12 (default 12). Small source files and their named tests share a request when they fit. Check per-chunk `focuses`, request/stream metadata and pending ranges. Line windows on opposite diff sides may shift, and cross-chunk dependencies are not exhaustively covered. `chunked=false` keeps the previous single-request mode. Model calls per chunk count toward the task's usage; do not split small tasks needlessly.

The packaged finite-contract benchmark distinguishes offline oracle checks from `quality_eval.py --live` model measurements. Four cases are an initial regression baseline, not a general accuracy claim. Live reports contain private local context and must remain local.

Complete matching reviews and redacted source excerpts may be cached for five minutes within one server process. Sources are reread and hashed; cached reviews undergo freshness checks again. `request.cache_hit=true` and `model_called=false` identify reuse. Set `use_cache=false` to bypass both caches. Restarting the server clears them. Result JSON can contain private project paths, source quotations and logs: keep it local and exclude it from public uploads.

## If native MCP tools are not yet available

Stream `transport_keepalive` means the connection is alive, not that the model produced output. Only recognized content delta/message types count as output activity; raw contents remain private.

Prefer the registered MCP tools when present. For a newly updated service that the current chat has not loaded, the packaged helper can call the same registered server:

```powershell
python <skill-directory>/scripts/call_mcp.py --tool review_diff --arguments-file <UTF-8-JSON-file> --output <result-json-file>
```

Create a UTF-8 argument file in the task directory instead of interpolating source code/logs into a shell command. The helper also accepts `diagnose_failure`, `ask_claude` and `claude_status`. If networking is blocked by the execution environment, use the normal approval mechanism for a scoped retry; do not misreport it as a provider outage.

Example arguments for a non-Git directory:

```json
{
  "project_root": "<absolute project directory>",
  "scope": "files",
  "files": ["module.py", "test_module.py"],
  "requirements": "Actual task requirements and acceptance conditions",
  "timeout_seconds": 180
}
```

Use the helper only with trusted local `claude_cli` configuration. Model calls consume the existing provider's usage. Keep result files inside the task directory; do not save full source context or credentials in shared logs.
