# 3× DGX Spark 部署 DeepSeek-V4.1-Flash（TP=3）· RoCE

三台 DGX Spark（GB10）用 CX7 直连成**三角**拓扑，跑 DeepSeek-V4.1-Flash 推理服务
（`max_model_len=262144`、`max_total_num_tokens=750000`）。

## 当前状态（2026-09-27 实测）

配置：`EP_SIZE=1` · `CHUNKED_PREFILL_SIZE=1024` · `MEM_FRACTION_STATIC=0.95` · `NCCL_NET=IB`

**现役启动参数的权威读数**（`curl -s <url>/get_server_info`，见 §8.1）：

| 参数 | 实测值 | 参数 | 实测值 |
|---|---|---|---|
| `tp_size` / `ep_size` | 3 / 1 | `page_size` | 256 |
| `chunked_prefill_size` | 1024 | `kv_cache_dtype` | `fp8_e4m3` |
| `context_length` | 262144 | `attention_backend` | `dsv4` |
| `max_running_requests` | **8** | `moe_runner_backend` | `flashinfer_mxfp4` |
| `max_total_tokens` | 750000 | `fp8_gemm_runner_backend` | `flashinfer_cutlass` |
| `max_total_num_tokens`（KV 池） | 749824 | `speculative_algorithm` | `DSPARK` |
| `mem_fraction_static` | 0.95 | `speculative_dspark_block_size` | **5** ⚠️ 见 §8.2 |
| `schedule_policy` | `fcfs` | `min_free_slots_delay` | 1 |
| `watchdog_timeout` | 1800 | `enable_metrics` | `True` |

> ⚠️ `max_running_requests=8`、`max_total_tokens=750000`、`min_free_slots_delay=1` 三项
> **只存在于线上 `.env`，本仓库的 `env.example` 未收录**（`env.example` 仍是旧档，见 §8.5）。
> 要复现现役行为，必须显式设置这三项。

### A. 预填（prefill）实测

口径：**非流式 `usage.prompt_tokens`**，**每题唯一随机 prompt**（防 radix 前缀复用），
**已扣解码时间**（先从 wall 里减掉 `completion_tokens ÷ 实测解码速率`）。脚本 `scripts/bench_prefill.py`。

