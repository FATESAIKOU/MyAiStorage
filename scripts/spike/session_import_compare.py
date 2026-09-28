"""比較兩次 stub 抓到的模型請求：是否位元組相同、共同前綴有多長。

用法：
  python3 session_import_compare.py A.json B.json [--skill-prefix P1 P2 ...]
--skill-prefix：把 system prompt 裡會因重名 skill 解析而翻轉的路徑前綴
正規化掉（只用於診斷環境噪音；正式判定以原始位元組為準）。
"""

import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--skill-prefix", nargs="*", default=[])
    args = ap.parse_args()
    ra = open(args.a, "rb").read()
    rb = open(args.b, "rb").read()
    print(f"A={args.a} {len(ra)} bytes")
    print(f"B={args.b} {len(rb)} bytes")
    print("FULL byte-identical:", ra == rb)
    if ra != rb:
        i = next((i for i, (x, y) in enumerate(zip(ra, rb)) if x != y), min(len(ra), len(rb)))
        print(f"first diff at byte {i} of {len(ra)}/{len(rb)}")
        print("A:", ra[max(0, i - 120) : i + 200])
        print("B:", rb[max(0, i - 120) : i + 200])
    if args.skill_prefix:
        sa, sb = ra.decode(), rb.decode()
        for p in args.skill_prefix:
            sa = sa.replace(p, "SK/")
            sb = sb.replace(p, "SK/")
        print("normalized-identical:", sa == sb)


if __name__ == "__main__":
    main()
