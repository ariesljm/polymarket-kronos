"""隔夜 dry-run 运行器：循环拉起 run_multi --dry-run，崩溃/异常自动重启。

用法（detached 启动）:
  uv run python .scratch/overnight_runner.py
状态: logs/overnight.log（拉起/退出记录）；主循环日志 logs/multi.log（按天滚动）。
"""
import os
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "overnight.log")


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> None:
    os.chdir(ROOT)
    log("overnight_runner 启动")
    while True:
        t0 = time.time()
        log("拉起 run_multi --dry-run")
        with open(os.path.join(ROOT, "logs", "overnight_child.out"), "a",
                  encoding="utf-8") as out:
            p = subprocess.Popen(
                ["uv", "run", "python", "-m", "pmbot.run_multi", "--dry-run"],
                cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
            rc = p.wait()
        dur = (time.time() - t0) / 60
        log(f"run_multi 退出 rc={rc} 运行 {dur:.1f} 分钟，10s 后重启")
        time.sleep(10)


if __name__ == "__main__":
    main()