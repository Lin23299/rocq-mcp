# Agent guide — CCV fork of `rocq-mcp`

This document is for **agents driving this fork** (and the humans briefing them) on a
Rocq project. It assumes you have read the upstream `README.md` (tool-by-tool
reference). Here we cover only what this fork changes, how to install it in a plain
project, and the operational rules distilled from long autonomous proof runs.

---

## 1. What this fork adds (vs. upstream `LLM4Rocq/rocq-mcp`)

**`pet` side (`rocq-lsp` fork, shipped together):** demand patches that add explicit
budgets and recovery behaviour:

- per-call timeout knobs for interactive and compile calls,
- a start budget large enough for position replay,
- bounded source sizes and bounded state retention,
- an RSS watchdog that restarts `pet` instead of letting it grow without bound.

**MCP server side (`rocq-mcp` fork):**

- **Oversized goal text is clipped, not dropped.** Each response keeps goal
  *structure*; over-long goals are head/tail clipped and marked
  `[clipped K of M chars; pass goals_max_chars=-1 ...]`.
- **Large text fields arrive as real multi-line text blocks** (a
  `TextBlockOutputMiddleware`), so `\n` inside goals is a newline, not `\\n`.
  The first block of a response is a one-line JSON envelope carrying
  `<field>_chars` sizes; the structured payload still contains the full dict.
- **`goals_max_chars` parameter** on `rocq_start` / `rocq_check` /
  `rocq_step_multi`: `-1` disables clipping for that call.

## 2. Environment variables (all read once at server start)

| Variable | Default | Effect |
|---|---|---|
| `ROCQ_WORKSPACE` | cwd | Default workspace root when the client does not pass one. |
| `ROCQ_COQC_TIMEOUT` | `60` | Per-call timeout for `rocq_compile` (seconds). Explicit per-call values are **not** clamped. |
| `ROCQ_VERIFY_TIMEOUT` | `120` | Per-call timeout for `rocq_verify`. |
| `ROCQ_PET_TIMEOUT` | `30` | Fallback pet timeout when a call passes none. Deployments that iterate on heavy files usually set `300`. |
| `ROCQ_PET_TIMEOUT_GRACE` | `10` | Extra seconds after the pet timeout before a call is declared dead. |
| `ROCQ_QUERY_TIMEOUT_CAP` | `300` | **Hard cap** for per-call `check`/`step_multi`/`query` timeouts. |
| `ROCQ_START_TIMEOUT` | `1800` | `rocq_start` budget; a position start replays **from the file head** to the anchor. |
| `ROCQ_COQC_BINARY` | `coqc` | Compiler binary (e.g. `rocq compile` wrappers). |
| `ROCQ_MAX_SOURCE_SIZE` | `1000000` | Max bytes for file/body/proof/statement payloads. |
| `ROCQ_MAX_PET_RSS_MB` | computed | RSS watchdog: when a pet exceeds it, it is **restarted** (all state ids die). |
| `ROCQ_MAX_GOAL_CHARS` | `12000` | Per-goal clipping threshold. |
| `ROCQ_MAX_GOALS_TOTAL_CHARS` | `40000` | Total goal text per response. |
| `ROCQ_MAX_STATES` | `1000` | LRU cap for retained states. |
| `ROCQ_COMPILE_MULTI_ERROR_CAP` | `20` | Max errors reported by the multi-error walker of `rocq_compile_file`. |
| `ROCQ_COMPILE_MULTI_ERROR_TIMEOUT` | `5.0` | Per-error walker budget (seconds). |
| `ROCQ_ENRICHMENT_TIMEOUT_CAP` | `5.0` | Budget for enriching compile results with goal state. |
| `ROCQ_DUNE_BUILD` | `1` | Auto load-path discovery via dune for dune projects (`0` disables). |

## 3. Installing in a plain project

Two supported models; both give a stdio MCP server, so the client side is identical.

**A. Switch-native (recommended for reuse across projects).** `pet` is installed into
an opam switch you already use, and the server + venv + launcher live under that
switch. Any project using the switch gets MCP by activating the switch:

```bash
eval $(opam env --switch=<switch>)
ccv-rocq-mcp          # stdio MCP server; point your client at this command
```

Installer: `scripts/mcp/switch_install.py` (in the CCV framework repo, branch
`feature/mcp-switch-install`). It pins the `rocq-lsp` fork via opam, vendors this
fork's source under `<switch>/.ccv-mcp/src`, builds a venv
(`<switch>/.ccv-mcp/venv`), and writes the `ccv-rocq-mcp` shim which exports
`COQLIB`/`COQCORELIB`/`ROCQPATH` and the `ROCQ_*` budgets. It refuses to touch a
switch that has live Rocq processes and snapshots the switch first. On a machine
without network access, pass `--ref-venv <existing venv>` to copy the dependency
packages from a reference venv and install the server with `--no-deps`.

