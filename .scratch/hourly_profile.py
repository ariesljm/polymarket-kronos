"""按小时统计盘口 ask 分布与成交表现 —— 找「可交易时段」vs「空转时段」。

数据源:
- logs/multi.log*(含按天滚动) 的 rej 行 → 每小时的 ask 中位 / 拦截量 / ≤0.64 占比(本地时间)
- data_multi/*/trades.csv → 每小时的成交笔数与 PnL(ts 为 UTC,+8 转本地)

用法: uv run python .scratch/hourly_profile.py
"""
import collections
import csv
import glob
import re
import statistics

REJ = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*rej sym=(\w+) win=(\d+) dir=(\w+) ask=([\d.]+)")

CAP = 0.64  # 当前封顶价


def main() -> None:
    # rej → (本地小时, ask)
    rej_hour: dict[str, list[float]] = collections.defaultdict(list)
    n_rej = 0
    for f in sorted(glob.glob("logs/multi.log*")):
        for ln in open(f, encoding="utf-8", errors="ignore"):
            m = REJ.match(ln)
            if not m:
                continue
            h = m.group(1)[11:13]
            rej_hour[h].append(float(m.group(5)))
            n_rej += 1

    # trades → (本地小时, pnl)
    tr_hour: dict[str, list[float]] = collections.defaultdict(list)
    n_tr = 0
    for f in glob.glob("data_multi/*/trades.csv"):
        for r in csv.DictReader(open(f, encoding="utf-8")):
            h = (int(r["ts"][11:13]) + 8) % 24
            tr_hour[f"{h:02d}"].append(float(r["pnl"]))
            n_tr += 1

    print(f"rej 总记录 {n_rej} 条, 成交 {n_tr} 笔\n")
    hours = sorted(set(list(rej_hour) + list(tr_hour)))
    print(f"{'时(本地)':>9} {'rej数':>6} {'ask中位':>8} {'≤0.64占比':>9} "
          f"{'成交':>5} {'PnL':>8} {'每笔':>7}")
    total_rej = sum(len(v) for v in rej_hour.values())
    total_tr = sum(len(v) for v in tr_hour.values())
    total_pnl = sum(sum(v) for v in tr_hour.values())
    for h in hours:
        asks = rej_hour.get(h, [])
        trs = tr_hour.get(h, [])
        n_a = len(asks)
        n_t = len(trs)
        if n_a == 0 and n_t == 0:
            continue
        med = statistics.median(asks) if asks else float("nan")
        lt = sum(1 for a in asks if a <= CAP) / n_a if asks else float("nan")
        pnl = sum(trs)
        per = pnl / n_t if trs else 0.0
        print(f"{h + ':00':>9} {n_a:>6} {med:>8.3f} {lt:>8.0%} "
              f"{n_t:>5} {pnl:>+8.2f} {per:>+7.2f}")
    print("-" * 64)
    print(f"{'合计':>9} {total_rej:>6} {'':>8} {'':>9} "
          f"{total_tr:>5} {total_pnl:>+8.2f} "
          f"{total_pnl / total_tr if total_tr else 0:>+7.2f}")


if __name__ == "__main__":
    main()