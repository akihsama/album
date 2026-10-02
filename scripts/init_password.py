#!/usr/bin/env python3
"""生成 PASSWORD_HASH，写进 .env 供容器使用。

用法：
    python scripts/init_password.py

会提示输入两遍口令（输入不回显），成功后打印一行 PASSWORD_HASH=...
把它粘进 .env 即可。口令本身不会被保存。
"""

import getpass
import sys

try:
    from argon2 import PasswordHasher
except ImportError:
    sys.exit("缺少依赖，先执行：pip install argon2-cffi")

# 参数与 app.py 中保持一致
HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def main() -> None:
    pwd = getpass.getpass("新口令: ")
    if not pwd:
        sys.exit("口令不能为空")
    if pwd != getpass.getpass("再输一次: "):
        sys.exit("两次输入不一致")
    if len(pwd) < 8:
        sys.exit("口令至少 8 位")

    print("\n把下面这行写进 .env：\n")
    print(f"PASSWORD_HASH={HASHER.hash(pwd)}")


if __name__ == "__main__":
    main()
