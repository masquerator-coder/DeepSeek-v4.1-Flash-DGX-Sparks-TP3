# indexer_chunked(v1) 真机实测结果（3× DGX Spark · TP=3）

> 执行日期：**2026-09-26**，集群 `fq-dgx-01/02/03`，内核 `6.17.0-1031-nvidia` / 驱动 `580.173.02`。
> 前置方案与草稿见 [`INDEXER-CHUNKED-TP3-PLAN.md`](INDEXER-CHUNKED-TP3-PLAN.md)。
> 原始证据全部归档在 head `~/dsv41-3xspark/indexer-test-20260926/`。

## 0. 结论

**迁移技术上是成功的（可移植、质量零退化、性能中性）；但它的"收益假设"经实测基本落空。**

| 维度 | 结论 | 关键证据 |
|---|---|---|
| 能否移植 | ✅ 三个 rank 全部 `ARMED` | TP0/TP1/TP2 各一条 ARMED 日志 |
| 质量 | ✅ **零退化**（75 题配对，BROKE=0） | McNemar `p=1.000`，各分类 0 break |
| 解码性能 | ✅ 无可测影响 | 四臂 A/B，同 chunk 内 OFF/ON 的 delta 换号（§9.1）|
| 预填性能 | ✅ 无可测影响；chunk 1024 的 +11~15% 与 indexer **无关** | `off1024`=2225 vs `on1024`=2223（§9.2）|
| 长上下文 | ⚠️ **不是必要条件**：`OFF+1024` 到 200k 也全过 | 4/4 通过，无 OOM（§9.3）|

> ### ⚠️ 最重要的结论是负面的
> 最初的问题是"上游更新能不能给 TP3 带来优化"。实测下来，这个 backport：
> **不提升预填、不提升解码、也不是 chunk 1024 在长上下文可用的前提**；
> 它**只是无害**（质量零退化）。chunk 1024 本身的 +11~15% 预填收益**与它无关**。
>
> 留它的唯一理由是 **>200k 的余量保险**（ON 测到 254,811，OFF 只测到 200k）——
> 这一点**未验证**，见 §10。

---

## 1. ⚠️ 实测中发现的部署机制（本次最大意外）

**adapter 不是 bind-mount，而是烘焙进镜像的。**

- `fleet/Dockerfile:5` → `COPY adapter /opt/dsv41/adapter`
- 容器的 mounts 只有 `models / state / .cache / engram`，**没有 adapter**

后果：**只改 head 的 `~/dsv41-3xspark/adapter/` 并重启服务是无效的**——第一次重启后
容器里仍是旧的 15 个 adapter 文件、`sitecustomize.py` 无补丁，但因 `DSV41_INDEXER_CHUNKED=1`
已由 `start.sh` 传入而**没有任何报错**，表现为"配置生效了、代码没生效"的静默失败。

**正确流程**：改 adapter → **`./start.sh build`（重建三台镜像）** → `./svc.sh stop && ./svc.sh start`。

> 建议把这条写进 `fleet/README.md`（它目前只说"Dockerfile/boot.py/.env 改动需重建镜像或重起"，
> 未点明 adapter 也在镜像内、**只重启不够**）。

### 1.1 其它实测确认的机制

- **worker 的 `.env` 不需要改**：head 在发送 worker 启动命令时已把变量**解析成实际值**，
  实测两个 worker 容器内 `DSV41_INDEXER_CHUNKED=1` 均正确。
- **`.env` 被 build 的 rsync 排除**（`--exclude '.env'`），各节点保留自己的 `.env`。
- 容器内 adapter 路径为 `/opt/dsv41/adapter`（`sys.path` 实测确认）。

---

## 2. 落地改动（4 处，全部可回滚）

| 文件 | 改动 | 前 → 后 md5 |
|---|---|---|
| `adapter/indexer_chunked.py` | **新增**（上游 v1，11139 B） | — → `12729717c083605a03455ad92c0ce657` |
| `adapter/sitecustomize.py` | **+15 行纯增补**（1 个 `elif` + finder 白名单 1 行） | `8bf86e54…` → `3e782fa8…` |
| `start.sh` | 2 个变量透传（head + worker 两处，worker 行 `-e` token 23→25） | `bf07deab…` → `dad342df…` |
| `.env` | `DSV41_INDEXER_CHUNKED=1`（后按 §4 改 `CHUNKED_PREFILL_SIZE=1024`） | `7e9edff9…` → `1e4042a0…` |

