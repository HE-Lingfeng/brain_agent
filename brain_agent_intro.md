# brain_agent 介绍文档

`brain_agent` 是一套面向 WorldQuant BRAIN Alpha 研究的本地自动化编排系统。它的目标不是替代研究者，而是把 Alpha 挖掘中重复、易错、需要持续记录的部分工程化：从生成假设、产出表达式、构建回测配置、批量回测、失败诊断、局部优化、submit gate 检查，到长期质量学习和研究报告沉淀。

系统的核心特点：

- 统一入口：用 `python3 -m brain_agent` 管理生成、回测、恢复、报告、记忆、Prompt A/B 和 settings 选择。
- 可复盘：每次 run 都落到独立目录和 SQLite，保留配置、候选、任务日志、回测结果、诊断、gate 检查和报告。
- 质量闭环：不只生成表达式，还学习哪些 thesis、字段族、operator、失败模式和修复策略长期有效。
- 保守安全：默认不自动 submit；网络错误、gate 不完整、平台繁忙等情况不会被误判成 Alpha 质量失败。
- 可扩展：legacy skills 作为执行适配层，`brain_agent` 作为长期维护的编排层。

## 1. 系统定位

```text
                 +--------------------------------+
                 |          Researcher             |
                 |  setting / dataset / judgement  |
                 +----------------+---------------+
                                  |
                                  v
                 +--------------------------------+
                 |          brain_agent CLI        |
                 |  run / resume / report / memory |
                 +----------------+---------------+
                                  |
             +--------------------+--------------------+
             |                                         |
             v                                         v
 +--------------------------+              +--------------------------+
 |  Local Research Runtime  |              |   WorldQuant BRAIN API   |
 |  SQLite / artifacts/logs |              |  datafields/simulation   |
 +--------------------------+              +--------------------------+
             |
             v
 +--------------------------+
 | Long-term Learning Layer |
 | memory / quality / prompt|
 +--------------------------+
```

`brain_agent` 的定位可以概括为：

```text
Idea generator + Simulation orchestrator + Research notebook + Quality learner
```

它负责把一次 Alpha 挖掘实验变成一个可恢复、可比较、可审计的工程流程。

## 2. 整体架构

```text
+-----------------------------------------------------------------------------------+
|                                  brain_agent                                       |
+-----------------------------------------------------------------------------------+
|                                                                                   |
|  +---------+      +------------+      +------------+      +--------------------+   |
|  |   CLI   | ---> | Controller | ---> |  Adapters  | ---> | Legacy Skill Layer |   |
|  +---------+      +------------+      +------------+      +--------------------+   |
|       |                 |                   |                       |              |
|       |                 |                   |                       v              |
|       |                 |                   |          +------------------------+  |
|       |                 |                   |          | BRAIN API / LLM / logs |  |
|       |                 |                   |          +------------------------+  |
|       |                 |                   |                                      |
|       v                 v                   v                                      |
|  +---------+      +------------+      +------------+                               |
|  | Presets |      | Repository |      | TaskRunner |                               |
|  +---------+      +------------+      +------------+                               |
|                         |                                                         |
|                         v                                                         |
|  +--------------------------------------------------------------------------+     |
|  | Runtime Store: SQLite + artifacts + task logs + reports                  |     |
|  +--------------------------------------------------------------------------+     |
|                         |                                                         |
|                         v                                                         |
|  +--------------------------------------------------------------------------+     |
|  | Quality Layer: scoring / quota / diagnosis / variants / memory / A/B      |     |
|  +--------------------------------------------------------------------------+     |
|                                                                                   |
+-----------------------------------------------------------------------------------+
```

整体设计采用“主编排层 + 执行适配层”的结构：

- `brain_agent` 负责状态、策略、质量判断、报告和长期演化。
- legacy skills 负责已经验证过的具体执行能力，例如生成表达式、构建 alpha list、批量回测。
- BRAIN API 和 LLM 都被封装在可记录、可失败恢复的任务流程里。

## 3. 一次 Run 的生命周期

