# DSPARK k=5 → 3 的单变量 A/B 方案（事前计划，未执行）

> 起草：2026-09-27。**本文是计划，不是结果。** 所有"实测"字样均引自既有文档并附 `file:line`。
> 执行前需读 `docs/PITFALLS.md` 与 `fleet/README.md`。

## 0. 为什么做这个（问题陈述）

2026-09-27 用 `get_server_info` 读到线上**实际生效**的配置：

```
speculative_dspark_block_size = 5
```

而仓库里对 k=5 的记录是**互相矛盾**的：

| 来源 | 说法 | 位置 |
|---|---|---|
| 实测结论（负面） | "**采样生成（temp>0）下 block 5 是负优化**：多出的 2 个草稿槽只换来 +0.48 accepted token，却让每步贵 14%。仅在 greedy/高可预测文本上为正。**已复位 3**" | `DEPLOY-RECORD-dsv41.md:420` |
| 避坑清单 | `DSPARK_BLOCK_SIZE` = **3**（4-token 验证窗）；k=5 在聊天/散文上**过度起草** | `PITFALLS.md:229`、`PITFALLS.md:26` |
| 部署手册 | "`DSPARK_BLOCK_SIZE=3` 是调优值（社区也确认 k=5 会过度起草），**勿随意改**" | `DEPLOY-GUIDE.md:194` |
| 迁移记录 | 批次 2 配置 `DSPARK_BLOCK_SIZE 3 → 5`，且 §6 现行配置写 **5** | `BATCH-MIGRATION-2026-09-25.md:767`、`:135` |
| 回滚说明 | "`DSPARK_BLOCK_SIZE=3 + DSV41_VERIFY_CAP=0 + DSV41_BLOCK_VERIFY=0`（关 k=5+cap，**必须成组**）" | `BATCH-MIGRATION-2026-09-25.md:234` |

**推断（非事实）**：09-25 迁移把 k 提到 5 是因为它**在代码负载上有明显收益**（`DEPLOY-RECORD-dsv41.md:696`：C1 代码 58.71 → 74.60，+27%），而 09-19 那次"复位 3"的结论是在**散文/聊天**负载下得出的。**两个结论可能都对，只是负载不同。**

⇒ **本 A/B 要回答的唯一问题**：在**当前真实生产负载**下，k=5 是否比 k=3 更快？

> ⚠️ 负载组成是这个实验的**关键变量**，不是无关细节。见 §2 步骤 0。
> ⚠️ **若生产以采样（temp>0）为主**，按 `:420` 的结论 k=5 正在付出约 **−10%** 的成本 —— 这是本方案的价值所在。
> ⚠️ **若生产以 greedy/代码/高可预测内容为主**，k=5 可能**是对的**，本方案可能得出"维持现状"的结论 —— **那也是有效结论**。

---

## 1. 先决条件（缺一不可）

### 1.1 实验设计的前提

**`DSV41_VERIFY_CAP` 与 `DSV41_BLOCK_VERIFY` 必须与 k 成组变动**（`:234` 明确要求）。
原因在代码里：`verify_cap.py:27-28`

```python
# verify rows per request = DSPARK block size + 1 (anchor); 6 for the stock block of 5
STRIDE = int(os.environ.get("DSV41_VERIFY_CAP_STRIDE") or
             int(os.environ.get("DSPARK_BLOCK_SIZE", "5")) + 1)
```

`STRIDE` 从 `DSPARK_BLOCK_SIZE` **自动推导**（5+1=6，3+1=4），所以**不要**手工设 `DSV41_VERIFY_CAP_STRIDE` —— 让它跟着走。
但 `VERIFY_CAP=conf:0.1` 的语义（`verify_cap.py:15-18`：按置信头存活率裁剪 live 长度）本身依赖正确的 STRIDE 才能对齐 verify 矩形。

**因此两个臂必须是**：

| 臂 | `DSPARK_BLOCK_SIZE` | `DSV41_VERIFY_CAP` | `DSV41_BLOCK_VERIFY` |
|---|---|---|---|
| A（现役锚点） | **5** | `conf:0.1` | `1` |
| B | **3** | `conf:0.1` | `1` |