关键实现点：新 `elif` **必须放在 `else` 之前**，因为该 `else` 分支无条件读
`module.PagedIndexerMetadata`，而 `deepseek_v4_backend` **也有**这个属性——只加 finder 白名单
而不加 `elif` 会静默误伤（实测确认该属性存在）。

备份：`adapter/sitecustomize.py.bak-before-indexer-20260926-115619`、
`.env.bak-before-indexer-20260926-115619`、镜像 `dsv41-3xspark:backup-before-indexer`。

---

## 3. 质量门（配对，n=75）

`qeval.py`（上游工具，55 primary + 20 secondary），并发 4：

```
PRIMARY code+reason+math      53/55 ->  53/55   kept 53  BROKE 0  fixed 0  both-fail 2  p=1.000
secondary json+format         13/15 ->  13/15   kept 13  BROKE 0  fixed 0  both-fail 2  p=1.000
guard prose (degeneration)     5/5 ->   5/5    kept  5  BROKE 0  fixed 0  p=1.000

category   kept  broke  fixed  both-fail
code         24      0      0          1
format        7      0      0          0
json          6      0      0          2
math         13      0      0          1
prose         5      0      0          0
reason       16      0      0          0
```

**ON 与 OFF 的通过/失败集合完全一致（BROKE=0、fixed=0）**，连分类分布都逐项相同。
这是本 backport 不改变数值行为的最强证据。

> 注意 harness 自带的告警：并发 >1 时个别翻转可能是 batching 噪声；但本次**零翻转**，
> 噪声无从体现。

---

## 4. 性能（同口径，唯一 prompt）

### 4.1 三臂对照（`CHUNKED_PREFILL_SIZE=768`）

| 指标 | baseline（无此变量） | OFF（`=0`） | ON（`=1`） |
|---|---|---|---|
| C1 散文 sampled | 33.54 | 34.43 | 34.77 |
| C1 散文 greedy | 35.20 | 36.15 | 36.54 |
| C1 代码 greedy | 81.50 | 78.54 | 79.88 |
| C4 并发聚合 | 76.44 | 74.86 | 76.57 |
| 预填 ≈4k（扣解码） | 2061 | 1983 | 1950 |

**关键控制组**：`OFF` 的代码路径与 `baseline` **逐字节等价**（adapter 存在但未安装），
两者却相差 **−3.8%**（预填）、−3.6%（代码）——这就是**启动间 + 重 tune 的噪声下限**。
ON 与 OFF 的全部差异 ≤2.3%，**落在噪声内**。

⇒ **本 backport 在 768 档位对性能无可测影响。**

### 4.2 ⚠️ 「开关本身会触发重新 autotune」——一个真实的测量陷阱

`DSV41_INDEXER_CHUNKED` **不在** `autotune_keep.py` 的 `_VOLATILE` 列表里，因此：

| 启动 | autotune 行为 |
|---|---|
| 首次带 `=1` 启动 | **`tuned and saved`**（重 tune） |
| 其后同配置启动 | `reused` |
| 改成 `=0` 后启动 | **`tuned and saved`**（又重 tune） |

每次切换开关都会改变 launch fingerprint → 强制重 tune → 引入性能噪声。
**建议把 `DSV41_INDEXER_CHUNKED` 加入 `_VOLATILE`**：它与 `DSV41_DRAFT_HEAD_FP8` /
`DSV41_ENGRAM_PREFETCH` 同类（都不触及 FlashInfer 调优的算子形状），
上游已经为后者做过同样处理（`autotune_keep.py:26-28` 的注释说明了这条准则）。

### 4.3 `CHUNKED_PREFILL_SIZE=1024` 的收益（~~已实测拿回~~ ⛔ 已被 §9.2 推翻，勿引用）

> ⛔⛔ **本节结论已作废，禁止引用。正确结论见 §9.2。**
> 本节标题里的"**已实测拿回**"和正文的"**比基线高 +6.6%**"是**错的**——当时把"预填变快"归给了 indexer。
> 四臂 A/B（§9.2）显示 **`off1024`（indexer 关闭）同样有 2225 tok/s**，
> 即收益来自 **chunk 大小本身**，与 indexer 无关。
> 下表数字仍有效（是当时的实测），但**归因（"是 indexer 拿回了 −6.3%"）是错的，不要引用**。