```text
+-------+    +----------+    +----------+    +-----------+    +------------+
| INIT  | -> | GENERATE | -> | INSPECT  | -> | SIMULATE  | -> | DIAGNOSE   |
+-------+    +----------+    +----------+    +-----------+    +------------+
                              |                |
                              |                v
                              |        +----------------+
                              |        | Quota Allocator|
                              |        +----------------+
                              |                |
                              v                v
                         alpha_list      simulation_status
                              |                |
                              +-------+--------+
                                      v
                              +---------------+
                              | VARIANT_SEARCH|
                              +---------------+
                                      |
                                      v
                              +---------------+
                              | SIM_VARIANTS  |
                              +---------------+
                                      |
                                      v
+----------+    +---------+    +------------+    +--------+    +------+
| ENHANCE  | <- | DECIDE  | <- | Learn/Score| -> |  GATE  | -> |REPORT|
+----------+    +---------+    +------------+    +--------+    +------+
```

每一轮研究大致做这些事：

1. `GENERATE`：先产出 factor thesis，再生成 Alpha 表达式。
2. `INSPECT`：把表达式转换成带完整 BRAIN settings 的 alpha list。
3. `SIMULATE`：按 score 和 quota 选择值得回测的表达式，批量提交到 BRAIN。
4. `DIAGNOSE`：解析回测指标、错误信息和失败模式。
5. `VARIANT_SEARCH`：对有弱信号或可修复迹象的 Alpha 做局部变体搜索。
6. `DECIDE`：决定是否增强、放弃、进入 gate 或人工复核。
7. `ENHANCE`：根据诊断和记忆生成下一批修复表达式。
8. `SUBMIT_GATE`：只做 submit readiness 检查，不自动生产提交。
9. `REPORT`：生成研究日志、指标汇总、失败模式和下一步建议。

## 4. CLI 与 Settings 模块

```text
+----------------------------+
|        brain_agent CLI      |
+----------------------------+
| run                         |
| resume                      |
| status / report / export    |
| tasks refresh/cancel/retry  |
| settings list/show/choose   |
| memory summary/ingest       |
| research summary            |
| prompt compare              |
| forum / knowledge           |
+-------------+--------------+
              |
              v
+----------------------------+
|        RunConfig            |
+----------------------------+
| dataset                    |
| region / delay / universe  |
| data_type / decay          |
| truncation / neutralization|
| batch_size / concurrency   |
| prompt versions            |
| variant/search limits      |
+-------------+--------------+
              |
              v
+----------------------------+
|      Settings Presets       |
+----------------------------+
| reusable region settings    |
| dataset supplied by user    |
| interactive choose flow     |
+----------------------------+
```

设计要点：

- 常用 settings 可以通过 preset 选择，减少每次会话重复输入。
- dataset 不内置，因为 BRAIN 数据集会变化，通常由用户输入。
- GLB 和非 GLB 的回测并发默认值不同，避免在更敏感区域触发过多限流。

## 5. Controller 编排模块

```text
+--------------------------------------------------+
|              BatchLoopController                 |
+--------------------------------------------------+
| load/create run                                  |
| advance stage                                    |
| call adapter                                     |
| persist artifacts/candidates/results             |
| stop when target_ready reached                   |
| mark FAILED with structured report on exception  |
+-----------------------+--------------------------+
                        |
                        v
        +---------------+---------------+
        |                               |
        v                               v
+---------------+               +----------------+
| RunStage FSM  |               | DecisionEngine |
+---------------+               +----------------+
| INIT          |               | select enhance |
| GENERATE      |               | select review  |
| INSPECT       |               | select gate    |
| SIMULATE      |               +----------------+
| VARIANT_SEARCH|
| ENHANCE       |
| SUBMIT_GATE   |
| REPORT        |
+---------------+
```

Controller 是系统主循环。它不直接关心 legacy skill 的内部实现，而是通过 adapter 获取标准化结果：

```text
AdapterResult
  status
  artifacts
  candidates_delta
  metrics_delta
  error_summary
```

这样一来，生成、回测、增强、gate 检查都可以用统一的状态契约接入。

## 6. Adapter 与 TaskRunner 模块

```text
+---------------------+       +---------------------+
|    Skill Adapter    | ----> |      TaskRunner      |
+---------------------+       +---------------------+
| MakeSomeGemAdapter  |       | spawn subprocess     |
| InspectAdapter      |       | capture stdout/stderr|
| BatchSimAdapter     |       | persist task status  |
| VariantAdapter      |       | refresh/cancel/retry |
| EnhanceAdapter      |       +----------+----------+
| SubmissionGate      |                  |
+----------+----------+                  v
           |                    +---------------------+
           |                    | runs/<run_id>/tasks |
           |                    +---------------------+
           v
+---------------------+
| Legacy Skill Layer  |
+---------------------+
| makeSomeGem         |
| inspectRawTemplate  |
| batchSimAndTrack    |
| enhanceTemplate     |
| shared BRAIN helpers|
+---------------------+
```