> **设计决策（需你确认）**：B 臂保留 `conf:0.1` + `BLOCK_VERIFY=1`，只动 k。
> 理由：本实验要隔离**k 单独**的效应。若同时关掉 cap（如 `:234` 的回滚写法），
> 就变成"k+cap 组合"的对比，**无法回答"k 该用几"**。
> 代价：cap 的阈值 `0.1` 是在 k=5 下调出来的，在 k=3 下未必最优 —— 这是本方案的
> **已知局限**，见 §6。

### 1.2 环境前提

- [ ] 三台容器 healthy、`/health=200`（`./svc.sh status`）
- [ ] `DSV41_AUTOTUNE_KEEP=1` 已开（现役是开的，`DEPLOY-RECORD-dsv41.md:774`）
- [ ] **`DSPARK_BLOCK_SIZE` 不在 `_VOLATILE` 列表里** —— 确认：
      `autotune_keep.py:23-36` 的 `_VOLATILE` 含 `DSV41_VERIFY_CAP`/`DSV41_BLOCK_VERIFY`，
      **不含** `DSPARK_BLOCK_SIZE`（它经 argv 传入，`boot.py:311`）
- [ ] 三台 `swapoff -a`（`DEPLOY-RECORD-dsv41.md:366`）

> ⚠️ **重 tune 陷阱（本实验最大的测量风险）**：
> `autotune_keep.py:41` 的 `launch_fingerprint()` payload 含 `sys.argv`，而 k 是通过
> `boot.py:311 --speculative-dspark-block-size` 传的 ⇒ **切换 k 必然改变指纹 ⇒ 每臂首次启动都会重 tune**
> （`INDEXER-CHUNKED-TP3-RESULTS.md:332-336` 已实测确认这个机制）。
> **这不影响实验有效性**（两臂各 tune 一次，地位对等），但**意味着每臂的第一轮必须丢弃**：
> 先跑一轮"热"，再测。与 `:338-340` 的建议一致。

---

## 2. 步骤

### 步骤 0（**先做，决定实验是否值得做**）：确认生产负载的采样特征

k 的收益**完全由内容决定**（`:421`：accept 实测 1.03–3.45，同样硬件单流 15→51 tok/s）。

**从服务端无法直接读到温度分布**（`temperature` 不在 `/metrics` 里 —— 已核实：`get_server_info` 与 `/metrics` 均无该字段）。
需要**业务侧确认**：线上请求里 greedy（temp=0）与采样（temp>0）各占多少？

**判据**：
- 采样为主 ⇒ **继续做本实验**（预期 k=3 更优）
- greedy/代码为主 ⇒ **也要做**，但预期可能是"维持 k=5"；重点转为验证 `:420` 的结论是否仍成立
- 混合 ⇒ 仍然做，结论按两类负载分别给

> 顺带可用的旁证：2026-09-27 实测 ITL **P50=14ms（≈71 tok/s）**、**mean=29.2ms（≈34 tok/s）**
> ⇒ **生产流量以高可预测内容为主**（P50 落在"代码/计数"档而非"散文"档），
> 但仍有约 10% 落在 ITL>60ms 的慢档。
> **这是旁证不是结论** —— accept 快也可能来自 greedy，两者都指向"k=5 未必合适"，
> 但**不能区分**，所以步骤 0 仍需业务确认。

### 步骤 1：建立基线（A 臂，k=5 现役）

**不需要重启** —— 现役就是 A 臂。

```bash
cd ~/dsv41-3xspark
URL=http://127.0.0.1:8888

# ① 记录当前配置与 autotune 状态
grep -E 'DSPARK_BLOCK_SIZE|VERIFY_CAP|BLOCK_VERIFY' .env | tee state/ab-k5-$(date +%m%d-%H%M).env
tail -50 serve-*.log | grep -E 'autotune_keep|reused|tuned and saved'

# ② 记尾延迟基线
python3 bench_tail.py $URL --reset

# ③ 跑负载（口径与仓库一致：唯一 prompt，见 README §口径 2）
python3 bench_migration.py $URL smoke . 3 300 | tee state/ab-k5-bench.txt

# ④ 取窗口分布
python3 bench_tail.py $URL --diff | tee state/ab-k5-tail.txt
```

> `bench_tail.py --reset/--diff` 是本轮新增的脚本（`scripts/bench_tail.py`），
> 用累积直方图做差取窗口分布；**引擎重启会让它主动报"不可比"**而不是给错数。