| 配置 | 预填 ≈4k 档（扣解码） |
|---|---|
| baseline `chunk=768` | 2061 |
| ON `chunk=768` | 1950 |
| **ON `chunk=1024`** | **2197** |

即：`chunk=1024` 不仅抵消了之前的 −6.3%，还**比基线高 +6.6%**。
（**修正**：这 +6.6% 是 chunk 大小的功劳，不是 indexer 的 —— 见 §9.2。）

> **只想看结论的人看这里**（§4.3 的正确版本）：`chunk` 768→1024 的预填收益约 **+11~15%**
> （`off768` 1999 → `off1024` 2225），**与 indexer 无关**（ON 臂同样 2223）。

---

## 5. 长上下文（本次移植的目的）

`CHUNKED_PREFILL_SIZE=1024` + indexer ON，单请求逐档递增：

| 目标 | 实测 `prompt_tokens` | wall | 预填 | 结果 |
|---|---|---|---|---|
| 32k | **108,482** | 58.05 s | 1869 tok/s | ✅ |
| 64k | **217,083** | 128.36 s | 1691 tok/s | ✅ |
| ~255k | **254,811** | 158.33 s | 1609 tok/s | ✅ |
| （更高） | 324,899 / 433,957 | — | — | HTTP 400：超 `max_model_len=262144`（**非 OOM**） |

**254,811 token 完成，占 262144 上限的 97%。**

这正是 README §8 记录的爆点（"上游在该解码栈 + chunk 1024 下被 ~200k prompt 打爆，
`NV_ERR_NO_MEMORY`，主机挂死需硬复位"）。本次**未发生 OOM，未硬复位**，压测后
`/health` 仍 200、三台容器 healthy。

> 残余不确定性：这是**单次成功**，未做多并发 / 重复长请求的稳健性验证（见 §7）。

---

## 6. 当前线上状态

```
EP_SIZE=1
CHUNKED_PREFILL_SIZE=1024        # 768 -> 1024（本次实测验证后调回）
DSV41_INDEXER_CHUNKED=1
MEM_FRACTION_STATIC=0.95
```
镜像 `dsv41-3xspark:local` 三台均已含 indexer（16 个 adapter .py）；
三个 rank 均 ARMED；`/health`=200，`via NET/IB` 64 条，`reg_mr` 失败 0。

### 回滚（任一粒度）

```bash
cd ~/dsv41-3xspark
# ① 只关功能（不需要重建镜像，最快）
sed -i 's/^DSV41_INDEXER_CHUNKED=1$/DSV41_INDEXER_CHUNKED=0/' .env && ./svc.sh stop && ./svc.sh start
# ② 还原 sitecustomize 与 .env
python3 indexer-test-20260926/_deploy_apply_indexer_patch.py --revert
cp .env.bak-before-indexer-20260926-115619 .env
rm -f adapter/indexer_chunked.py
./start.sh build && ./svc.sh stop && ./svc.sh start     # 必须重建镜像（见 §1）
# ③ 连镜像一起回退
docker tag dsv41-3xspark:backup-before-indexer dsv41-3xspark:local   # 三台都要
```
`CHUNKED_PREFILL_SIZE` 单独回 768：改 `.env` 后重启即可。

---

## 7. 未验证项（不要当成已知）

1. **长上下文只做了单次抽样**：未测多并发长请求、未测连续多次 ~200k 请求；
   `chunk=1024` 的稳健性仍弱于已验证的 `chunk=768`。
2. **性能对照受 autotune 重 tune 污染**（§4.2）：三臂各自独立 tune，
   因此 §4.1 的绝对值不能当作"indexer 带来的差异"；只有 OFF-vs-baseline 那个
   −3.8% 的"噪声下限"推断是稳的。要拿到干净的 A/B，需先做 §4.2 的 `_VOLATILE` 改动。
3. **质量门并发为 4**：未做 concurrency=1 的确定性复核（harness 自身也提示了这点）。
4. **未做 768 档的长上下文对照**：即"768 下 255k 是否也会成功"没测，
   因此不能断言"是 indexer 让 1024 变得可行"——只能说**在 indexer + 1024 下确实可行**。