| prompt 长度（实测 `prompt_tokens`） | 预填 tok/s | 证据 |
|---|---|---|
| ≈4k 档（唯一 prompt） | **2223**（同口径四臂 A/B：`off1024` 2225 / `on1024` 2223） | [`INDEXER-CHUNKED-TP3-RESULTS.md:294-299`](docs/INDEXER-CHUNKED-TP3-RESULTS.md#L294-L299) |
| ~100k（99,957） | 1864 | [`…RESULTS.md:343-348`](docs/INDEXER-CHUNKED-TP3-RESULTS.md#L343-L348) |
| ~130k（129,616） | 1880 | 同上 |
| ~160k（160,183） | 1835 | 同上 |
| ~200k（199,533–200,054，顺序 5 次） | **1840**（1759–1846，离散度 5.2%） | [`…RESULTS.md:262-267`](docs/INDEXER-CHUNKED-TP3-RESULTS.md#L262-L267) |
| ~255k（**254,811**） | **1609** | [`…RESULTS.md:157-161`](docs/INDEXER-CHUNKED-TP3-RESULTS.md#L157-L161) |

> **与上游对照**：上游 TP3 首页只给了一句 `Prefill, 3 Sparks (TP=3) | ~2,000 tok/s`（**无口径**）。
> 我们 ≈4k 档 **2223**，比它高约 **11%**；但这 +11% 几乎全部来自 **`chunk` 768→1024** 这一个旋钮
> （`off768` 1999 → `off1024` 2225，**+11.3%，且与 indexer 无关** —— [`…RESULTS.md:316-336`](docs/INDEXER-CHUNKED-TP3-RESULTS.md#L316-L336)）。
> 长档（200k / 255k）上游 TP3 **未给任何数**，无对照。

### B. 解码（decode）实测

口径：**非流式 `usage.completion_tokens ÷ 整请求 wall`**，**每题唯一 prompt**，256 token 输出。
脚本 `scripts/bench_migration.py`。**单流速度强依赖负载内容**（投机解码接受率），故必须写清类型。

| 负载 | 并发 | 实测 tok/s | 证据 |
|---|---|---|---|
| 散文 greedy | C1 | **33.6** | [`…RESULTS.md:294-299`](docs/INDEXER-CHUNKED-TP3-RESULTS.md#L294-L299) |
| 散文 sampled | C1 | **35.3** | 同上 |
| 代码 greedy | C1 | **79.3** | 同上 |
| 散文 greedy | C4 聚合 | **76.5** | 同上 |
| 散文/计数（高可预测内容） | C1 | 最高 **87** | 见下方 [报数必带口径](#e-报数必带口径) |
| socket 兜底档对照（RoCE 未修好时，仅作对照） | C1 / C4 | 17.9–19.2 / 39.7–43.3 | [`DEPLOY-RECORD:206-207`](docs/DEPLOY-RECORD-dsv41.md#L206-L207) |

### C. 其它

| 指标 | 实测值 |
|---|---|
| 长上下文可用上限 | **254,811 token 实测通过**（262144 的 97%）|
| 并发长请求 | 3 × ~80k 并发总 wall 114.5 s（与单次 200k 的 108–114 s 几乎相同）|
| fabric `all_reduce` 256 MB | 13.86 GB/s |
| 三台容器 | healthy · `via NET/IB` 64 条 · `reg_mr` 失败 0 |

### D. 尾延迟（2026-09-27 实测，**生产流量**）

口径：引擎自带的 Prometheus 直方图，**自启动以来累积的 2403 个真实请求**——
不是压测样本，是线上实际负载。脚本 `scripts/bench_tail.py`（本仓库，只读）。

> ⚠️ **这是一个负载窗口的快照，不是稳态 SLA**。直方图是**累积**的，随流量构成漂移
> （复采一次：n 2403→2483，TTFT P99 114.2→111.3 s、ITL mean 29.2→28.3 ms —— 同量级但非定值）。
> 要比较两个配置，用 `bench_tail.py --reset` / `--diff` 取**同一窗口**的增量，
> 并确保两臂在同一时段、同样负载下测。

| 指标 | P50 | P90 | P99 | mean |
|---|---|---|---|---|
| TTFT（streaming） | **0.98 s** | 5.90 s | **114.2 s** | 6.06 s |
| E2E（streaming） | 2.94 s | 27.60 s | **185.0 s** | 12.28 s |
| ITL（逐 token） | 14 ms | 32 ms | 138 ms | **29.2 ms** |
| **queue_time** | 0.001 s | 0.005 s | **0.039 s** | 0.067 s |

**派生比率**（同窗口）：

| 指标 | 值 |
|---|---|
| 平均 prompt | **64,580** token |
| 平均 uncached | 8,566 token |
| 前缀缓存命中率 | **86.7 %** |
| 平均生成长度 | 213 token |
| prompt > 100k 的请求占比 | **27.4 %** |

**三条结论**（都可从上面数据直接读出）：

1. **TTFT 是严重双峰分布**：52% 的请求 <1 s，但 10% 落在 6 s 以上，P99 达 **114 s**。
   **不是"普遍慢"，是两类完全不同的体验。**
2. **瓶颈不是排队**：`queue_time` 的 P99 只有 **39 ms**。
   ⇒ 114 s 的长尾来自**长 prompt 的预填本身**（27.4% 的请求 >100k token）。
   > 与 09-21 那次"5 × 126k 撑爆 500k 池 → FCFS 排队 → TTFT 248 s"的故障**机制不同**：
   > 池已从 500k 提到 **750k**（实测 `max_total_num_tokens=749824`），排队问题已修掉。
3. **ITL 自洽性交叉验证通过**：mean 29.2 ms ⇒ 单流 34.3 tok/s，
   与 §B 的散文 greedy **33.6** 吻合 ⇒ 说明采集脚本的解析口径正确。

> ⚠️ **推论（不是实测）**：既然不是排队，**单纯提高 `MAX_RUNNING_REQUESTS` 对尾延迟帮助有限**。
> 尾延迟的正确杠杆是**预填速度**与长 prompt 的前缀缓存命中率。
> 这一条**尚未做受控验证**——§8.4 记的"并发扫描未做"正指此处。

### E. 报数必带口径

以下三条都会显著影响数字：

1. **单流 decode 速度强依赖内容**：数数字这类高可预测内容可到 **87 tok/s**，散文只有 **34** ——
   差异来自投机解码接受率，不是系统变慢。**不写负载类型的吞吐数没有意义。**
2. 非流式 `usage.completion_tokens ÷ 整请求 wall`，**每请求唯一 prompt**（重复 prompt 会被 radix
   前缀复用，预填虚高 1.5–2×）；预填数**已扣解码时间**。
3. **运行间噪声约 ±3–4%**（用"代码路径逐字节等价"的对照臂实测得出）。
   小于 4% 的差异不是真实变化；单流离散最大（±9%），C4 最稳（±0.8%）。

> ⚠️ **两条已作废的历史数字，不要引用**：
> ① **"预填 ≈3.0k tok/s"** —— 用重复 filler 造 prompt 被 radix 前缀复用导致的虚高 1.5–2×，
>    唯一 prompt 重测只有 2.0–2.1k（[`DEPLOY-RECORD:354`](docs/DEPLOY-RECORD-dsv41.md#L354)）；
> ② **"chunk=1024 的收益是 indexer 拿回的"** —— 归因错误，收益来自 chunk 大小本身
>    （[`…RESULTS.md:316-336`](docs/INDEXER-CHUNKED-TP3-RESULTS.md#L316-L336)）。

---

## 1. 接线（第一决定因素）

```
        正确（交叉环）                        错误（会 110 / 静默死）
        W1: n1.p0 ↔ n3.p1                    W1: n1.p0 ↔ n3.p0
        W2: n1.p1 ↔ n2.p0                    W2: n1.p1 ↔ n2.p0
        W3: n2.p1 ↔ n3.p0                    W3: n2.p1 ↔ n3.p1
```

**规则**：必须是**每条缆 p0↔p1**。每台的 2 个物理 QSFP 口会在**两个 PCI 域各暴露一个 netdev**，
所以每台看起来有 4 个 RDMA 设备 / 4 个 fabric 地址 / 全网 6 个 `/24` —— **物理上只有 3 根线**，
域 2 那批是同一根缆的第二层视图，**一个 `/24` 只属于一根缆**。

若有两根缆接在**同一端口索引**上，NCCL 按设备索引（索引 0 三台都是 `p0`）配对时必然把不同缆的
两口配到一起 → `ibv_modify_qp 110 Connection timed out`，或**无任何 NCCL 告警的静默死**。
诊断过程与踩坑见 [`docs/ROCE-INVESTIGATION.md`](docs/ROCE-INVESTIGATION.md)（历史记录）。

现役地址表（实测）：

| 节点 | `p0·域0` | `p1·域0` | `p0·域2` | `p1·域2` |
|---|---|---|---|---|
| n1 head | 10.100.178.2 | 10.100.180.2 | 10.100.179.2 | 10.100.181.2 |
| n2 | 10.100.180.1 | 10.100.176.2 | 10.100.181.1 | 10.100.177.2 |
| n3 | 10.100.176.1 | 10.100.178.1 | 10.100.177.1 | 10.100.179.1 |

**每台必做**（netplan 示例见 `assets/`）：

1. 四个 fabric 口 **MTU 9000**，并为**每个对端**加 `/32` 直连路由；
2. 这两项**必须写进 netplan / NetworkManager** —— 临时 `ip route add` 会被 NM re-apply 冲掉，
   症状是到 fabric 的 TCP 停在 `SYN-SENT` 且**源地址是 WiFi**，引擎 init 永久挂起；
3. **`/etc/nvidia/cx7-hotplug-enabled` 必须移走**（三台都查）：留着的话某台重启会把 ConnectX 口
   从**邻居**的 PCI 总线上摘掉，fabric 局部静默消失；
4. **控制面走管理网**：`NCCL_SOCKET_IFNAME` / `GLOO_SOCKET_IFNAME` = WiFi 口 `wlP9s9`。
   设成 fabric 口会导致各 rank 公布互不可达的地址 → 永久 `SYN-SENT`、**NCCL 一行日志都不打**、
   三台空转（诊断：`sudo ss -tnp | grep 10.100`）。

```bash
sudo netplan generate && nmcli con reload && nmcli dev reapply <iface>   # 不要用 netplan apply

# 验证：① 四条 ConnectX 满速（期望 32.0 GT/s x4）
for f in $(lspci -D -d 15b3: | awk '{print $1}'); do
  echo $f $(cat /sys/bus/pci/devices/$f/current_link_speed) x$(cat /sys/bus/pci/devices/$f/current_link_width); done
# ② 四个 IB 口全 4: ACTIVE
for d in /sys/class/infiniband/*; do echo "$(basename $d) $(cat $d/ports/1/state)"; done
# ③ 严格判同缆用 L2 ARP 探针（ping -I 在有 /32 路由时会误判"通"）
python3 scripts/arp_probe.py
```

## 2. 另两条前提（都不在 NCCL 里，同样致命）

| # | 问题 | 判据 | 修法 |
|---|---|---|---|
| ① | **内核 CMA 回归**：`7.0.0-1019-nvidia` 上 `ibv_reg_mr_iova2` 必然 `Cannot allocate memory`（连 1 KB 都失败）⇒ 引擎建后续 communicator 时 `ncclSystemError` | `grep CmaTotal /proc/meminfo` = **`0 kB`**（正常 `131072 kB`） | 内核/驱动换到 **`6.17.0-1031-nvidia` + `580.173.02`**；`GRUB_DEFAULT="1>2"` + `update-grub` + `apt-mark hold` 防升回 |
| ② | **引擎镜像 `/etc/nccl.conf` 残留**：`NCCL_IB_USE_INLINE=1` + `NCCL_IB_PREPOST_RECEIVE_WORK_REQUESTS=1` ⇒ pynccl 的 **4 字节** warmup all_reduce 永久冻死（日志停在 `sglang is using nccl==2.30.7`，GPU 96% 但功耗仅 ~16 W 自旋） | `docker exec <容器> cat /etc/nccl.conf` | `start.sh` 补透传并置 0：`patch_startsh_envvar.py NCCL_IB_USE_INLINE 0`、`... NCCL_IB_PREPOST_RECEIVE_WORK_REQUESTS 0`（**进程 env 优先于 conf 文件**）|

> ② 的关键排查经验：所有"能通过"的独立 smoke/探针跑的**都是基础镜像**（不带该文件），
> 于是形成"单测能过、引擎卡死"的假矛盾——**复现必须用引擎镜像**跑探针。
> 另：引擎容器带 `--ulimit memlock=-1:-1`，**裸 `docker run` 的容器内默认只有 8192**，别被它误导。

## 3. 传输配置

```bash
# ── RoCE（现役）──
NCCL_NET=IB
NCCL_IB_DISABLE=0
NCCL_IB_HCA=rocep1s0f0,rocep1s0f1      # 每台两个逻辑口 = 域0 的两个物理口
NCCL_IB_GID_INDEX=3                     # RoCE v2 + fabric IPv4
NCCL_IB_SUBNET_AWARE_ROUTING=1          # 交叉环下靠它逐连接选对网卡（不要关）
NCCL_IB_MERGE_NICS=0
NCCL_NET_PLUGIN=none
NCCL_CROSS_NIC=0
NCCL_SOCKET_IFNAME=wlP9s9               # 控制面走管理网
GLOO_SOCKET_IFNAME=wlP9s9
NCCL_P2P_DISABLE=1
NCCL_SHM_DISABLE=1
NCCL_MAX_NCHANNELS=8
NCCL_BUFFSIZE=1048576
NCCL_PROTO='^LL128'
NCCL_HOST_DIR=/nonexistent-nccl-host-dir   # 禁挂自建 NCCL，用镜像自带

# ── socket 回退（只在 RoCE 出问题时用；吞吐约为 RoCE 的 55%）──
# NCCL_NET=Socket / NCCL_IB_DISABLE=1 / NCCL_SOCKET_IFNAME=enp1s0f0np0（数据面仍走 CX7 直连）
```

**瓶颈是延迟不是带宽**：每步约 104 次集合通信，socket 单次约 1 ms、RoCE 50–100 µs ——
所以 RoCE 的增益主要在**解码**（单流 +60~85%、4 并发 +75%），预填两档同量级（预填是算力受限）。

## 4. 快速上手

```bash
# 0) 前置：内核/驱动配对（§2 ①）+ engine 侧两个开关（§2 ②）+ 接线与链路（§1）

# 1) 链路：MTU 9000 + /32 直连路由（三台都执行）
sudo bash scripts/fabric-mtu-route.sh

# 2) 起服务（在 head 上）
cp env.example .env            # 按需修改 IP/HCA/内存水位
./svc.sh start                 # 预检 → share → serve → 等就绪 → 三层验证
#   本次实测就绪 497–617 s（约 8–10 分钟）；svc.sh 自带提示写 13–15 分钟，偏保守
#   其余子命令：./svc.sh status（只读体检）｜stop｜restart｜preflight｜logs -f
#   开机自启已启用：systemd 单元 dsv41.service → scripts/svc-boot.sh

# 3) 三层验证（缺一不可）
curl -s http://127.0.0.1:8888/health -w " %{http_code}\n"                      # 200
docker ps --format "{{.Names}} {{.Status}}"                                     # 三台 healthy
L=$(ls -t serve-*.log|head -1); grep -c "via NET/IB" $L; grep -c ibv_reg_mr_iova2 $L   # ≈64 / 0
python3 scripts/bench_migration.py http://192.168.0.86:8888 smoke . 3 300        # 从 worker 跑
```

## 5. 运维铁律

完整清单见 [`docs/PITFALLS.md`](docs/PITFALLS.md)。这几条**踩过且复现过**：

1. **`adapter/` 是 `COPY` 进镜像的，不是 bind-mount** —— 改 `adapter/` 下任何文件（含
   `sitecustomize.py`）**必须 `./start.sh build` 重建镜像**；**只重启服务是静默无效的**：
   容器里仍是旧代码，而 `start.sh` 传的 env 照常生效，表现为"配置生效了、代码没生效"、
   **没有任何报错**。详见 [`fleet/README.md`](fleet/README.md)。
2. **`svc.sh stop` 有意保留 `dsv41-nfs`，而 `start.sh` 的 share 不幂等** —— stop 后直接 start
   会在 `docker run --name dsv41-nfs` 处报 **container name 冲突**。两者之间先
   `docker rm -f dsv41-nfs`（若因导出被占用而失败，重试几次）。
3. **一轮失败后三台重启再开下一轮**——残留容器与挂起上下文会让下一轮在更早的地方假失败。
4. 重启机器后**先 `./start.sh share` 再 `serve`**（否则 worker 的 NFS 检查静默卡十几分钟）。
   注意开机自启的 `dsv41.service` 会**自动**做一次 share，此时再手工 start 就会撞上第 2 条。
5. NFSv4 `fsid=0` 导出时，客户端必须挂**伪根 `:/`**，不是子目录。
6. **绝不要 `docker ps -q | xargs docker rm -f`**——引擎容器也在列表里。
7. `MEM_FRACTION_STATIC` 用 0.95（低于 0.944 会报 "weights leave no GPU memory for the KV cache"）。
8. 新增 `.env` 变量**必须补透传**（`patch_startsh_envvar.py`）；`bash -n` 查不出拼接错误，
   要实测 worker 行的 `-e` token 数并真实展开参数。

## 6. 仓库结构

```
docs/DEPLOY-RECORD-dsv41.md          现场主记录（按时间追加的历轮实验日志）
docs/DEPLOY-GUIDE.md                 完整部署手册         docs/PITFALLS.md   避坑清单（按现象索引）
docs/INDEXER-CHUNKED-TP3-RESULTS.md  ← 2026-09-26 indexer 移植实测（四臂 A/B、质量门、**负面结论**）
docs/INDEXER-CHUNKED-TP3-PLAN.md     该移植的事前方案
docs/BATCH-MIGRATION-2026-09-25.md   2026-09-25 解码栈迁移实测   ADAPTER-MIGRATION-PLAN.md 其事前方
docs/ROCE-INVESTIGATION.md           历史排查记录（已作废）      docs/UPSTREAM-ISSUE.md  上游 issue + 更正

scripts/                  可直接复用的脚本
  svc.sh                  服务启停与体检：preflight / start / stop / restart / status / logs
  svc-boot.sh             开机自启包装            dsv41.service    systemd 单元
  bench_migration.py      主基准：C1 散文(sampled+greedy) + C1 代码 + C4，每请求唯一 prompt
  bench_tail.py           尾延迟分位数（P50/P90/P99）—— 从 /metrics 直方图桶读，
                          `--reset`/`--diff` 取窗口增量，见 §D 与 §8.1
  bench_prefill.py        预填基准（唯一 prompt + 扣解码修正）
  bench_longctx.py        长上下文稳健性（重复 + 并发）
  patch_startsh_envvar.py 给 start.sh 补 env 透传（head + worker 两处，幂等）
  patch_autotune_volatile.py  重放 autotune_keep 的 _VOLATILE 改动（幂等、带回滚）
  probe_indexer_variant.sh    只读探测引擎的 indexer 属于哪个变体
  run_probe.sh + pynccl_probe3.py   多 communicator 复现探针（**必须用引擎镜像跑**）
  kernel_switch2.sh       内核/驱动配对切换（带回滚）      gpu_burn.py  GPU 烧机自检
  ping_matrix.sh / arp_probe.py / measure-fabric.sh   接线与 fabric 验证
  fabric-mtu-route.sh     MTU 9000 + /32 直连路由        verify_extras.py  视觉 + tool_calls 验证
  __legacy__/              已作废排查路线的证据，保留备查（**不要照做**）

fleet/                    线上引擎侧**文件本体**快照（17 个 adapter + 4 个部署文件，见 fleet/README.md）
  Dockerfile              基础镜像之上的 overlay —— **adapter 就是在这里 COPY 进镜像的**
  start.sh / boot.py / files/nfs-share.sh   启动器 / 容器入口 / NFS 导出

  # start.sh 的 env 透传计数有两个不同口径，别混：
  #   · 文件里**当前共有** 30 个 DSV41_* 透传（head 段与 worker 段各 30，实测一致）
  #   · 其中**相对上游新增**的是 14 个（12 个解码栈 + 2 个 indexer）—— 这个数在 NOTICE 里
  #   · 另有 9 个非 DSV41_ 前缀的增补（NCCL_IB_USE_INLINE / NCCL_IB_PREPOST_RECEIVE_WORK_REQUESTS /
  #     DSPARK_ALIGN_VERIFY_TO_TIER / SGLANG_RAGGED_VERIFY_MODE / SGLANG_SIMULATE_ACC_LEN /
  #     SGLANG_TEST_RAGGED_VERIFY_FORCE_UNIFORM_CAPTURE / SGLANG_DSPARK_ENABLE_SPS_RECORD /
  #     NCCL_NET_GDR_LEVEL / NCCL_DMABUF_ENABLE）
assets/                   netplan 示例 + 已作废的 NCCL 拓扑文件
env.example               环境变量样例（已脱敏，RoCE 档 + socket 回退注释）
```

## 7. 硬件/软件基线

| 项 | 值 |
|---|---|
| 节点 | 3 × DGX Spark（GB10），每机 1 颗 GPU，121.7 GiB 统一内存 |
| 内核 / 驱动 | **`6.17.0-1031-nvidia` / `580.173.02`**（配对标；`7.0.0-1019` 有 CMA 回归，见 §2）|
| NCCL | 镜像自带 2.30.7（**不需要任何补丁**）|
| 引擎 | 厂商 sglang 镜像，内部 build `da64c5cbb`（`lmsysorg/sglang:dev-dsv41`，实测 `/get_server_info` 的 `version` 字段）|
| 容器镜像 | `dsv41-3x-spark:local`（由 `fleet/` 的 Dockerfile overlay 构建）|

> **镜像名的正确拼法是 `dsv41-3x-spark:local`**（`fleet/start.sh:89` 的 `${IMAGE:-...}` 默认值），
> 工作目录同理是 `~/dsv41-3x-spark`（`start.sh:92` 的 `WORKER_DIR`）。
> 早先文档里写成 `dsv41-3xspark:local`（无中横线）是**笔误**——与实际镜像名不符，照抄会 `image not found`。

> DGX 上**驱动与内核是配对的**（`580.173.02 ↔ 6.17.0-1031`、`580.178.04 ↔ 7.0.0-1019`）。
> 换内核必须同时换驱动；降级驱动会卸掉当前内核的驱动模块，所以"降驱动 + 引导旧内核"必须成对做。

## 8. 已知限制与待澄清项

### 8.1 本轮新增的实测（2026-09-27）

- **尾延迟已测**（§D）：用 `scripts/bench_tail.py` 从 `/metrics` 直方图读到 **2403 个真实请求**的
  P50/P90/P99。**README 早先"尾延迟未测"的限制已解除**，但只在**当前这一个负载窗口**内成立。
  证据存档：`docs/tail-latency-live-20260927-1026.txt`、`docs/live-config-20260927.txt`。
- **`/get_server_info` 是现役参数的权威读数**：它返回完整启动参数（176 个非默认字段）。
  核对现役配置**不要**去猜 `.env`，直接 `curl -s http://<head>:8888/get_server_info`。
- **`get_server_info` 查不到 NCCL 变量**（它们是进程 env，非启动参数）——
  核实 `NCCL_PROTO` / `NCCL_MAX_NCHANNELS` 仍需 SSH 进容器 `docker exec <ctn> env | grep NCCL`。

### 8.2 ⚠️ 待澄清：`DSPARK_BLOCK_SIZE` 线上是 5，但文档说"3 是调优值"

实测 `speculative_dspark_block_size = 5`（§现役参数表），而仓库内有**互相矛盾**的记载：

| 来源 | 说法 |
|---|---|
| 实测结论 | "**采样生成（temp>0）下 block 5 是负优化**（−10%）；仅在 greedy 为正。**已复位 3**"（[`DEPLOY-RECORD:420`](docs/DEPLOY-RECORD-dsv41.md#L420)）|
| 避坑清单 | `DSPARK_BLOCK_SIZE` = **3**；k=5 在聊天/散文上**过度起草**（[`PITFALLS.md:229`](docs/PITFALLS.md#L229)、[`:26`](docs/PITFALLS.md#L26)）|
| 部署手册 | "k=3 是调优值……**勿随意改**"（[`DEPLOY-GUIDE.md:194`](docs/DEPLOY-GUIDE.md#L194)）|
| 迁移记录 | 批次 2 改成 5，且现行配置写 **5**（[`BATCH-MIGRATION:15`](docs/BATCH-MIGRATION-2026-09-25.md#L15)、[`:136`](docs/BATCH-MIGRATION-2026-09-25.md#L136)）|

**推断（非事实）**：09-25 提 k 到 5 是因为它在**代码负载**上收益明显（C1 代码 58.71→74.60，+27%），
而 09-19 的"复位 3"结论出自**散文/聊天**负载。**两个结论可能都对，只是负载不同。**

⇒ **要回答的是"当前生产负载下 k 该用几"**，而不是"哪个文档对"。
事前方案（含单变量设计、重 tune 陷阱、回滚）见
[`docs/K3-VS-K5-AB-PLAN.md`](docs/K3-VS-K5-AB-PLAN.md)。**尚未执行。**

> 旁证：§D 的 ITL 分布 P50=14 ms（≈71 tok/s）落在"代码/高可预测"档，
> 但**这不能区分 greedy 与采样**，故仍需业务侧确认线上温度配比。

### 8.3 `indexer_chunked` backport：收益落空（保留仅为余量保险）

2026-09-26，详见 [`docs/INDEXER-CHUNKED-TP3-RESULTS.md`](docs/INDEXER-CHUNKED-TP3-RESULTS.md)：
对性能**无可测影响**，**也不是** `CHUNKED_PREFILL_SIZE=1024` 在长上下文可用的前提
（`OFF+1024` 到 200k 也全过）。唯一被证实的是**质量零退化**（75 题配对 BROKE=0）。
**预填的真正收益来自 chunk 大小本身**（768→1024 约 +11~15%），与该 backport 无关。
留它的唯一理由是 >200k 的余量保险——**未验证**（ON 测到 255k，OFF 只到 200k）。

### 8.4 其它未解项

- **SPS（投机解码吞吐表）在当前构建下无法生效**（`compact` ragged-verify 启动即崩、三处 shape
  不一致），现用 `SGLANG_RAGGED_VERIFY_MODE=static`，表 inert；对并发 ≥2 本可有收益。
  ⚠️ 注意 `speculative_dspark_sps_table_path` 实测**已设置**为 `/state/dspark_sps.json`
  （§现役参数表）——即**路径存在但表不被消费**，别误读成"表没配"。
- **单流解码延迟抖动（历史上 0.9 s ↔ 18 s）只部分处理**：`DSV41_AUTOTUNE_KEEP=1` 后 autotune
  缓存跨重启 `reused`、同一 greedy prompt 连跑 3 次 sha256 一致，**但那种秒级抖动本身没有做
  前后对照测量**，不能声称已消除。已排除 GPU 降频（2.1–2.3 GHz、节流位 0x0）与带宽瓶颈。
- **并发只测到 C4**，更高并发与"高并发 × 长上下文"未测。
  （§D 的生产数据覆盖到真实并发，但**未做受控的并发扫描**。）
- **255k 量级只做过顺序请求**；`OFF+1024` 那组只测到 200k，且未做并发。

### 8.5 文档一致性问题（已修/待修）

| 项 | 状态 |
|---|---|
| 镜像名写成 `dsv41-3xspark:local`（实为 `dsv41-3x-spark:local`） | ✅ 本版已修（§7）|
| `start.sh` 透传计数"14"未区分"新增"与"当前总数"（实为 30） | ✅ 本版已说明（§6）|
| `env.example` **未收录** `MAX_RUNNING_REQUESTS=8` / `MAX_TOTAL_TOKENS=750000` / `MIN_FREE_SLOTS_DELAY=1` 等现役键 | ⚠️ **待修**——照 `env.example` 复现不出线上行为 |
| `env.example:47-51` 仍写着"RoCE 三角拓扑实测不可用"的**已作废结论** | ⚠️ **待修**（README 正文已是 RoCE 档）|
| `svc.sh:4` 注释里的路径 `~/dsv41-3xspark` 与 `start.sh:92` 的 `dsv41-3x-spark` 不一致 | ⚠️ 待核（脚本用 `$ROOT` 自解析，**不影响运行**，仅注释误导）|

- 已验证（2026-09-19）：视觉分支（自造测试图颜色/形状/位置全对）、工具调用（标准 `tool_calls`）、
  三台 `swapoff -a`（`/etc/fstab` 已注释，重启不复活）。

## 9. 许可与致谢

- 本仓库文档与脚本：MIT（见 `LICENSE`）。
- **例外**：`fleet/` 下派生自上游配方的文件 —— 4 个部署文件（`start.sh`、`boot.py`、`Dockerfile`、
  `files/nfs-share.sh`）与 **`fleet/adapter/` 下全部 17 个源文件** —— 派生自
  [上游配方](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-DGX-Sparks)（AGPL-3.0-or-later），
  在该目录内按 **AGPL-3.0-or-later** 分发，全文见 `LICENSE.AGPL`。
  其中 8 个解码 adapter 源自 `knapcio/DeepSeek-V4.1-Flash-4x-DGX-Spark-TP4`；`LICENSE.upstream-MIT`
  保留的 0xSero MIT 声明必须随本仓库一并保留。第三方署名与**本地改动声明**见 `NOTICE`。
- 不包含任何厂商源码、模型权重或镜像内容；引用的第三方补丁请遵循其各自仓库的许可。
- 感谢 `luxingcom/aicad-nccl-optimization` 与 LuZ 生产栈作者公开 ring-only 补丁与构建记录：
  它们在我们接线错误、内核有 CMA 回归的阶段提供了关键对照，也促成了最终定位。
