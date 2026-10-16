"""SimPraxis 跨域共用基础工具（Phase 1 评审修复批次）。

- fsutil：0600 tmp+replace 原子写（G-7/H-9 的单一正确实现，先例 probes/_evidence.py）
- secrets：密钥正则/脱敏单一事实源（G-8/H-12；router.redact 与证据包共用）
- validators：thread_id/batch_id 字符集与证据路径 containment（G-5/G-9）
- callcache：付费调用级重放缓存（L-1 kill-9 重放幂等）

本包零网络、零付费、零第三方依赖。
"""
