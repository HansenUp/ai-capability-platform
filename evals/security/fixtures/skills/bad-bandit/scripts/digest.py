"""坏包夹具：以 MD5 作安全摘要（bandit B324，HIGH）；不含 remote_fetch 规则禁止的导入。"""

import hashlib
import json
import sys


def sign(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 —— 故意违规的安全用例夹具


if __name__ == "__main__":
    print(json.dumps({"sig": sign(sys.stdin.buffer.read())}))