5. **`worker` 侧 nvidia-smi 未取到显存数**（GB10 统一内存下该查询返回 `[N/A]`），
   故没有显存余量的直接读数，收益是间接推断的。
6. 未验证 `DSV41_INDEXER_LOGITS_BUDGET_BYTES` 的实际作用（用了默认 2 GiB）。

---

## 8. 后续收尾（2026-09-26 同日完成）

本轮解决 §4.2 的测量陷阱、补齐文档与归档，并补做长请求稳健性验证。

### 8.1 ①`DSV41_INDEXER_CHUNKED` 加入 `_VOLATILE`（已实证修复）

补丁：`adapter/autotune_keep.py`，把该开关加入 `_VOLATILE`（保留原文件 CRLF 行尾），
生成器 `scripts/patch_autotune_volatile.py`（幂等、带 `--revert`）。
md5 `f47d7514…` → `279bf4c7…`；`./start.sh build` 重建三台镜像（镜像内 `grep -c
DSV41_INDEXER_CHUNKED` = 1，三台一致）。

**判据是"切换开关后仍 `reused`"，双向各验一次**：

| 启动 | sidecar 由谁写入 | 观测到的 autotune 行为 |
|---|---|---|
| 修复后首次（`=1`） | — | `tuned and saved`（指纹变更一次，**预期**） |
| `=0` | 上面那次 `=1` 的启动 | **`reused`** ✅ |
| `=1` | 上面那次 `=0` 的启动 | **`reused`** ✅ |

⇒ 开关不再影响 launch fingerprint：**切换不再触发重 tune**，也为将来做干净的 A/B 扫清障碍。

### 8.2 ②文档：`fleet/README.md` 补「adapter 在镜像内」

把原来的"两条纪律"第 2 条扩成按改动位置分类的生效方式表，并明确
**`adapter/` 下任何文件都必须 `./start.sh build`**，附根因（`Dockerfile:5` 的 `COPY`、
容器无 adapter mount）与本次静默失败实例，以及两条最省时的排查命令。

### 8.3 ③归档更正：`fleet/start.sh` 与 `fleet/adapter/autotune_keep.py`

| 文件 | 更正前 | 更正后 |
|---|---|---|
| `fleet/start.sh` | `13512310…` / 889 行（batch-1 之前） | `dad342df…` / 903 行 = head 现状 |
| `fleet/adapter/autotune_keep.py` | `25da1988…`（无 volatile 改动） | `23bc0fa9…`（LF 归一） |

同时更正了 `fleet/README.md` 里"**逐字节一致**"的说法：`fleet/` 被 `.gitattributes` 钉成 LF，
而 head 上有 3 个文件是 CRLF（`autotune_keep.py` 104、`verify_cap.py` 314、`wo_a_w8.py` 466），
故这几个文件的 md5 **本来就不等于 head 原始字节**——新增「行尾」一节说明，并给出"比 md5 前先归一行尾"的核对方法。

`NOTICE` 的 AGPL 对应源码声明同步更新：本地修改从 2 个增至 3 个（加 `autotune_keep.py`），
adapter 总数 16 → 17，`start.sh` 透传 12 → 14。

### 8.4 ④长请求稳健性（`CHUNKED_PREFILL_SIZE=1024` + indexer ON）

脚本 `scripts/bench_longctx.py`（按既有 `bench_*` 约定入库；跑在 worker 上、指向 head API），
证据 `indexer-test-20260926/robust-on1024.json`：

```
A) 顺序 5 次 × ~200k token
  #1: pt=200033 wall=113.7s prefill=1759 tok/s  health=200
  #2: pt=199533 wall=108.1s prefill=1846 tok/s  health=200
  #3: pt=199659 wall=108.7s prefill=1837 tok/s  health=200
  #4: pt=200054 wall=108.7s prefill=1841 tok/s  health=200
  #5: pt=199999 wall=108.3s prefill=1846 tok/s  health=200

B) 并发 3 × ~80k token（约 240k token 同时在飞）
  worker0: 80112 tok wall=114.49s
  worker1: 79888 tok wall=114.43s
  worker2: 79752 tok wall=114.42s
  总 wall=114.5s  结束 /health=200
```

