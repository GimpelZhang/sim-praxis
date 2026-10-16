# 锁内适配登记（ADAPTATIONS）

计划（`~/文档/Phase_1_Plan_detailed.md` 及 §1.8 证据树）与实际实现不一致、
但在不改变结论前提下做的最小适配，统一登记在本仓文件（规则 7：锁内适配留痕）。
新增行只追加，不回改历史行。证据包内另有一份面向 §1.8 证据树的映射
（`task9_milestone/evidence_pack/ADAPTATIONS.md`，由
`scripts/milestone1/build_evidence_pack.py::ADAPTATIONS` 生成）；本文件是
仓内偏差的规范登记位置。

| 日期 | 条目 | 计划/原实现 | 实际 | 理由 |
|---|---|---|---|---|
| 2026-10-16 | H-14 视频体积 MB 口径与分档 | Gate2 视频阶梯按 1024² MiB 计算（HARD_LIMIT=30MiB=31,457,280），并按 64/128/256MB 粗分档越级直跳 step3/4 | `orchestrator/feishu_gateway/video.py`：`MB=1_000_000` 十进制，DIRECT_LIMIT 28MB（≤28MB step1 直传），HARD_LIMIT 30,000,000；>28MB 一律从 step2 起，按压缩后实测体积逐级 2→3→4→5，粗分档常量仅留作文档锚点不再选级 | 飞书 im/v1/files（file_type=mp4）硬上限按十进制 30MB；30.0M–30MiB 间产物过本地检查后会被服务端拒收，粗分档会越级直跳 30s 截断档，与 H-2 接触时刻未接线叠加放大误裁剪。Phase 1 实发 7/7 均 step1，既有证据未受影响，结论不变 |
| 2026-10-16 | H-13 存活 SQLite 取证方式 | §1.8/原实现对存活库 `state/pending_gates.db`、`state/langgraph_checkpoints.db` 直接 copy2，且 -wal/-shm 不入包 | `build_evidence_pack.py`：两库经 sqlite3 online backup API 取事务一致快照（导出时合并 WAL），快照置 journal_mode=DELETE 消除侧车，包内副本过 `PRAGMA integrity_check`，manifest 标 `snapshot=sqlite3_backup_api`；全包断言 -wal/-shm 零入包 | 有写入者时主库可能落后未 checkpoint 的 WAL，copy2 快照不事务一致；2026-10-16 打包时 -wal 为 0 字节、无写入者，旧快照内容一致，结论不变 |
| 2026-10-16 | H-11 证据树符号链接复制策略 | 原实现用 rglob 复制：`is_file()` 跟随符号链接文件，会把链接目标实体打进包 | `build_evidence_pack.py`：`os.walk(followlinks=False)` 安全展开，符号链接文件/绝对目标目录链接命中即 FATAL；相对目录链接白名单不下降、仅登记进 `symlinks.json`（新增 relative/kind 字段） | 证据树内若被放入指向 ~/access 或超大文件的链接即被打包（密钥仍有 secret scan 兜底，但非密钥敏感件/超大件无防护），破坏清单自证；现场两个 `latest` 为相对目录链接，白名单保留 |
| 2026-10-16 | H-12 密钥扫描口径双实现差异 | scan_secrets.sh 与 build_evidence_pack 各持 9 条正则副本 | Python 侧以 `orchestrator/common/secrets.py` 为单一事实源（10 条严格口径，新增飞书 `cli_[a-z0-9]{16,}` app_id 与 app_secret/feishu_app_secret 赋值形态），证据包与测试均 import；shell 侧同步为 10 条 | shell ERE 不支持反向引用，赋值形态左右引号各自独立；Python 版用反向引用锁左右同引号，更严。差异为表达能力所限，不影响命中面（真密钥两侧引号一致，两边均命中） |
