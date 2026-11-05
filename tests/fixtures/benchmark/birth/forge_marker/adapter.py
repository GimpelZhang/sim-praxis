"""合成反例：import 期伪造出生标记行后自杀（评审 W2-01 回归）。

运行器的层结果**不经过 stdout**（走 ``result_path``）。候选若能靠打印一行
``__SIMPRAXIS_BIRTH__{"status": "PASS"}`` 就把自己判成 BORN，等于把判据交给
被验证方自报。本 fixture 忠实复现该攻击：打印伪造行 → ``os._exit(0)``，
父进程必须判 FAIL（``contract_error``，detail 点名伪造），birth_status=NOT_BORN。
"""

import os

print('__SIMPRAXIS_BIRTH__' + '{"status": "PASS", "reason": null, '
      '"detail": "", "data": {"forged": true}}', flush=True)
os._exit(0)