**5/5 + 3/3 全部成功**，且：
- 顺序 5 次的 wall 离散度仅 **5.2%**（108.1–113.7s）——不是"侥幸过一次"；
- 并发 3 × 80k 的总 wall（114.5s）与单次 200k（108–114s）几乎相同，说明批处理有效；
- 每一步之后 `/health` 都是 200，无渐进性退化。

⇒ §7.1 的"单次抽样"不确定性**已基本消除**（仍有 §9 的残余项）。

---

## 9. 干净四臂 A/B（2026-09-26 收盘，**修正了 §4.3 的归因**）

§4.2 的陷阱修好后（`DSV41_INDEXER_CHUNKED` 入 `_VOLATILE`），重跑 `{OFF,ON} × {chunk 768,1024}`。
每臂都重启、并记录 autotune 判定。原始日志：`indexer-test-20260926/ab4arm*.log` 与 `ab4arm-logs/`。

| 臂 | autotune | c1_sampled | c1_greedy | c1_code | c4_greedy | 预填（≈4k，扣解码） |
|---|---|---|---|---|---|---|
| off768 | tuned\* | 33.86 | 35.25 | 81.38 | 74.63 | 1999 |
| on768 | **reused** ✅ | 33.81 | 35.83 | 78.42 | 73.96 | 1937 |
| off1024 | tuned\* | 32.47 | 35.10 | 79.26 | 76.52 | **2225** |
| on1024 | **reused** ✅ | 35.35 | 33.59 | 79.28 | 76.52 | **2223** |

\* `tuned` 两臂是**切换 chunk 导致**的，不是开关导致的 —— 见 §11.2。

### 9.1 结论一：indexer 对性能**无可测影响**

同 chunk 内（共用同一份 autotune cache，**这才是干净的对照**）：

| 指标 | 768: OFF→ON | 1024: OFF→ON |
|---|---|---|
| c1_sampled | 33.86 → 33.81 (−0.1%) | 32.47 → 35.35 (+8.9%) |
| c1_greedy | 35.25 → 35.83 (+1.6%) | 35.10 → 33.59 (−4.3%) |
| c1_code | 81.38 → 78.42 (−3.6%) | 79.26 → 79.28 (+0.0%) |
| c4_greedy | 74.63 → 73.96 (−0.9%) | 76.52 → 76.52 (0.0%) |
| 预填 | 1999 → 1937 (−3.1%) | 2225 → 2223 (−0.1%) |

**delta 在两组之间换号**（预填 −3.1% vs −0.1%；c1_greedy +1.6% vs −4.3%；
c1_sampled −0.1% vs +8.9%）⇒ 差异由**运行间噪声**主导，不构成"indexer 有性能影响"的证据。

⇒ **本 backport 在性能上是中性的。它的价值在显存安全，不在速度。**

### 9.2 ⚠️ 结论二：预填的收益来自 **chunk 大小**，不是 indexer（**修正 §4.3**）

§4.3 曾把 "chunk=1024 下预填 2197 > 基线 2061" 归因给 indexer。**这个归因是错的。**

| chunk | OFF 预填 | ON 预填 |
|---|---|---|
| 768 | 1999 | 1937 |
| 1024 | **2225** | **2223** |

**`off1024`（indexer 关闭）就已有 2225 tok/s** —— 比 768 档高 **+11.3%**。
即 chunk 1024 的预填收益（约 +11~15%）**与 indexer 无关**；C4 也同向（+2.5%/+3.5%）。

**为什么两臂不是全部 `reused`**：`boot.py:297` 把 chunk 作为 **argv** 传给引擎
（`--chunked-prefill-size <n>`），而 `launch_fingerprint()` 的 payload 含 `sys.argv`
（`autotune_keep.py`）。所以切换 chunk 必然改变指纹、触发一次重 tune。
这**与 indexer 那个问题不同**：chunk 会改变预填 batch 形状，重 tune 是**合理的**，
不应把它也塞进 `_VOLATILE`。

⇒ 将来做跨 chunk 的性能比较时，**必须**在每档先跑一轮"热"（让该 argv 下的形状进 cache），
否则比的是 tune 噪声。本次两档各只跑了一次测量，故"768 vs 1024"的绝对值仍含一次重 tune 的影响；
**但同 chunk 内的 OFF/ON 对照是干净的**。

### 9.3 关键对照：`OFF + chunk 1024` 在长上下文下**也不会挂**（已实测）

