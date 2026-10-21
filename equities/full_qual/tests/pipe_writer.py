"""Child used by the subprocess pipe-drain regression test."""

import sys

payload = "x" * 8192
for _ in range(32):
    sys.stdout.write(payload + "\n")
    sys.stderr.write(payload + "\n")
sys.stdout.flush()
sys.stderr.flush()