Adapter 层的职责：

- 把 `RunConfig` 转换成 legacy skill 可接受的参数。
- 找到并登记输入输出 artifact。
- 解析 legacy 输出，把它转换成 candidate、simulation result、gate check 等结构化数据。
- 把 malformed JSON、网络错误、平台繁忙等情况转换成可记录的 adapter failure，而不是让整轮研究无信息崩溃。

TaskRunner 的职责：

- 管理长任务子进程。
- 持久化 stdout/stderr。
- 支持 `tasks --refresh`、`tasks --cancel`、`tasks --retry`。
- 让前台 CLI 超时和后台 BRAIN 等待解耦。

## 7. 数据与持久化模块

```text
.brain_runtime/
  runs/
    <run_id>/
      brain_agent.sqlite3
      artifacts/
      tasks/
        <task_id>/
          stdout.log
          stderr.log
      run_report.md
      run_result.json
```

```text
+------------------+
| Repository       |
+------------------+
| runs             |
| tasks            |
| artifacts        |
| candidates       |
| sim_results      |
| gate_checks      |
| decisions        |
| memory evidence  |
+--------+---------+
         |
         v
+------------------+
| SQLite per run   |
+------------------+
```

主要数据对象：

- `runs`：本次实验的配置、状态、开始/结束时间。
- `tasks`：所有长任务的执行状态和日志路径。
- `artifacts`：生成文件、alpha list、simulation CSV、报告等。
- `candidates`：表达式、来源、fingerprint、lineage、thesis。
- `sim_results`：Sharpe、Fitness、Turnover、失败标签、错误摘要。
- `gate_checks`：submit/self/prod correlation 等检查结果。
- `decisions`：增强、变体、人工复核等决策记录。

## 8. 质量观测与 Quota Allocator

```text
                  +--------------------+
                  | Candidate Universe |
                  +---------+----------+
                            |
                            v
                  +--------------------+
                  | Scoring Layer      |
                  +--------------------+
                  | quality_score      |
                  | repairability      |
                  | novelty            |
                  | coverage           |
                  | risk_penalty       |
                  +---------+----------+
                            |
                            v
                  +--------------------+
                  | Quota Allocator    |
                  +--------------------+
                  | exploit memory     |
                  | explore new pattern|
                  | repair failures    |
                  +---------+----------+
                            |
                            v
                  +--------------------+
                  | Selected to Sim    |
                  +--------------------+
```

这层解决的问题是：BRAIN 回测 quota 有限，不能把所有生成噪声都提交。系统会综合候选质量、可修复性、新颖性、覆盖风险和 memory 证据，决定哪些 Alpha 值得进入真实回测。

典型效果：

- 避免简单按生成顺序取前 N 条。
- 给“有弱信号但可修复”的表达式保留一定探索空间。
- 给长期有效的 thesis / field family / operator pattern 更高优先级。
- 把明显语法错误、不可用字段、重复表达式等降权。

## 9. Memory 分层模块

```text
+------------------------------------------------+
|                  AlphaMemory                   |
+------------------------------------------------+
| Candidate Memory                               |
|   generated expressions / fingerprints          |
|                                                |
| Simulation Evidence Memory                      |
|   metrics / failures / repair outcomes          |
|                                                |
| Thesis Memory                                  |
|   thesis_type / field_family / hypothesis       |
|                                                |
| Repair Memory                                  |
|   failure_mode -> repair_method -> outcome      |
|                                                |
| Prompt Experiment Memory                        |
|   prompt version -> comparable run metrics      |
+------------------------+-----------------------+
                         |
                         v
+------------------------------------------------+
|         Used by generation / quota / enhance    |
+------------------------------------------------+
```

Memory 不是简单保存“历史好 Alpha”，而是学习长期结构：

- 哪些假设类型更容易产生有效信号。
- 哪些字段族在某个 region/dataset 下更稳定。
- 哪些 operator 组合容易导致 low turnover、coverage issue 或 high correlation。
- 哪些修复方式对某类失败模式真实有效。
- 哪些 prompt 版本在同 dataset/settings 下更优。