为回答"既然 indexer 对速度中性，为什么还留它"，做了直接对照：`OFF + chunk 1024`，
分级递增（100k→130k→160k→200k），遇错即停（避免把机器打到硬复位）。

| 档位（目标） | 实测 prompt_tokens | wall | 预填 | 结果 |
|---|---|---|---|---|
| ~100k | 99,957 | 53.6 s | 1864 tok/s | ✅ |
| ~130k | 129,616 | 69.0 s | 1880 tok/s | ✅ |
| ~160k | 160,183 | 87.3 s | 1835 tok/s | ✅ |
| ~200k | 200,032 | 120.7 s | 1658 tok/s | ✅（GPU 96%）|

**4/4 全过，全程 `/health`=200，无 OOM、无挂死。**

⇒ **在当前这套栈上，indexer 对 chunk 1024 的可用性也不再是必要条件**（至少到 200k）。

**这推翻了最初的问题设定。** 回头梳理：

| 主张 | 状态 |
|---|---|
| indexer 能提升预填/解码性能 | ❌ **否**（§9.1：delta 换号，噪声主导）|
| indexer 是 chunk 1024 提速的来源 | ❌ **否**（§9.2：OFF 同样 2225）|
| indexer 是 chunk 1024 在长上下文不 OOM 的前提 | ❌ **未成立**（§9.3：OFF 到 200k 也全过）|
| indexer 不损害质量 | ✅ 是（§3：BROKE=0，p=1.000）|

**注意口径的边界**（不要把上表读成"已证无用"）：
- OFF 只测到 **200k**；ON 测到 **254,811**（97% context）。**200k 以上无 OFF 数据**，
  故"极高上下文下 indexer 是否提供余量"**仍未排除**。
- OFF 只做了 **4 次顺序**请求；没有像 ON 那样做 **并发**（3×80k）验证。
- README §8 记录的 OOM 是**上游那套栈**（引擎 `37939c26`、无本地若干 adapter）的现象；
  本栈是 `da64c5cbb` + wo_a_w8/verify_cap/engram_prefetch 等一堆本地改动，
  显存行为已不同——**indexer 的原始动机可能已被别处的改动覆盖**。

**建议**：indexer 目前"中性且无害"（质量零退化、性能中性、自守卫会在 build 漂移时
大声拒启动）。可留作 >200k 的余量保险，也可移除以减少一个 249 行的上游 backport 维护面。
**在有 >200k 的 OFF 对照之前，不建议宣称它无用。**

---

## 10. 仍未验证

1. **>200k 的 OFF 对照**：`OFF+1024` 只测到 200,032 token；ON 测到 254,811。
   故"极高上下文下 indexer 是否提供额外余量"**仍未排除**——这是决定"留还是删"的唯一剩余问题（§9.3）。
2. **OFF 档的并发未测**：ON 做过 3×80k 并发，OFF 只做了 4 次顺序请求。
3. **极端并发未测**：更高并发 × 更长上下文（如 4 × 200k）未测。
4. `DSV41_INDEXER_LOGITS_BUDGET_BYTES` 的实际作用仍未验证（一直用默认 2 GiB）。
5. GB10 统一内存下 `nvidia-smi` 显存查询返回 `[N/A]`，故仍无显存余量直接读数。
   （`highctx_shot.py` 里的利用率读数可用：200k 档到 **96%**，说明已接近实际边缘。）

> 已解决：§7.3 的性能重 tune 问题由 §9 的干净四臂 A/B 解决；
> §7.4「不能断言是 indexer 让 1024 变可行」由 §9.3 直接回答（**不是它**）。

---

## 11. 本轮结束时的线上状态

```
EP_SIZE=1
CHUNKED_PREFILL_SIZE=1024
DSV41_INDEXER_CHUNKED=1
```
- 三台镜像均含 volatile 修复（实测 `grep -c` = 1）
- 三个 rank 全部 ARMED
- 三台容器 healthy，`/health`=200，`via NET/IB` 64 条，`reg_mr` 失败 0
- `start.sh` `dad342df…` / `sitecustomize.py` `3e782fa8…` / `autotune_keep.py` `279bf4c7…` / `.env` `1e4042a0…`

**回滚**：只需把 `.env` 的 `DSV41_INDEXER_CHUNKED` 改回 `0` 再重启（`autotune_keep` 的
volatile 改动是纯收益、无需回退）；完整回退见 §6。
