# 测试能力导航

测试包覆盖 MCP 后端行为，不等于 C 项目的验证证书，也不替代宿主模型实际使用测试。
测试有纯函数、mock、真实 Pet、真实 stdio、真实 coqc 等层次；运行时须保留 passed、failed、skipped 的区别。

## 2026-10-08：19 项旧测试迁移

基准为 `c239cda` 的现行实现，以及此前已对账的 19 项失败。
本轮按已确认合同迁移测试，未修改 `src/`，没有新增 skip/xfail。

| 原失败数 | 测试入口 | 所验证的框架能力及迁移依据 |
|---:|---|---|
| 4 | `test_assumptions.py::TestAssumptionsAvailableInFile` | `Print Assumptions` 缺名恢复。真实 Rocq 9.0 的 `Coq: The reference <requested> was not found in the current environment.` 才触发 TOC 补充及 `not_found` 分类；保留清单、截断标记、诊断归属与避免重复记账检查。 |
| 7 | `test_start.py::TestStartNotFoundAvailableInFile` | 定理入口缺名恢复。Pet 的 `Theorem_not_found` code 为 `-32006`；保留名称排序、500 项 cap、原总数、缓存命中/mtime 失效。其它 code 不借 TOC 重分类。 |
| 3 | `test_query.py::TestGetFileEndState` | 文件查询上下文完整加载。真实 EOF 使用零基行及 UTF-16 列；无尾换行不越界，有尾换行取下一空行零列，空文件走 root。补 CRLF、裸 CR、非 BMP 字符边界。 |
| 1 | 同上，权限用例 | 文件读取错误诚实报告。对当前 `read_bytes` 注入 PermissionError，核注入确实发生、错误不泄露绝对路径、不请求 Pet 状态。 |
| 2 | `test_server.py::TestFormatGoals` | 完整目标文字与首屏预览分层。格式化层不得按旧目标个数 cap 丢项；显式预览可裁剪，但完整渲染须逐字保留所有目标与结论中段/尾段。 |
| 1 | `test_focus_depth.py::TestGoalsWithDepth` | 目标读取失败与空目标分离。真实失败必须传播；实际空目标为 `(None, 0)`，不能以 `(None, None)` 吞故障。 |
| 1 | `test_start.py::TestStartTimeoutForwarding` | 文件 start 与局部 preamble/query 分预算。文件 start 使用 `ROCQ_START_TIMEOUT`，局部模式使用 `ROCQ_QUERY_TIMEOUT_CAP`；超限回显上限，低于本模式上限不被另一模式截短。 |

`test_real_pet_integration.py::TestNotFoundEnrichmentRealPet` 同时验证 assumptions 和 start：
真实错误形状、真实 TOC 名称、正确诊断归属、同 PID 后续正确名称成功。
其中运行 assumptions 前先读取真实 query 原始错误，不拿 mock 的绿色替代协议核验。

两项旧裁剪测试改为格式化层的原文/预览检查；有界传输和完整补读的替代覆盖见
`test_output_goal_part.py`、`test_output_tools.py`，后者不是由 `_format_goals` 一层独自保证。

## 完整测试包的能力分组

| 能力 | 主要测试文件 |
|---|---|
| 开证明、续步骤、候选试探、分支状态/证明链 | `test_start.py`、`test_check.py`、`test_step_multi.py`、`test_server.py`、`test_proof_walk.py` |
| 文件编辑恢复、有效前驱、状态失效及原生环境更新 | `test_file_progress.py`、`test_partial_and_anchor.py`、`test_staleness.py`、`test_workspace_updates.py` |
| 查询、假设、公理检查、TOC、符号和 notation | `test_query.py`、`test_assumptions.py`、`test_toc.py`、`test_notations.py` |
| 大文本限幅、UTF-8/SHA/offset 身份、受控句柄、逐项索引、活 goal/hyp 补读 | `test_output_store.py`、`test_output_query.py`、`test_output_feedback.py`、`test_output_goal_part.py`、`test_output_toc_notations.py`、`test_output_tools.py` |
| 错误回执、首错恢复、时间/内存治理、诊断 | `test_envelope_contract.py`、`test_format_error.py`、`test_timeout.py`、`test_memory_watchdog.py`、`test_diag.py`、`test_focus_depth.py` |
| 文件编译、错误位置/状态捕获、沙箱证明核验 | `test_compile.py`、`test_compile_file.py`、`test_multi_error.py`、`test_verify.py`、`test_integration.py` |
| 真实 Pet 对照 | `test_real_pet_integration.py` 及其它文件带 Pet 可用性条件的 integration 类 |

## 本轮实测与范围

- 六个修改文件的 366 个测试在完整回归中全部通过。
- 完整包：**1381 passed / 0 failed / 201 skipped**（61.80s）。
- skips 保持原有 coqc 可用性条件：169 项 `coqc not available`，7 项 compile-error capture，
  5 项 Phase 2 verification，20 项 multi-error。部分 mock 也受原模块级条件影响；
  不能将这些 skip 当成编译/核验能力已通过。
- 真实 Pet 使用安装制品 `122c3b3185fb-92e1d19c3c65/bin/pet`，证明库用 fcil，ML runtime 用 Pet 构建 switch。
  MCP 测试执行当前 checkout 的 `src/`。
- 隔离内存变异 6/6 被断言抓住：宽泛 assumptions 缺名、宽泛 start 缺名、越界 EOF、
  吞目标读取异常、丢尾部目标、完整渲染仍裁剪；未改动生产实现。
- 本轮只调整测试；安装 runtime 身份不变。正式 host/FSM/model 的新业务 run 仍是独立验收。