## 10. 生成端 Thesis 化模块

```text
+-----------------------+
| Prompt Builder        |
+-----------------------+
| approved knowledge    |
| memory signals        |
| dataset/settings      |
| field/operator budget |
+----------+------------+
           |
           v
+-----------------------+
| Factor Thesis First   |
+-----------------------+
| hypothesis type       |
| economic intuition    |
| field family          |
| expected failure mode |
| intended repair path  |
+----------+------------+
           |
           v
+-----------------------+
| Alpha Expression      |
+-----------------------+
| FASTEXPR              |
| thesis_json attached  |
| candidate persisted   |
+-----------------------+
```

生成端不再只让 LLM 直接吐表达式，而是要求先形成研究假设，再落到表达式。这能让后续 memory 学习从“operator 级别”提升到“假设级别”。

例如系统可以区分：

- 这条 Alpha 是 momentum/reversal 还是 fundamental cross-section。
- 用的是 analyst、institution、price-volume 还是 sentiment 字段族。
- 预期可能失败在 coverage、turnover、correlation 还是 weak fitness。
- 后续应该用 sign flip、window sweep、neutralization 替换还是 coverage repair。

## 11. Variant Search 与专项 Optimizer

```text
                      +----------------------+
                      | Simulated Candidate  |
                      +----------+-----------+
                                 |
                   +-------------+-------------+
                   |                           |
                   v                           v
          +------------------+        +------------------+
          | Failure Tags     |        | Weak Signals     |
          +------------------+        +------------------+
                   |                           |
                   +-------------+-------------+
                                 v
                      +----------------------+
                      | Optimizer Router     |
                      +----------+-----------+
                                 |
     +---------------------------+---------------------------+
     |            |              |              |            |
     v            v              v              v            v
+---------+ +-----------+ +------------+ +-----------+ +-------------+
|LowFitness| |LowTurnover| |HighTurnover| |ShortFlip | |CoverageRepair|
+---------+ +-----------+ +------------+ +-----------+ +-------------+
     |            |              |              |            |
     +---------------------------+---------------------------+
                                 |
                                 v
                      +----------------------+
                      | CorrelationOptimizer |
                      +----------+-----------+
                                 |
                                 v
                      +----------------------+
                      | Lineage Variants     |
                      +----------------------+
```

局部变体搜索的设计原则是“小步、可解释、保留 lineage”。系统不会盲目大改表达式，而是围绕已有信号做邻域探索：

- sign flip：系统性测试负信号反转。
- window sweep：调整 `ts_*` 窗口。
- decay sweep：扫描 simulation setting 中的 decay。
- rank/zscore swap：测试标准化方式。
- group neutralization sweep：替换中性化方式。
- turnover control：增加或放松换手控制。
- coverage repair：增加 backfill、winsorize 或 group neutralize。
- correlation repair：降低 self/prod correlation 风险。

所有变体都会保留：

```text
parent_candidate_id
variant_strategy
variant_params
lineage_json
```

报告中会展示原始 Alpha 与变体的指标变化，方便判断某个优化方向是否有效。

## 12. Gate 与稳定性设计

```text
+--------------------+
| Candidate after Sim |
+---------+----------+
          |
          v
+--------------------+
| Submit Gate Checks |
+--------------------+
| self correlation   |
| prod correlation   |
| submission checks  |
+---------+----------+
          |
          v
+-----------------------------+
| Gate Result Classification  |
+-----------------------------+
| pass                        |
| fail                        |
| incomplete                  |
| network_error               |
| platform_busy               |
+-----------------------------+
```

Gate 的原则：

- 不自动 submit。
- correlation 或 proxy 请求失败时，记录为 `gate_incomplete/network_error`。
- gate 不完整不等于 Alpha 质量差，不能污染长期质量统计。
- 可 submit 的 alpha ID 只进入报告，等待人工确认。

系统也对常见不稳定点做了防御：

- LLM 输出 JSON 解析失败：记录结构化错误。
- BRAIN 平台繁忙或限流：保留 task 日志和 Retry-After 线索。
- datafield preflight 失败：记录 incomplete，但不阻断后续 per-alpha simulation。
- controller 未预期异常：run 标记 `FAILED` 并生成报告，而不是丢失上下文。

## 13. Prompt A/B 实验模块