**A 臂要重复 3 次**（`README.md:58`：运行间噪声 ±3–4%，单流离散最大 ±9%）。
**记录 `accept len`**（回归哨兵，正常 **2.5–3.05**，`:715`）。

### 步骤 2：切到 B 臂（k=3）

```bash
cd ~/dsv41-3xspark
cp .env .env.bak-before-k3ab-$(date +%m%d-%H%M)      # 备份先行

sed -i 's/^DSPARK_BLOCK_SIZE=5$/DSPARK_BLOCK_SIZE=3/' .env
grep -E 'DSPARK_BLOCK_SIZE|VERIFY_CAP|BLOCK_VERIFY' .env    # 回读：k=3，cap/block_verify 不动

./svc.sh restart            # 约 13–15 分钟
```

**就绪后必须确认两件事**（否则本轮数据不可用）：

```bash
# ① k 真的生效了
curl -s $URL/get_server_info | python3 -c 'import sys,json;print("block_size =",json.load(sys.stdin)["speculative_dspark_block_size"])'
#    期望: block_size = 3

# ② 引擎健康
grep -c 'Warm-up done' serve-*.log                      # ≥1
grep -c 'Scheduler hit an exception' serve-*.log        # 0
docker ps --format '{{.Names}} {{.Status}}' | grep -c healthy   # 3
```

> ⚠️ **若启动日志出现 `engram target-verify expects one equal block per request`** —
> 这是 `engram.py:296` 的等宽断言（`DEPLOY-RECORD-dsv41.md:486`）。
> 该断言在 **compact ragged verify** 下触发；现役是 `SGLANG_RAGGED_VERIFY_MODE=static`，
> 按 `:438` 的说法 static 下该路径是 no-op。**若仍触发，立即停止本实验并回滚**（见 §5）。

### 步骤 3：丢弃"冷"轮，测"热"轮

```bash
# ① 热轮：不记录，只为让 k=3 的形状进 autotune cache
python3 bench_migration.py $URL smoke . 3 100

# ② 记尾延迟基线 + 正式测（同步骤 1 的 ③④）
python3 bench_tail.py $URL --reset
python3 bench_migration.py $URL smoke . 3 300 | tee state/ab-k3-bench.txt
python3 bench_tail.py $URL --diff | tee state/ab-k3-tail.txt
```

同样**重复 3 次**。

### 步骤 4：质量门（**必做，不能省**）

每臂重启后跑（`:714`：每批重启后必跑）：

```bash
python3 ~/quality_gate.py        # worker 上；6 项可判定任务 + 乱码检测
```

**判据**：6/6 通过。**质量门不过 ⇒ 该臂作废**，无论速度多好。

> ⚠️ **只看速度会误判**（`:715-716`）：上游的失败模式是 accept **塌到 1.0 且输出变垃圾，
> 而 tok/s 反而"变快"**。所以 accept len 与质量门是**必须同时看**的。

---

## 3. 判据（什么算"赢了"）

按 `README.md:58` 的噪声下限：**<4% 的差异不算真实变化**。

| 指标 | 主/辅 | 阈值 |
|---|---|---|
| **C1 散文 sampled** | **主** | >4% 且方向与 `:420` 一致 |
| **C1 散文 greedy** | **主** | 同上（`:420` 预测此档 k=5 更优） |
| C1 代码 greedy | 辅 | 记录，解释差异来源 |
| C4 聚合 | 辅 | 记录 |
| **accept len** | **哨兵** | 必须 2.5–3.05；塌到 1.0 ⇒ 该臂作废 |
| TTFT/ITL 分位数 | 辅 | `bench_tail.py --diff` 的窗口值 |
| 质量门 | **闸门** | 必须 6/6 |

**四种可能结论（都要如实报告）**：

1. B 在采样档显著更优、greedy 档更差 ⇒ **按负载配比决定**，或考虑按请求路由（本栈不支持，只能二选一）
2. B 全面更优 ⇒ 采用 k=3
3. A 全面更优 ⇒ **维持 k=5**，并把 `PITFALLS.md:229` / `DEPLOY-GUIDE.md:194` 的"k=3"标注为**在 09-25 栈上已过时**
4. 差异落在噪声内 ⇒ **维持现状**，并把"无结论"写进文档

---

## 4. 回滚

