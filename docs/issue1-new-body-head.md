> # ⚠️ 结案更正（2026-09-19）：根因不在 NCCL —— 是我们自己环境的三处问题，已全部解决，本 issue 关闭
>
> **结论**：同一套 3 节点 CX7 三角拓扑上 **NCCL over RoCE 一次通过**，官方 NCCL 2.30.7 原样即可跑通，
> **不需要**任何 ring-only / netdev 补丁栈（本 issue 里用的补丁确实能消掉 `ibv_modify_qp 110`，
> 但那是在我们**接线错误**的前提下才有意义的实验）。

## 三处真凶

1. **接线**（100% 决定性）：原接线中有两根缆接在**同一端口索引**上（一根 `p0↔p0`、一根 `p1↔p1`），
   而 NCCL 按**设备索引**跨 rank 配对（`ncclTopoSearchCheckNet`，索引 0 = PCI 域最小的卡 = 三台都是 `p0`），
   于是必然把"不在同一根缆上"的两个口配到一起 → `ibv_modify_qp 110` 或静默死，任何环境变量/补丁都救不了。
   **改成每条缆 `p0↔p1`（交叉环）** + `NCCL_IB_SUBNET_AWARE_ROUTING=1` + `NCCL_IB_HCA=<域0 的两个口>`
   + 控制面 `NCCL_SOCKET_IFNAME=wlP9s9` 后一次通过（`ibv_modify_qp` 不再出现，32/32 信道 `via NET/IB`）。
2. **内核 CMA 回归**：DGX OS `7.0.0-1019-nvidia` 上 `grep CmaTotal /proc/meminfo` = `0 kB`（正常 `131072 kB`），
   表现为 `ibv_reg_mr_iova2 failed with error Cannot allocate memory`——连 **1 KB** 区域都注册失败，
   而节点有 9 GB 空闲（不是内存、不是 memlock：引擎容器带 `--ulimit memlock=-1:-1`）。
   换 **`6.17.0-1031-nvidia` + 驱动 `580.173.02`** 后消失。
3. **容器镜像残留 `/etc/nccl.conf`**：其中 `NCCL_IB_USE_INLINE=1` + `NCCL_IB_PREPOST_RECEIVE_WORK_REQUESTS=1`
   会让 SGLang 的 **PyNccl 4 字节 warmup all_reduce 永久冻死**（日志停在 `sglang is using nccl==2.30.7`，
   `py-spy` 栈 `... synchronize ← pynccl.py:131 ← parallel_state.py:457`，GPU 96% 但功耗仅 ~16 W = 自旋）。
   这两个变量置 0 即解（进程 env 优先于 conf 文件）。长期误诊的原因：所有独立冒烟/探针跑的
   **都是不带该文件的基础镜像** → "单测能过、引擎卡死"。

## 实测（同一整套硬件与配方）

- fabric `all_reduce` 256 MB：**13.86 GB/s**（1 MB 8.1 / 16 MB 13.2），32/32 信道 `via NET/IB`；
- SGLang TP=3 服务：单流 **28.5–35.4 tok/s**、4 并发聚合 **67.5–75.8 tok/s**、预填 **2.0–2.1k tok/s**
  （≈4k 档扣解码，**每题唯一随机 prompt**；早期记录的"≈3.0k"是重复 filler 被 radix 前缀复用导致的虚高 1.5–2×，
  **已作废**，见 `docs/DEPLOY-RECORD-dsv41.md` §14）；
- 对照（同拓扑 socket 兜底）：2.07–2.14 GB/s、单流 17.9–19.2、4 并发 39.7–43.3 tok/s。

## 给后来者的三条判据（最容易踩）

- `ibv_modify_qp failed with 110 ... local GID <A> → remote GID <B>`：那两个 GID 就是被配到一起的两个口，
  拿地址表一查即知是哪两根缆 —— **先怀疑接线，不要先去调 NCCL 参数**；
- `grep CmaTotal /proc/meminfo` = `0 kB` ⇒ 直接换内核，别在注册路径上浪费时间；
- `docker exec <容器> cat /etc/nccl.conf` 与基础镜像对比 ⇒ 独立 smoke 能过而引擎卡死时，第一时间查这里。

感谢贵仓库公开的 ring-only 补丁与构建记录：它们在**错误接线**阶段提供了关键对照（让我们确认"QP 能建起来
但数据仍崩"不是唯一可能），也促成了最终把根因钉在接线与内核上。为先前把根因指向 NCCL 本身致歉。