```text
+-------------------+        +-------------------+
| Run A             |        | Run B             |
+-------------------+        +-------------------+
| dataset           |        | dataset           |
| region            |        | region            |
| delay             |        | delay             |
| universe          |        | universe          |
| decay/truncation  |        | decay/truncation  |
| neutralization    |        | neutralization    |
+---------+---------+        +---------+---------+
          |                            |
          +-------------+--------------+
                        v
              +-------------------+
              | Comparable Check  |
              +-------------------+
              | same settings only|
              +---------+---------+
                        |
                        v
              +-------------------+
              | Promotion Evidence|
              +-------------------+
              | valid rate        |
              | promising rate    |
              | avg fitness       |
              | hard failure rate |
              +-------------------+
```

Prompt A/B 不允许跨 dataset/settings 直接比较。只有配置完全一致的 run 才能判断 prompt 改动是否带来真实改进。

默认 promotion 需要证据：

- valid rate 提升。
- promising rate 提升。
- average fitness 提升。
- hard failure 模式改善。
- 没有引入明显 hard failure 回退。

## 14. 报告与研究日志

```text
+---------------------+
| Runtime Evidence    |
+---------------------+
| config              |
| candidates          |
| sim results         |
| diagnostics         |
| decisions           |
| gate checks         |
| task logs           |
+----------+----------+
           |
           v
+---------------------+
| run_report.md       |
+---------------------+
| Executive Summary   |
| Experiment Setup    |
| Research Timeline   |
| Prompt Metrics      |
| Failure Diagnostics |
| Variant Deltas      |
| Submit Ready IDs    |
| Lessons & Next Steps|
+---------------------+
```

报告不只是结果表，而是一份研究日志。它回答这些问题：

- 本轮用了什么 dataset/settings/prompt version。
- 生成了哪些候选，为什么被选中回测。
- 失败主要集中在哪些模式。
- 哪些变体改善了指标，哪些方向无效。
- 哪些 Alpha 通过 gate，可以人工考虑 submit。
- 下一轮应该优化什么。

## 15. 模块职责总览

```text
+-------------------------------+--------------------------------------------------+
| Module / Package              | Responsibility                                   |
+-------------------------------+--------------------------------------------------+
| cli.py                        | command entry, args, presets, user workflows     |
| core/repository.py            | SQLite persistence                               |
| core/models.py                | RunConfig, RunStage, CandidateStatus             |
| core/task_runner.py           | subprocess execution and task logs               |
| core/progress.py              | CLI simulation progress visualization            |
| core/settings_presets.py      | reusable BRAIN settings choices                  |
| pipeline/controller.py        | run lifecycle orchestration                      |
| pipeline/adapters.py          | bridge brain_agent and legacy skills             |
| pipeline/quota_allocator.py   | allocate scarce simulation budget                |
| pipeline/variant_search.py    | local variant generation                         |
| pipeline/optimizers.py        | specialized optimizer modules                    |
| pipeline/thesis.py            | factor thesis extraction/inference               |
| analysis/diagnostics.py       | failure tags and repair objectives               |
| analysis/scoring.py           | candidate selection score                        |
| analysis/memory.py            | long-term evidence learning                      |
| analysis/quality.py           | research quality and prompt comparison metrics   |
| analysis/reporting.py         | research log and result reports                  |
| intelligence/prompting.py     | prompt context and versioned generation          |
+-------------------------------+--------------------------------------------------+
```

根目录只保留入口文件。旧的 `brain_agent.adapters`、`brain_agent.repository`
等兼容导入已移除；维护和测试都应直接使用上表中的分组路径。

## 16. 对外介绍时的一句话版本

`brain_agent` 是一个把 WorldQuant BRAIN Alpha 研究流程工程化的本地智能编排器：它把 LLM 生成、BRAIN 回测、失败诊断、局部优化、长期记忆和研究报告连接成一个可恢复、可审计、可持续改进的闭环，同时保持 submit 决策由人控制。

## 17. 当前适合强调的优势

- 从“生成很多表达式”升级为“生成假设、验证假设、学习假设”。
- 从“单次脚本运行”升级为“有状态的研究实验系统”。
- 从“回测失败靠人工猜”升级为“失败标签、修复目标、专项 optimizer”。
- 从“prompt 靠感觉改”升级为“同配置下的 Prompt A/B 证据比较”。
- 从“黑盒自动化”升级为“每个 run 都能复盘、恢复和解释”。
