#!/usr/bin/env python3
"""尾延迟分位数：从 /metrics 的直方图桶取 P50/P90/P99。

与 bench_migration.py 的口径区别：那个测吞吐（tok/s），本脚本测**延迟分布**。
数据源是引擎自己的 Prometheus 直方图，因此可以读到**部署以来累积的**真实
生产流量，而不只是本次压测样本。

为什么需要它：README §8 记「尾延迟未测（只有中位数，没有 P50/P99）」——
而 `--enable-metrics` 已经在跑，桶数据一直都在，只是没人读。

用法：
    python3 bench_tail.py <base_url>              # 读一次，打印累积 + 派生比率
    python3 bench_tail.py <base_url> --watch 5    # 每 5 秒采样
    python3 bench_tail.py <base_url> --reset      # 记基线（A/B 前调用）
    python3 bench_tail.py <base_url> --diff       # 只报窗口增量（A/B 后调用）

A/B 用法（本脚本的主用途）：
    ./svc.sh restart && sleep 120            # 等 warm-up 完
    python3 bench_tail.py $URL --reset       # ① 记基线
    python3 bench_migration.py $URL smoke . 3 300   # ② 跑负载
    python3 bench_tail.py $URL --diff        # ③ 只看这一窗的分布
    # 改配置 -> 重启 -> 重复 ①②③；两窗的 --diff 输出可直接对比

口径注意：
  · 直方图是**累积**的（自引擎启动）。--diff 靠逐桶相减得到窗口值。
  · 引擎重启会让累积计数归零 —— 此时 --diff 主动报"不可比"而不是给错数。
  · 桶内按**线性插值**估分位数；桶宽很粗（如 8→10s），故结果只在本桶精度内可信。
  · 上限桶 le="+Inf" 不参与插值——若样本落在最后一个有限桶之外，
    只能报"溢出"，不能编一个数出来。
  · --diff 的窗口里若混了**别的流量**（真人在用），数字同样不干净：
    这是"累积计数器"方法的固有局限，A/B 时应在同一时段、同样负载下比。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

FAMILIES = [
    "sglang:time_to_first_token_seconds",
    "sglang:e2e_request_latency_seconds",
    "sglang:inter_token_latency_seconds",
    "sglang:queue_time_seconds",
    "sglang:per_stage_req_latency_seconds",
]


def fetch(base, path="/metrics", timeout=20):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _ascii_safe():
    """Windows 控制台默认 GBK，中文/箭头会 UnicodeEncodeError 打断采集
    （2026-09-27 实测：'⇒' 直接让脚本崩在打印上）。输出统一压成 ASCII，
    报告文字保留中文但只在不支持时才降级 —— 采集本身绝不因编码失败。"""
    enc = (sys.stdout.encoding or "utf-8").lower()
    return "utf" not in enc


def parse_histograms(text):
    """-> {family: {series_labels: {"buckets": [(le, cum)], "sum": s, "count": c}}}

    series_labels 是**去掉 le 之后**的标签串——这是关键：le 是桶边界，
    不是序列身份。按完整标签做 key 会给每个桶建一条独立记录（踩过）。
    """
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        brace = line.find("{")
        if brace < 0:
            continue
        name, rest = line[:brace], line[brace + 1:]
        end = rest.rfind("}")
        if end < 0:
            continue
        labels, value = rest[:end], rest[end + 1:].strip()
        try:
            value = float(value)
        except ValueError:
            continue
        for suffix, kind in (("_bucket", "bucket"), ("_sum", "sum"), ("_count", "count")):
            if not name.endswith(suffix):
                continue
            family = name[: -len(suffix)]
            break
        else:
            continue

        le = None
        kept = []
        for part in labels.split(","):
            key = part.split("=", 1)[0].strip()
            if key == "le":
                le = part.split("=", 1)[1].strip().strip('"')
            else:
                kept.append(part.strip())
        series = ",".join(kept)

        fam = out.setdefault(family, {}).setdefault(
            series, {"buckets": [], "sum": None, "count": None}
        )
        if kind == "bucket":
            try:
                bound = float(le) if le != "+Inf" else float("inf")
            except (TypeError, ValueError):
                continue
            fam["buckets"].append((bound, value))
        elif kind == "sum":
            fam["sum"] = value
        else:
            fam["count"] = value
    return out


def quantile(series, q):
    """线性插值估分位数。样本落在 +Inf 桶时返回 None（不编造）。"""
    buckets = sorted(series["buckets"])
    count = series.get("count")
    if not count:
        return None
    target = count * q
    prev_bound, prev_cum = 0.0, 0.0
    for bound, cum in buckets:
        if bound == float("inf"):
            if cum >= target and prev_cum < target:
                return None  # 落在 +Inf 桶：只能说"超出最后一个有限边界"
            break
        if cum >= target:
            if cum == prev_cum:
                return bound
            frac = (target - prev_cum) / (cum - prev_cum)
            return prev_bound + (bound - prev_bound) * frac
        prev_bound, prev_cum = bound, cum
    return None


def subtract(after, before):
    """累积直方图做差 -> 只含本次窗口样本的直方图。

    桶边界在两次采样间通常一致，但**不能假设**：引擎重启/配置变更会重建桶
    （本仓库踩过多轮重启）。因此只在边界集合完全相同时做逐桶相减；
    否则返回 None 让调用方报"不可比"，而不是编一个混了两次桶边界的数。

    另一个陷阱：`count` 是累积请求数，重启后会**归零**。after.count < before.count
    说明中途重启过，此时差值无意义 —— 同样拒绝。
    """
    out = {}
    for family, series_map in after.items():
        if family not in before:
            continue
        for series, ac in series_map.items():
            bc = before[family].get(series)
            if bc is None:
                continue
            if (ac.get("count") or 0) < (bc.get("count") or 0):
                return None  # 重启过：累积计数回退
            ab = {b: v for b, v in ac["buckets"]}
            bb = {b: v for b, v in bc["buckets"]}
            if set(ab) != set(bb):
                return None  # 桶边界变了：不可逐桶相减
            buckets = [(b, ab[b] - bb[b]) for b in ab]
            count = (ac.get("count") or 0) - (bc.get("count") or 0)
            asum, bsum = ac.get("sum"), bc.get("sum")
            total = None if asum is None or bsum is None else asum - bsum
            out.setdefault(family, {})[series] = {
                "buckets": buckets, "sum": total, "count": count
            }
    return out


def ratio_report(hist, n_scale=None):
    """由直方图算派生比率。这些比值是诊断瓶颈的关键，比单看分位数有用得多。

    —— 例：queue_time 的分位数再低也不能说明"不排队"，
    要跟 TTFT 的尾巴放在一起看才成立（2026-09-27 实测：queue P99=39ms
    而 TTFT P99=114s ⇒ 瓶颈是预填，不是排队）。
    """
    def grab(family, key):
        d = hist.get(family, {})
        for series, v in d.items():
            if key in series:
                return v
        return None

    prompt = grab("sglang:prompt_tokens_histogram", "")
    uncached = grab("sglang:uncached_prompt_tokens_histogram", "")
    gen = grab("sglang:generation_tokens_histogram", "")
    itl = grab("sglang:inter_token_latency_seconds", "")
    if not (prompt and prompt.get("count")):
        return
    n = prompt["count"]
    mean_p = (prompt.get("sum") or 0) / n
    mean_g = ((gen or {}).get("sum") or 0) / n
    mean_u = ((uncached or {}).get("sum") or 0) / n
    hit = 100 * (1 - mean_u / mean_p) if mean_p else float("nan")
    print(f"\n--- 派生比率（n={int(n)} 请求）---")
    print(f"  平均 prompt        : {mean_p:12,.0f} token")
    print(f"  平均 uncached       : {mean_u:12,.0f} token")
    print(f"  前缀缓存命中率      : {hit:12.1f} %")
    print(f"  平均生成长度        : {mean_g:12.1f} token")
    if itl and itl.get("count"):
        mean_itl = (itl.get("sum") or 0) / itl["count"]
        if mean_itl > 0:
            print(f"  平均 ITL            : {mean_itl * 1000:12.1f} ms"
                  f"  -> 单流 decode 约 {1 / mean_itl:.1f} tok/s")
    # 长 prompt 占比：TTFT 长尾的直接来源
    for label, thr in ((">50k", 50000.0), (">100k", 100000.0)):
        for bound, cum in sorted(prompt["buckets"]):
            if bound >= thr:
                print(f"  prompt {label:<6} 占比   : {100 * (n - cum) / n:12.1f} %")
                break


def report(hist, prefix=""):
    for family in FAMILIES:
        if family not in hist:
            continue
        for series, data in sorted(hist[family].items()):
            count = data.get("count") or 0
            if not count:
                continue
            mean = (data["sum"] / count) if data.get("sum") is not None else float("nan")
            parts = []
            for q, label in ((0.5, "P50"), (0.9, "P90"), (0.99, "P99")):
                v = quantile(data, q)
                parts.append(f"{label}={'溢出' if v is None else format(v, '.3f')}")
            short = family.replace("sglang:", "")
            label = series.replace('engine_type="unified",', "").replace(
                ',model_name="deepseek-v4.1-flash"', "").replace(
                'model_name="deepseek-v4.1-flash",', "")
            print(f"{prefix}{short:<34} n={int(count):<9} mean={mean:9.4f}s  " + "  ".join(parts))
            if label:
                print(f"{prefix}  └─ {label}")


def snapshot_path(base):
    """基线文件按 base_url 区分，避免多实例/多档位互相污染。"""
    import hashlib
    tag = hashlib.sha256(base.encode()).hexdigest()[:10]
    return Path(f".bench_tail-{tag}.json")


def main():
    # 输出编码兜底：即使 console 是 GBK 也不让采集崩掉（见 _ascii_safe）。
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    base = args[0] if args else "http://127.0.0.1:8888"
    snap = snapshot_path(base)

    if "--reset" in flags:
        hist = parse_histograms(fetch(base))
        snap.write_text(json.dumps(hist))
        total = sum(int(d.get("count") or 0)
                    for fam in hist.values() for d in fam.values())
        print(f"基线已存 {snap}（{len(hist)} 族 / 累计 {total} 样本）")
        print("现在去跑你的负载，完成后用 --diff 看这一段窗口的分布。")
        return

    if "--diff" in flags:
        if not snap.is_file():
            raise SystemExit(f"没有基线 {snap}；先跑一次 --reset")
        before = json.loads(snap.read_text())
        after = parse_histograms(fetch(base))
        delta = subtract(after, before)
        if delta is None:
            raise SystemExit(
                "不可比：中途引擎重启过（计数回退）或桶边界变了。\n"
                "重跑 --reset 再采集这一窗。")
        n = sum(int(d.get("count") or 0)
                for fam in delta.values() for d in fam.values())
        print(f"=== 本窗口增量（相对 {snap}）窗口内共 {n} 样本 ===")
        report(delta)
        ratio_report(delta)
        return

    watch = None
    if "--watch" in sys.argv:
        watch = float(sys.argv[sys.argv.index("--watch") + 1])
    if watch:
        while True:
            print(f"\n=== {time.strftime('%H:%M:%S')} 累积量 ===")
            hist = parse_histograms(fetch(base))
            report(hist)
            ratio_report(hist)
            time.sleep(watch)
    else:
        hist = parse_histograms(fetch(base))
        report(hist)
        ratio_report(hist)


if __name__ == "__main__":
    main()