**B. Isolated pin (CCV's model).** `build_demand.py --install` copies a patched
`pet` and this server's source into a content-addressed directory under
`~/.local/lib/ccv-mcp/`, and workspace-level launchers (`rocq-mcp.json` +
`rocq-mcp-launch.py`) point at it. Heavier to configure per project, but the
toolchain switch stays untouched and every artifact is sha-verified.

## 4. Client wiring

Any MCP client that can run a local stdio command works. Example (opencode):

```json
{ "mcp": { "rocq": { "type": "local", "enabled": true,
  "command": ["ccv-rocq-mcp"], "timeout": 1860000 } } }
```

Claude Code: `.mcp.json` with `"command": "ccv-rocq-mcp"`, same long timeout.
Set the server working directory to your project (or pass `workspace` per call) so
`_RocqProject` discovery works. Expect the first interactive call to pay the
document replay cost.

## 5. Session model and hard limits (read before planning work)

- **States are snapshots in the pet process.** `rocq_start` returns a state id;
  every `check`/`step_multi` returns the new id. Ids survive across calls *and*
  across agent restarts **as long as the same pet process lives**.
- **No undo.** `from_state=<id>` re-executes from that snapshot; that is the *only*
  branch/rollback mechanism.
- **`stale_warning` means the workspace file changed since the state was built.**
  The state still answers, but it describes the old text. After editing the file,
  continue from a state built *before* the edit, or restart.
- **Everything is a budget.** One `rocq_check` call has a single wall-clock budget
  (≤ `ROCQ_QUERY_TIMEOUT_CAP`) **shared by all commands in its body**. The response
  tells you which command failed and the last good state id — continue from there
  with the remainder as a new call. Put heavy tactics (big `entailer!`,
  `simplify`, closed-chain rewrites) in their own call.
- **A position start replays from the file head.** `rocq_start(line=N)` is not a
  fast-forward: the whole prefix is re-executed, bounded by `ROCQ_START_TIMEOUT`.
  On files with heavy prefixes, deep position starts are the wrong tool; use a
  shallow anchor plus chunked advancement (see §6).
- **State losses are silent and routine.** Two mechanisms kill states: the LRU cap
  (`ROCQ_MAX_STATES`) and the RSS watchdog (`ROCQ_MAX_PET_RSS_MB`, which restarts
  the pet). A restart invalidates **all** ids. Commit the proof file often and
  treat `pet_restarted` as "rebuild the ladder", not as data loss of your work.
- **In-file `Set Default Timeout` applies to MCP-run sentences too.** If a sentence
  hits it, the call fails with `Error: Timeout!` at that line — that is a proof
  engineering signal (split the sentence), not a tool malfunction.
- **The host may truncate what you *see*.** MCP clipping (`[clipped ...]`) is
  separate from client-side truncation (oversized tool results are spilled to
  files by the host). When in doubt, ask for `goals_max_chars=-1` and read the
  spilled file with grep/read; do not re-issue the same call hoping for more.

## 6. Patterns that survive long runs

1. **State ladder + baseline replay.** Pick a shallow anchor (file start or any
   cheap landmark). Advance in chunks (each ≤ the call cap) and *keep* the state
   just before the region you are about to edit ("baseline"). Iterate fixes by
   replaying only `[baseline .. failure]` via `from_state=baseline` — seconds to
   minutes instead of a full replay. If you must edit before the baseline, the
   ladder is invalidated; rebuild from the earliest affected layer (file start if
   you touched top-level definitions).
2. **Feed verbatim text.** The session must see exactly what is on disk. In
   particular: Coq will not let you redefine an `Ltac` with the same name, so
   *never* work around it by feeding renamed copies — the session silently
   diverges from the file and your goals stop matching the compiler's. Rebuild the
   state instead.
3. **Do not full-compile to locate failures** in heavy files: the first pass costs
   the same as a full compile, but yields no usable state. Full compile is for
   **closure** (final `.vo`, `Print Assumptions`) and for validating that
   remaining work is isomorphic to what the ladder already verified.
4. **Batch budget accounting.** If a call dies mid-body, note the returned state
   id and re-issue only the remaining sentences. Heavy closers that time out
   inside a batch often succeed as their own call (the batch's earlier commands
   consumed the shared budget).
5. **Goal-order sensitivity.** `all: try (...)` batteries behave differently
   depending on which goal is focused. A residual that a battery closes when it is
   Goal 1 may survive inside a batch. Inspect (`Show 1.`, full dump) before
   concluding a tactic is wrong.
6. **Keep terms linear.** Rewriting patterns that inline the previous array/list
   into the new one (e.g. repeated `upd_Znth` chains) grow terms multiplicatively;
   after a few iterations `entailer!`/`forward` diverge. `remember` cannot splice
   an Ltac `constr`; use an `assert ... as H; destruct ...; rewrite` style to
   abstract the new term into an atom and keep goals tractable. This single change
   turned 30s–300s divergences into ~1s closings in the runs that motivated this
   guide.
7. **Dump discipline.** Defaults clip per goal (12k chars) and per response (40k).
   `[clipped K of M chars]` is normal — do not retry for more unless you truly
   need the full text; then use `goals_max_chars=-1` once and read the spilled
   file, not the chat.

## 7. Failure taxonomy

| Symptom | Meaning | Action |
|---|---|---|
| `No node at point` on start | Line/character has no sentence | Use a line that contains a sentence (0-based, sentence start). |
| `Error: Timeout!` at `File "...", line N` | The *file's* per-sentence timeout fired (e.g. `Set Default Timeout 300`) | Split/harden that sentence; it is pathological. |
| Call timed out, `commands_run=k` | Call budget consumed | Continue from the returned state id with the remainder. |
| `No matching clauses for match` | Goal shape differs from what the tactic expected (often after an edit or a wrong feed) | Dump the focused goal; verify the session's text matches disk (§6.2). |
| `pet_restarted` / all ids invalid | RSS watchdog or crash | Rebuild the ladder; commit work first. |
| `stale_warning` on a state | File changed after the state was built | Replay from an earlier consistent state. |
| Host truncated output | Client-side spill, not MCP clipping | grep/read the spill file; or request a smaller/complete dump. |

## 8. Record for auditability

When a proof lands, record: the fork commit(s) (`pet` and server), the toolchain
switch + Rocq version, the `ROCQ_*` values your launcher exported, and where the
final `.vo` was produced. The CCV flow stores this in a registry JSON plus a
per-workspace config; a plain project can keep the same facts in its README or a
`mcp.lock.json`. Without them, a certificate is not reproducible.
