"""合併 GitHub 流量資料（clones + views）並畫趨勢圖。

用法：python3 traffic.py <clones_new.json> <views_new.json>
輸出：traffic/clones.json、traffic/views.json、traffic/chart.png
"""
import json
import os
import sys
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

OUT_DIR = "traffic"


def merge(new_file, key, out_name):
    """用日期當 key 合併，重複抓同一天會覆蓋成最新值，不會重複計算。"""
    path = os.path.join(OUT_DIR, out_name)
    old = {}
    if os.path.exists(path):
        with open(path) as f:
            old = json.load(f)
    with open(new_file) as f:
        new = json.load(f)
    for d in new.get(key, []):
        old[d["timestamp"][:10]] = {"count": d["count"], "uniques": d["uniques"]}
    merged = dict(sorted(old.items()))
    with open(path, "w") as f:
        json.dump(merged, f, indent=2)
    return merged


def plot(ax, data, title):
    if not data:
        ax.set_title(f"{title} (no data yet)")
        return
    days = [date.fromisoformat(k) for k in data]
    counts = [v["count"] for v in data.values()]
    uniques = [v["uniques"] for v in data.values()]
    ax.plot(days, counts, marker="o", markersize=3, label="Total")
    ax.plot(days, uniques, marker="o", markersize=3, label="Unique")
    ax.set_title(f"{title}  (all-time total: {sum(counts)})")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))


def main():
    clones_new, views_new = sys.argv[1], sys.argv[2]
    os.makedirs(OUT_DIR, exist_ok=True)
    clones = merge(clones_new, "clones", "clones.json")
    views = merge(views_new, "views", "views.json")

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    plot(axes[0], clones, "Git clones")
    plot(axes[1], views, "Page views")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "chart.png"), dpi=120)
    print(f"clones: {len(clones)} days, views: {len(views)} days")


if __name__ == "__main__":
    main()