```bash
cd ~/dsv41-3xspark
cp state/.env.bak-before-k3ab-<时间戳> .env      # 或直接 sed 回 5
sed -i 's/^DSPARK_BLOCK_SIZE=3$/DSPARK_BLOCK_SIZE=5/' .env
./svc.sh restart
```

- **改 k 只动 `.env` + 重启，不需要 `./start.sh build`**
  （对比：`adapter/` 改动必须重建镜像 —— `INDEXER-CHUNKED-TP3-RESULTS.md:36-40`）
- **无不可逆操作**，`DSPARK_BLOCK_SIZE` 不在 `_VOLATILE` 里，回滚后行为等同原始 A 臂

---

## 5. 风险与止损

| 风险 | 触发信号 | 止损 |
|---|---|---|
| **长上下文 OOM / 主机挂死** | `NV_ERR_NO_MEMORY`、三台无响应 | 立即断电重启；这是**唯一灾难性**模式（`INDEXER-CHUNKED-TP3-RESULTS.md:166-168`） |
| engram 等宽断言 | 日志 `engram target-verify expects one equal block` | 停实验，回滚到 k=5 |
| 质量退化 | 质量门 <6/6 或 accept 塌到 1.0 | 该臂作废，回滚 |
| 混合 build | worker 镜像与 head 不一致 | 本方案**不涉及** adapter，风险低；但重启前确认三台镜像 id 一致 |

> ⚠️ **本实验不碰长上下文**：只跑 4k 档（与 k 的效应正交）。
> 长上下文是独立问题（`:809-811` 列为最高优先遗留），**不要混在同一个实验里**。

---

## 6. 已知局限（写清楚，避免过度解读）

1. **`conf:0.1` 阈值是在 k=5 下调的**，在 k=3 下未必最优（§1.1 的设计决策）。
   若要排除这个混杂因素，需追加一个 **k=3 + cap 关闭**的第三臂 —— 但那会变成
   "k 与 cap 的组合"对比，**两臂单变量性被破坏**，且违反 `:234` 的"必须成组"。
   **本方案选择保留 cap、接受这一局限**，并把"k=3 下 cap 的最优阈值"留作后续独立实验。
2. **autotune 每臂各 tune 一次**（k 在 argv 里 ⇒ 指纹变）。已用"热轮丢弃"缓解，
   但两臂的 tune 结果不完全可比 —— 这是 `:338-340` 早已记录的固有限制。
3. **负载组成不可控**：生产流量在实验期间会变。`bench_tail.py --diff` 的窗口里
   若混入真实用户请求，其分布也被计入 —— 这是累积计数器方法的固有局限。
   **缓解**：两臂在**同一时段**、同样负载脚本下比；必要时在低峰期做。
4. **`bench_migration.py` 的 C1 代码档**是 09-25 中途加入的（`DEPLOY-RECORD-dsv41.md:689-691`），
   跨口径不可比。本次两臂都用同一版本脚本，**内部可比**。

---

## 7. 产出物清单

执行后应归档到 `docs/`（与本仓库既有风格一致）：

- [ ] `docs/K3-VS-K5-AB-RESULTS.md`：两臂的逐轮数字、accept len、质量门、结论
- [ ] 原始日志：`state/ab-k5-*.txt`、`state/ab-k3-*.txt`、`state/ab-k*.env`
- [ ] 更新 `README.md` §8「已知限制」：k 的结论
- [ ] 更新 `PITFALLS.md:229` / `DEPLOY-GUIDE.md:194`：若结论与"k=3 是调优值"不符，**必须**更正
      （否则下次还会有人在同样的问题上踩坑）

---

## 8. 工作量估算

| 阶段 | 时间 |
|---|---|
| 步骤 0（业务确认负载配比） | 取决于你 |
| A 臂（无需重启）+ 3 轮 | ~40 分钟 |
| B 臂重启 + 热轮 + 3 轮 | ~25 分钟 + 15 分钟重启 |
| 质量门 ×2 | ~20 分钟 |
| **合计** | **约 2 小时**（含一次重启） |

> 若步骤 4 得出"差异在噪声内"，可再补一轮（两臂各 +3 次）以缩小置信区间；
> 但按 `README.md:58` 的 ±3–4% 运行间噪声，**要把 4% 的效应检定出来需要相当多的重复**——
> 建议先做一轮，若差异明显（>8%）即可下结论；若在 4–8% 之间，**如实报告"边缘，需更多数据"**，
> 不要强行下结论。