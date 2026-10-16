#!/usr/bin/env python3
"""Phase 2 Task0：Phase 1 封存台账时区一次性归一迁移（P2 历史债）。

把 ``$SIMPRAXIS_PHASE1_ROOT/quota/ledger.jsonl`` 中混写的本地偏移后缀
（实测 29 行 = 18 行 +0800 / 11 行 +0000）逐行换算为**同一 UTC 瞬时**的
严格 Z 后缀（``YYYY-MM-DDTHH:MM:SSZ``）。已是 Z 的行原样保留。

安全性质（本脚本不含任何网络代码）：
  - 默认 **--dry-run**，只打印将要改动的统计，不触碰台账；
  - --apply 前先整库备份到同目录
    ``ledger.jsonl.bak-tz-<UTC 时间戳>``（0600，仓外，绝不入 git）；
  - 换算在内存完成后先校验：行数不变、seq 多重集不变、除 ts_iso 外所有
    字段逐行字节等价，校验不过则原样抛错、不写回；
  - 0600 原子写（fsutil：同目录唯一 tmp 名 + fsync + os.replace）；
  - 幂等：全部行已是 Z 时零改动、零备份，退出 0。

用法：
  python scripts/milestone2/normalize_ledger_tz.py            # dry-run
  python scripts/milestone2/normalize_ledger_tz.py --apply    # 写回 + 备份
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from datetime import timezone  # noqa: E402

from orchestrator.common.clock import (  # noqa: E402
    parse_ts_iso_lenient,
    utc_now_iso_z,
)
from orchestrator.common.fsutil import atomic_write_text  # noqa: E402
from orchestrator.common.runtime_paths import phase1_root  # noqa: E402


def ledger_path() -> Path:
    return phase1_root() / "quota" / "ledger.jsonl"


def _convert_ts(ts: str) -> str:
    """偏移时间串 -> 同一 UTC 瞬时的严格 Z 串（Z 输入原样返回）。"""
    dt = parse_ts_iso_lenient(ts)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(path: Path, *, apply: bool = False) -> dict:
    """归一单个 ledger 文件，返回统计 dict。

    apply=False 为 dry-run：文件不被打开为写。apply=True 时先备份再原子写回。
    """
    if not path.is_file():
        raise FileNotFoundError(f"台账不存在：{path}")
    raw_lines = path.read_text(encoding="utf-8").splitlines()

    before_rows: list[dict] = []
    for i, line in enumerate(raw_lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            before_rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"台账第 {i} 行不可解析（拒绝迁移）：{exc}") from exc

    n_plus_0800 = n_plus_0000 = n_z = n_other = 0
    converted_rows: list[dict] = []
    for row in before_rows:
        ts = row.get("ts_iso")
        new_row = dict(row)
        if isinstance(ts, str) and ts.endswith("Z"):
            n_z += 1
        elif isinstance(ts, str) and (ts.endswith("+0800") or ts.endswith("+0000")):
            if ts.endswith("+0800"):
                n_plus_0800 += 1
            else:
                n_plus_0000 += 1
            new_row["ts_iso"] = _convert_ts(ts)
        elif isinstance(ts, str):
            # 偏移可能是其他时区（如 +0900）：仍按同一瞬时换算，单列计数可见。
            n_other += 1
            new_row["ts_iso"] = _convert_ts(ts)
        else:
            raise ValueError(f"遇到非字符串 ts_iso={ts!r}（拒绝迁移）")
        converted_rows.append(new_row)

    changed = n_plus_0800 + n_plus_0000 + n_other
    stats = {
        "path": str(path), "total": len(before_rows),
        "already_z": n_z, "plus_0800": n_plus_0800,
        "plus_0000": n_plus_0000, "other_offset": n_other,
        "changed": changed, "apply": apply, "backup": None,
    }
    if changed == 0:
        return stats  # 幂等：零改动、零备份

    # 不变量校验（换算前后）：行数 / seq 多重集 / 除 ts_iso 外字段全等。
    if len(converted_rows) != len(before_rows):
        raise AssertionError("换算后行数变化（内部错误）")

    def _seq_multiset(rows: list[dict]) -> Counter:
        return Counter(json.dumps(r.get("seq"), sort_keys=True) for r in rows)

    if _seq_multiset(converted_rows) != _seq_multiset(before_rows):
        raise AssertionError("换算后 seq 多重集变化（拒绝写回）")
    for old, new in zip(before_rows, converted_rows):
        old_rest = {k: v for k, v in old.items() if k != "ts_iso"}
        new_rest = {k: v for k, v in new.items() if k != "ts_iso"}
        if old_rest != new_rest:
            raise AssertionError(
                f"seq={old.get('seq')} 行换算后除 ts_iso 外字段发生变化（拒绝写回）")
        # 同一瞬时校验：lenient 回读两边必须给出相同 UTC epoch。
        t_old = parse_ts_iso_lenient(old["ts_iso"]).astimezone(timezone.utc)
        t_new = parse_ts_iso_lenient(new["ts_iso"]).astimezone(timezone.utc)
        if t_old != t_new:
            raise AssertionError(
                f"seq={old.get('seq')} 换算前后 UTC 瞬时不一致："
                f"{old['ts_iso']} -> {new['ts_iso']}")

    if not apply:
        return stats

    stamp = utc_now_iso_z().replace(":", "")  # 如 2026-10-17T030504Z
    backup = path.with_name(f"{path.name}.bak-tz-{stamp}")
    shutil.copyfile(path, backup)
    backup.chmod(0o600)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in converted_rows)
    atomic_write_text(path, text)
    stats["backup"] = str(backup)
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 1 ledger 时区归一（默认 dry-run）")
    p.add_argument("--apply", action="store_true",
                   help="实际写回（先备份到 ledger.jsonl.bak-tz-<ts>）；缺省只演练")
    p.add_argument("--path", default=None,
                   help="覆盖台账路径（默认 $SIMPRAXIS_PHASE1_ROOT/quota/ledger.jsonl）")
    args = p.parse_args(argv)

    path = Path(args.path) if args.path else ledger_path()
    stats = normalize(path, apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[normalize-ledger-tz][{mode}] {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
