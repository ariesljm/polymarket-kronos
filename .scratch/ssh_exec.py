import os
import sys

import paramiko

# 凭据从环境变量读取（不硬编码）：PMBOT_SSH_HOST / PMBOT_SSH_USER / PMBOT_SSH_PASSWORD
HOST = os.environ.get("PMBOT_SSH_HOST", "")
USER = os.environ.get("PMBOT_SSH_USER", "root")
PASSWORD = os.environ.get("PMBOT_SSH_PASSWORD", "")

def run(cmd, timeout=60):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15, banner_timeout=15)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    client.close()
    return code, out, err

if __name__ == "__main__":
    cmd = sys.argv[1]
    code, out, err = run(cmd)
    print(f"=== EXIT {code} ===")
    if out:
        print("=== STDOUT ===")
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print("=== STDERR ===")
        print(err, end="" if err.endswith("\n") else "\n")
