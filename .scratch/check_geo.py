"""一键验证代理出口地区与 Polymarket 地理限制（切换节点后运行）。

用法（在 v2rayN 里选好节点后）:
  uv run python .scratch/check_geo.py

输出：
  1. geoblock 连续 3 次（权威判据）—— blocked=False 即通过
  2. 出口 IP 归属（用 geoblock 返回的 ip，不依赖 ipify；ipify 可用时顺带显示）

Polymarket 受限（39 国）: AU BE BY BR BI CF CD CU DE ET FR GB IE IR IQ IT JP
  KP LB LY MM MT NI NL NZ PL RU SG SK SO SS SD SY TW TH UM US VE YE ZW
  外加加拿大 AB/BC/ON/QC、乌克兰 Crimea/Donetsk/Luhansk
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmbot.run import resolve_proxy  # noqa: E402

resolve_proxy()
import requests  # noqa: E402

print(f"代理 = {os.environ['HTTPS_PROXY']}\n")

from pmbot.preflight import geoblock_status  # noqa: E402

# 1. geoblock（权威判据）连查 3 次
results = []
for i in range(3):
    blocked, detail = geoblock_status()
    print(f"geoblock [{i+1}] blocked={blocked} | {detail}")
    results.append((blocked, detail))
    if blocked is False:
        break

# 2. 用 geoblock 返回的 ip 查归属（补充 isp 信息）
last = results[-1]
if last[1]:
    ip = last[1].split("ip=")[-1].strip() if "ip=" in last[1] else None
    if ip:
        try:
            px = {"http": os.environ["HTTPS_PROXY"], "https": os.environ["HTTPS_PROXY"]}
            info = requests.get(
                f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp",
                proxies=px, timeout=15,
            ).json()
            if info.get("status") == "success":
                print(f"\n出口归属 = {info.get('country')} | {info.get('regionName')} | "
                      f"{info.get('city')} | {info.get('isp')}")
        except Exception:
            pass

print()
if any(b is False for b, _ in results):
    print("✅ 出口未受限，可以实盘")
else:
    print("❌ 出口受限（或查询失败）。换一个落地在非受限地区的节点后重跑。")
    print("   受限列表见文件头注释；常见可用：KR 韩国 / HK 香港 / IN 印度 /")
    print("   VN 越南 / TR 土耳其 / ES 西班牙 / PT 葡萄牙 / AE 阿联酋 / BG 保加利亚")
