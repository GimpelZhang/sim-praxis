"""飞书真实审批网关客户端（详细计划 §2 Task 1，L397-407 逐段契约）。

职责边界：
  - 凭据只从构造函数注入（cli/main 入口才读 FEISHU_APP_ID/FEISHU_APP_SECRET/
    FEISHU_RECEIVE_ID 环境变量）；token 仅内存缓存，绝不写库/写日志；
  - requests 直连 open.feishu.cn，超时 15s，code != 0 抛 FeishuAPIError 并落 ws_run.log；
  - WS 生产参数 auto_reconnect=True / log_level=WARNING / on_reconnecting|reconnected 落日志；
  - 回调只做校验→落库→set Event→回 toast（3s 内返回），重活一律不在回调线程；
  - 永不自动批准：任何分支都没有硬编码 approve，等待只发提醒（8h/16h 各一次，共 2 次）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from .cards import (
    APPROVAL_EMPTY_TOAST,
    GATE_MISMATCH_TOAST,
    NOT_OWNER_TOAST,
    REJECT_EMPTY_TOAST,
    build_gate1_card,
    build_gate1_card_ascent,
    build_gate1_card_ascent_v1,
    build_gate1_card_v2,
    build_gate2_card,
    build_gate2_card_diff,
    build_gate2_card_diff4,
    build_gate2_card_diff4_v2,
    build_gate2_card_diff_v2,
    build_gate2_card_v2,
    build_reminder_card,
    build_reminder_card_v2,
    parse_card_event,
    parse_message_event,
)
from .store import PendingStore, now_ms
from .video import (
    HARD_LIMIT,
    compress_command,
    expected_outputs,
    host_to_container_path,
    next_ladder_step,
    select_ladder_step,
    source_duration_note,
    trim_interval_note,
    validate_trim,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Gate2 双活占位行存活观察窗（QA-2 MEDIUM）：claim_pending 失败且读到「全空
# PENDING 行」时，无法区分他方崩溃遗留与他方存活但正处于秒级上传窗——Task6
# runbook 含 kill-9 resume，同一 (thread, gate) 两个存活调用方是真实场景。
# 窗内轮询四个消息列：任一列增长即判他方存活（零发送、纯等待同一裁决）；
# 窗内零变化判陈旧（本次接管补发）；行消失 fail-loud（拒绝孤儿媒体）。
# 测试 monkeypatch 这两个常量（或 time.sleep）确定性快进，禁止真实长 sleep。
CLAIM_OBSERVE_WINDOW_S = 8.0
CLAIM_OBSERVE_TICK_S = 0.2


class FeishuAPIError(RuntimeError):
    """飞书 OpenAPI 返回 code != 0（或 HTTP 层失败）。"""

    def __init__(self, code: Any, msg: str, *, api: str = "") -> None:
        self.code = code
        self.msg = msg
        self.api = api
        super().__init__(f"[{api}] code={code} msg={msg}")


class FeishuGateway:
    BASE = "https://open.feishu.cn/open-apis"
    SESSION_WINDOW_MS = 10 * 60 * 1000          # 通道② 会话窗 10 分钟
    WAIT_TICK_S = 60                            # 阻塞等待节拍
    # 锁内适配（详细计划 §2 Task1 L403“24h/最多 1 次”与主控任务书冲突）：以主控
    # 任务书为准——超 8h、16h 各发一次提醒卡片（共 2 次），任何情况绝不代决。
    REMIND_AFTER_S = (8 * 3600, 16 * 3600)
    HTTP_TIMEOUT = 15

    # 探针 D 裁决点（详细计划 §1.4）：media content 的官方文档形态为
    # {"file_key","image_key"}；image_key 是否必填以实测为准，故 send_media 按
    # 下表顺序尝试，成功形态在运行结果与证据 JSON 中记录。
    MEDIA_CONTENT_SHAPE = {
        "attempt_order": [
            {"name": "file_key+image_key",
             "content": {"file_key": "<files 接口>", "image_key": "<images 接口>"},
             "note": "官方文档形态：视频 file_key + 首帧封面 image_key"},
            {"name": "file_key_only",
             "content": {"file_key": "<files 接口>"},
             "note": "备选：仅 file_key（image_key 是否必填待探针 D 实测裁决）"},
        ],
    }

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        owner_open_id: str,
        *,
        db_path: "str | Path | None" = None,
        evidence_dir: "str | Path | None" = None,
        run_dir: "str | Path | None" = None,
        start_ws: bool = True,
        on_p2p_text: "Callable[[dict], None] | None" = None,
        on_card_action: "Callable[[dict], None] | None" = None,
        require_approval_notes: bool = False,
    ) -> None:
        self.app_id = app_id
        self._app_secret = app_secret
        self.owner_open_id = owner_open_id
        self.store = PendingStore(db_path)
        self.evidence_dir = Path(
            evidence_dir
            or os.path.join(os.environ.get("SIMPRAXIS_PHASE1_ROOT",
                                           "/mnt/artifacts/simpraxis/phase1"),
                            "task1_feishu")
        )
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = run_dir or os.environ.get("SIMPRAXIS_RUN_DIR",
                                                 "/mnt/artifacts/simpraxis/runs")
        self._on_p2p_text = on_p2p_text
        self._on_card_action = on_card_action
        # M2：Task6 生产图显式打开——卡片空批准一律 toast 拒绝、不落决策；
        # 默认 False 保证 Task7/8 既有路径逐字节不变。
        self.require_approval_notes = bool(require_approval_notes)

        self._token: "str | None" = None
        self._token_expire_at = 0.0
        self._token_lock = threading.Lock()
        self._events: dict[tuple[str, int], threading.Event] = {}
        self._events_lock = threading.Lock()
        # 测试/探针计数（重启不重发断言也用 message_id 本身，计数仅便于取证）
        self.send_counts = {"interactive": 0, "media": 0, "text": 0,
                            "files": 0, "images": 0}

        self._log = self._build_logger()
        self._ws_client = None
        self._ws_thread: "threading.Thread | None" = None
        if start_ws:
            self.start_ws()

    # ---------- 基础设施：日志 / token / HTTP ----------

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"feishu_gateway.{id(self)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            fh = logging.FileHandler(self.evidence_dir / "ws_run.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(fh)
        return logger

    def _tenant_token(self) -> str:
        """tenant_access_token 内存缓存，过期前 60s 主动刷新（线程安全）。"""
        with self._token_lock:
            if self._token and time.time() < self._token_expire_at:
                return self._token
            resp = requests.post(
                f"{self.BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self._app_secret},
                timeout=self.HTTP_TIMEOUT,
            )
            body = resp.json()
            if body.get("code") != 0:
                self._log.error("tenant_token 获取失败 code=%s msg=%s",
                                body.get("code"), body.get("msg"))
                raise FeishuAPIError(body.get("code"), body.get("msg", ""),
                                     api="tenant_token")
            self._token = body["tenant_access_token"]
            # 过期前 60s 刷新；无 expire 字段时保守按 90 分钟
            self._token_expire_at = time.time() + int(body.get("expire", 5400)) - 60
            return self._token

    def _post_json(self, url: str, payload: dict, *, api: str) -> dict:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            json=payload, timeout=self.HTTP_TIMEOUT,
        )
        return self._check(resp, api)

    def _post_multipart(self, url: str, *, data: dict, files: dict,
                        api: str) -> dict:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            data=data, files=files, timeout=self.HTTP_TIMEOUT,
        )
        return self._check(resp, api)

    def _check(self, resp: requests.Response, api: str) -> dict:
        try:
            body = resp.json()
        except ValueError as exc:
            self._log.error("%s 非 JSON 响应 http=%s", api, resp.status_code)
            raise FeishuAPIError(resp.status_code, "non-json response", api=api) from exc
        if body.get("code") != 0:
            # 只记路径/返回码/msg，绝不记录 Authorization 与请求体（含 token/密钥纪律）
            self._log.error("%s 失败 code=%s msg=%s", api, body.get("code"),
                            body.get("msg"))
            raise FeishuAPIError(body.get("code"), body.get("msg", ""), api=api)
        return body

    # ---------- 发消息 / 上传 ----------

    def send_text(self, receive_id: str, text: str) -> str:
        body = self._post_json(
            f"{self.BASE}/im/v1/messages?receive_id_type=open_id",
            {"receive_id": receive_id, "msg_type": "text",
             "content": json.dumps({"text": text}, ensure_ascii=False)},
            api="im.messages.text",
        )
        self.send_counts["text"] += 1
        return body["data"]["message_id"]

    def _send_interactive(self, receive_id: str, card1: dict, card2: dict,
                          *, form_required: bool = False) -> dict:
        """发 interactive 卡片；首选形态失败（返回码非 0）自动改发另一形态。

        # 锁内适配（基础计划 §5.1 卡片版本，探针 C 2026-10-14 实测裁决）：
        # 原设计「1.0 先发、非 0 再退 2.0」在真实接口上不成立——1.0 卡混排
        # form/textarea/form_submit 时发送接口【静默返回 code=0】，飞书客户端
        # 只渲染 markdown 文字、form 控件整块消失（空意见打回都无法操作）。
        # 故：含表单的 Gate1/Gate2 卡 form_required=True，2.0 先发；
        # 无表单的提醒卡保持 1.0 先发。两种尝试都落日志。
        """
        if form_required:
            primary, fallback, primary_name, fallback_name = (
                card2, card1, "2.0", "1.0")
            fallback_api = "im.messages.interactive.v1"
        else:
            primary, fallback, primary_name, fallback_name = (
                card1, card2, "1.0", "2.0")
            fallback_api = "im.messages.interactive.v2"
        resp = requests.post(
            f"{self.BASE}/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            json={"receive_id": receive_id, "msg_type": "interactive",
                  "content": json.dumps(primary, ensure_ascii=False)},
            timeout=self.HTTP_TIMEOUT,
        )
        body = resp.json()
        used = primary_name
        if body.get("code") != 0:
            self._log.warning(
                "interactive 卡片 %s 被拒 code=%s msg=%s → 改发 schema %s%s",
                primary_name, body.get("code"), body.get("msg"), fallback_name,
                "（注意：1.0 卡不含可用表单控件）" if form_required else "")
            body = self._post_json(
                f"{self.BASE}/im/v1/messages?receive_id_type=open_id",
                {"receive_id": receive_id, "msg_type": "interactive",
                 "content": json.dumps(fallback, ensure_ascii=False)},
                api=fallback_api,
            )
            used = fallback_name
        else:
            self._log.info("interactive 卡片 %s 发送成功", primary_name)
        self.send_counts["interactive"] += 1
        data = body.get("data", {})
        return {"message_id": data.get("message_id"),
                "chat_id": data.get("chat_id"),
                "schema": used}

    def upload_file(self, path: "str | Path", duration_ms: "int | None" = None) -> str:
        path = Path(path)
        data: dict[str, Any] = {"file_type": "mp4",
                                "file_name": path.name}
        if duration_ms is not None:
            data["duration"] = str(int(duration_ms))
        with path.open("rb") as fh:
            body = self._post_multipart(
                f"{self.BASE}/im/v1/files",
                data=data,
                files={"file": (path.name, fh, "video/mp4")},
                api="im.files",
            )
        self.send_counts["files"] += 1
        return body["data"]["file_key"]

    def upload_image(self, path: "str | Path") -> str:
        path = Path(path)
        with path.open("rb") as fh:
            body = self._post_multipart(
                f"{self.BASE}/im/v1/images",
                data={"image_type": "message"},
                files={"image": (path.name, fh, "image/png")},
                api="im.images",
            )
        self.send_counts["images"] += 1
        return body["data"]["image_key"]

    def send_media(self, receive_id: str, file_key: str,
                   image_key: "str | None" = None) -> dict:
        """按 MEDIA_CONTENT_SHAPE 两种形态顺序尝试，返回 message_id 与成功形态名。"""
        attempts: list[dict] = []
        if image_key:
            attempts.append(("file_key+image_key",
                             {"file_key": file_key, "image_key": image_key}))
        attempts.append(("file_key_only", {"file_key": file_key}))
        last_err: "FeishuAPIError | None" = None
        for name, content in attempts:
            try:
                body = self._post_json(
                    f"{self.BASE}/im/v1/messages?receive_id_type=open_id",
                    {"receive_id": receive_id, "msg_type": "media",
                     "content": json.dumps(content, ensure_ascii=False)},
                    api=f"im.messages.media.{name}",
                )
                self.send_counts["media"] += 1
                self._log.info("media 发送成功 shape=%s message_id=%s", name,
                               body["data"].get("message_id"))
                return {"message_id": body["data"]["message_id"],
                        "shape": name}
            except FeishuAPIError as exc:
                last_err = exc
                self._log.warning("media 形态 %s 被拒 code=%s msg=%s", name,
                                  exc.code, exc.msg)
        assert last_err is not None
        raise last_err

    # ---------- Gate2 视频：阶梯压缩 + 上传 + 降级 ----------

    @staticmethod
    def _file_size(path: "str | Path") -> int:
        """文件字节数（单测 mock 点：无需造 31MB 实体文件即可驱动阶梯判定）。"""
        return Path(path).stat().st_size

    @staticmethod
    def _in_sandbox() -> bool:
        """Task7/8 图整体在 sim-sandbox 容器内运行（compose 注入
        SIMPRAXIS_IN_SANDBOX=1）：此时 /mnt 同路径挂载、ffmpeg 已在镜像内，
        不得再嵌套 `docker compose run`，路径也不做容器 runs 改写。"""
        return os.environ.get("SIMPRAXIS_IN_SANDBOX", "") == "1"

    def _compress_in_container(
        self,
        host_video: Path,
        step: int,
        *,
        trim: "dict | None" = None,
    ) -> tuple[Path, Path]:
        """在 sim-sandbox 环境内执行 ffmpeg（宿主机不装 ffmpeg）。

        两种执行位置（锁内适配，Task7 图在容器内跑）：
          - 宿主：经 `docker compose run` 起一次性容器，RUN_DIR 路径映射成
            /home/agent/runs（host_to_container_path；非 root Task0 起）；
          - 容器内（SIMPRAXIS_IN_SANDBOX=1）：产物在 /mnt 同路径挂载下，直接
            `bash -lc <ffmpeg>` 调镜像内 ffmpeg，禁止嵌套 docker。
        H-2/C1：step4 的裁剪区间由调用方（pipeline lane 计算后）经 trim 透传，
        本方法不自行决定接触时刻；trim 为 None 时用 video.compress_command
        的默认前 30s；step2/3 压缩参数不含裁剪。
        Task1 单测全部 mock 本方法/subprocess；真实执行发生在 Task7/8。
        """
        out_host, cover_host = expected_outputs(host_video, step)
        kw: dict[str, float] = {}
        if trim is not None and step == 4:
            kw = {"trim_start": float(trim["trim_start"]),
                  "trim_seconds": float(trim["trim_seconds"])}
        if self._in_sandbox():
            in_c, out_c, cover_c = str(host_video), str(out_host), str(cover_host)
            cmd = compress_command(in_c, step, out_c, cover_c, **kw)
            argv = ["bash", "-lc", cmd]
            self._log.info("容器内直压 step=%s（同路径 /mnt 挂载，无嵌套 docker）", step)
        else:
            in_c = host_to_container_path(host_video, self.run_dir)
            out_c = host_to_container_path(out_host, self.run_dir)
            cover_c = host_to_container_path(cover_host, self.run_dir)
            cmd = compress_command(in_c, step, out_c, cover_c, **kw)
            argv = [
                "docker", "compose",
                "-f", str(_REPO_ROOT / "sandbox" / "docker-compose.yml"),
                "run", "--rm", "sim-sandbox", "bash", "-lc", cmd,
            ]
            self._log.info("宿主起容器压缩 step=%s argv=%s", step,
                           " ".join(argv[:6]) + " bash -lc <ffmpeg>")
        subprocess.run(argv, check=True, timeout=1800)
        return Path(out_host), Path(cover_host)

    def send_video_native(
        self,
        video_path: "str | Path",
        duration_ms: "int | None" = None,
        cover_png: "str | Path | None" = None,
        *,
        trim: "dict | None" = None,
    ) -> dict:
        """完整五级阶梯：体积判定→容器压缩→files/images/media→失败逐级→step5 降级。

        返回 {ok, video_fallback, ladder_step, media_message_id, media_shape,
              detail, attempts:[...]}。任何分支都不抛 FeishuAPIError（失败收敛为
        video_fallback=True），但 docker/本地文件错误不在此吞（属调用方环境故障）。

        H-14：>28MB 一律 step2 起步，之后按压缩后实测体积对照 HARD_LIMIT 逐级
        2→3→4→5，不再粗分档越级。H-2：trim（C1 四键）为调用方区间，仅在走到
        step4 时透传给 ffmpeg；None 时 step4 裁默认前 30s。
        """
        trim = validate_trim(trim)
        video = Path(video_path)
        cover = Path(cover_png) if cover_png else None
        size = self._file_size(video)
        step = select_ladder_step(size)
        attempts_log: list[dict] = []
        self._log.info("send_video_native size=%d 初始阶梯=%d", size, step)
        while True:
            if step == 5:
                attempts_log.append({"step": 5, "action": "fallback_intranet"})
                return {"ok": False, "video_fallback": True, "ladder_step": 5,
                        "media_message_id": None, "media_shape": None,
                        "detail": "压缩/上传全部失败，降级为内网链接",
                        "attempts": attempts_log}
            if step == 1:
                cur_video, cur_cover = video, cover
            else:
                out_video, out_cover = self._compress_in_container(
                    video, step, trim=trim)
                out_size = self._file_size(out_video)
                attempts_log.append(
                    {"step": step, "action": "compress", "size": out_size})
                if out_size > HARD_LIMIT:
                    self._log.warning("step=%d 压缩后 %d 仍超 30MB，推进阶梯",
                                      step, out_size)
                    step = next_ladder_step(step)
                    continue
                cur_video, cur_cover = out_video, out_cover
            try:
                file_key = self.upload_file(cur_video, duration_ms)
                image_key = None
                if cur_cover is not None:
                    try:
                        image_key = self.upload_image(cur_cover)
                    except FeishuAPIError as exc:
                        # 封面失败不致命：按 file_key_only 形态尝试（MEDIA_CONTENT_SHAPE）
                        self._log.warning("封面上传失败 code=%s，仅用 file_key",
                                          exc.code)
                media = self.send_media(self.owner_open_id, file_key, image_key)
                attempts_log.append({"step": step, "action": "media_ok",
                                     "shape": media["shape"]})
                return {"ok": True, "video_fallback": False, "ladder_step": step,
                        "media_message_id": media["message_id"],
                        "media_shape": media["shape"],
                        "detail": f"原生视频发送成功（step={step}, "
                                  f"shape={media['shape']}）",
                        "attempts": attempts_log}
            except FeishuAPIError as exc:
                attempts_log.append({"step": step, "action": "api_error",
                                     "code": exc.code, "msg": exc.msg})
                self._log.warning("step=%d 上传/发送失败 code=%s，推进阶梯",
                                  step, exc.code)
                step = next_ladder_step(step)

    # ---------- WebSocket 长连接 ----------

    def start_ws(self) -> None:
        import lark_oapi as lark

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_card_action_trigger(self._handle_card)
            .register_p2_im_message_receive_v1(self._handle_message)
            .build()
        )

        def _reconnecting() -> None:
            self._log.warning("WS on_reconnecting：长连接重连中")

        def _reconnected() -> None:
            self._log.warning("WS on_reconnected：长连接已恢复")

        client = lark.ws.Client(
            self.app_id, self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
            auto_reconnect=True,
        )
        # 锁内适配（lark-oapi 1.7.4 实测）：on_reconnecting/on_reconnected 是
        # ws.Client 的可写实例属性（ws/client.py L44-45），不是 __init__ kwarg；
        # 必须在 start() 前赋值，内部 _reconnect 会 try/except 包裹回调。
        client.on_reconnecting = _reconnecting
        client.on_reconnected = _reconnected
        self._ws_client = client
        self._ws_thread = threading.Thread(
            target=self._ws_thread_main, name="feishu-ws", daemon=True)
        self._ws_thread.start()

    def _ws_thread_main(self) -> None:
        try:
            self._ws_client.start()  # type: ignore[union-attr]
        except Exception:
            self._log.exception("WS 线程异常退出（auto_reconnect 仍在内部兜底）")

    def stop_ws(self) -> None:
        # 锁内适配（lark-oapi 1.7.4 实测）：websockets Connection.close() 是协程，
        # 且连接绑定在 WS 线程的模块级 event loop 上（ws/client.py 模块全局 loop）；
        # 从主线程直接同步调用只会产生 "coroutine never awaited" 警告而连接不关。
        # 必须 run_coroutine_threadsafe 投递到该 loop，最多等 3s（close 在探针/
        # 进程退出路径调用，绝不允许阻塞主流程）。
        client = self._ws_client
        if client is None:
            return
        conn = getattr(client, "_conn", None)
        if conn is None:
            return
        try:
            from lark_oapi.ws import client as _lark_ws_mod

            fut = asyncio.run_coroutine_threadsafe(conn.close(), _lark_ws_mod.loop)
            try:
                fut.result(timeout=3)
            except Exception:
                self._log.warning("WS close 协程执行异常/超时（忽略）", exc_info=True)
        except Exception:
            self._log.warning("关闭 WS 连接时异常（忽略）", exc_info=True)

    # ---------- 回调①：卡片 form_submit ----------

    def _toast(self, toast: dict) -> "Any":
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
        return P2CardActionTriggerResponse({"toast": toast})

    def _handle_card(self, data: Any) -> "Any":
        """卡片回调整体 try/except：异常只落日志、回 error toast，绝不杀 WS 线程。"""
        info: dict[str, Any] = {}
        try:
            value, feedback, operator, _chat_id, open_message_id = parse_card_event(data)
            info.update({"value": value, "operator": operator,
                         "feedback": feedback, "message_id": open_message_id})
            # 1) owner 白名单：他人点击拒绝且不落决策
            if operator != self.owner_open_id:
                self._log.warning("非 owner 点击已拒绝 operator=%s", operator)
                info.update({"toast": NOT_OWNER_TOAST, "decided": False,
                             "reason": "not_owner"})
                if self._on_card_action:
                    self._on_card_action(dict(info))
                return self._toast(NOT_OWNER_TOAST)

            gate = int(value.get("gate", 0))
            decision = value.get("decision")
            choice = value.get("choice")
            if gate not in (1, 2) or decision not in (
                    "approve", "reject_with_comments"):
                toast = {"type": "error", "content": "非法按钮值，已忽略"}
                info.update({"toast": toast, "decided": False,
                             "reason": "bad_value"})
                if self._on_card_action:
                    self._on_card_action(dict(info))
                return self._toast(toast)

            # 2) 按卡片 message_id 找 PENDING 行
            row = self._find_pending_by_card(open_message_id)
            if row is None:
                toast = {"type": "info", "content": "该审批已结束或不存在"}
                info.update({"toast": toast, "decided": False,
                             "reason": "no_pending"})
                if self._on_card_action:
                    self._on_card_action(dict(info))
                return self._toast(toast)

            # H-8：按钮 value.gate 必须与卡片对应落库行的 gate 一致，防止旧卡/
            # 伪造回调跨 Gate 落决策（mark_decided 只认按钮 gate，交叉校验失败
            # 一律拒绝且不落库）。
            if int(row["gate"]) != gate:
                self._log.warning(
                    "按钮 gate=%s 与审批单行 gate=%s 不一致，已拒绝 thread=%s",
                    gate, row["gate"], row["thread_id"])
                info.update({"toast": GATE_MISMATCH_TOAST, "decided": False,
                             "reason": "gate_mismatch",
                             "thread_id": row["thread_id"], "gate": gate})
                if self._on_card_action:
                    self._on_card_action(dict(info))
                return self._toast(GATE_MISMATCH_TOAST)

            # 3) 打回空意见二次校验（服务端 form 对 textarea 无强制必填）
            if decision == "reject_with_comments" and not (feedback or "").strip():
                info.update({"toast": REJECT_EMPTY_TOAST, "decided": False,
                             "reason": "empty_feedback",
                             "thread_id": row["thread_id"], "gate": gate})
                self._log.warning("gate=%s 打回意见为空，已拒绝落库", gate)
                if self._on_card_action:
                    self._on_card_action(dict(info))
                return self._toast(REJECT_EMPTY_TOAST)

            # M2（Task6 专用，默认关）：批准同样强制真人原文。否则空批准会
            # 先落 DECIDED 再被 Task6 opinion 接缝以空 notes fail-loud 抛错，
            # 决策已持久化、意见件永不写入，resume 永久复现同一异常——真人
            # Gate1/Gate2 被楔死。此处与打回空意见同构：只 toast、不落库，
            # 行保持 PENDING；p2p/cli 来源仍由接缝 fail-loud 兜底。
            if self.require_approval_notes and decision == "approve" \
                    and not (feedback or "").strip():
                info.update({"toast": APPROVAL_EMPTY_TOAST, "decided": False,
                             "reason": "empty_approval_feedback",
                             "thread_id": row["thread_id"], "gate": gate})
                self._log.warning(
                    "gate=%s 批准意见为空（require_approval_notes），已拒绝落库",
                    gate)
                if self._on_card_action:
                    self._on_card_action(dict(info))
                return self._toast(APPROVAL_EMPTY_TOAST)

            choice_val = int(choice) if choice is not None else None
            ok = self.store.mark_decided(
                row["thread_id"], gate,
                decision=decision, source="card_form",
                choice=choice_val if gate == 1 else None,
                human_notes=feedback or "",
                operator_open_id=operator,
            )
            if not ok:
                toast = {"type": "info", "content": "该审批已有结论，重复点击已忽略"}
                info.update({"toast": toast, "decided": False,
                             "reason": "race_decided"})
                if self._on_card_action:
                    self._on_card_action(dict(info))
                return self._toast(toast)

            self._set_event(row["thread_id"], gate)
            toast = {"type": "success",
                     "content": ("已批准，继续执行" if decision == "approve"
                                 else "已打回并记录意见")}
            info.update({"toast": toast, "decided": True,
                         "thread_id": row["thread_id"], "gate": gate,
                         "decision": decision, "choice": choice_val})
            self._log.info("card_form 落库 thread=%s gate=%s decision=%s choice=%s",
                           row["thread_id"], gate, decision, choice_val)
            if self._on_card_action:
                self._on_card_action(dict(info))
            return self._toast(toast)
        except Exception:
            self._log.exception("card 回调异常（已吞掉以保护 WS 线程）")
            toast = {"type": "error", "content": "服务内部错误，请稍后重试或用 P2P 文本"}
            info.update({"toast": toast, "decided": False, "reason": "exception"})
            if self._on_card_action:
                try:
                    self._on_card_action(dict(info))
                except Exception:
                    pass
            return self._toast(toast)

    def _find_pending_by_card(self, card_message_id: "str | None") -> "dict | None":
        if not card_message_id:
            return None
        for row in self.store.list_pending(status="PENDING"):
            if row["card_message_id"] == card_message_id:
                return row
        return None

    # ---------- 回调②：P2P 文本 ----------

    def _locate_pending_by_reply(
        self,
        ev: dict,
    ) -> "tuple[dict | None, list[dict]]":
        """H-1：按消息回复上下文把 P2P 文本唯一定位到一张待审批卡。

        共享 P2P 会话里多个审批并存，旧实现「取最近一行」会把意见记错线程。
        现只在 status='PENDING' 行里，用 parent_id/root_id/thread_id 匹配该行
        的 card_message_id 或任一媒体消息 id（标准 Gate2 的 media_message_id；
        Task3 双媒体 Diff Gate2 的 media_message_id_v1/v2——回复卡片或回复任一
        视频均可）。返回 (唯一匹配行|None, 该会话全部 PENDING 候选)；匹配 0 或
        >1 时第一元素为 None，由调用方回歧义提示、不落库。
        """
        refs = {r for r in (ev.get("parent_id"), ev.get("root_id"),
                            ev.get("thread_id")) if r}
        pending = self.store.list_pending(status="PENDING",
                                          open_chat_id=ev["chat_id"])
        if not refs:
            return None, pending
        matches = [
            r for r in pending
            if r["card_message_id"] in refs
            or r["media_message_id"] in refs
            or r.get("media_message_id_v1") in refs
            or r.get("media_message_id_v2") in refs
        ]
        if len(matches) == 1:
            return matches[0], pending
        return None, pending

    @staticmethod
    def _ambiguity_text(pending: list[dict]) -> str:
        """0/多匹配时的歧义回执：列出候选并引导用卡片按钮，绝不猜测归属。"""
        if not pending:
            return ("未找到进行中的审批单：本消息未记录。请直接点击审批卡片上的"
                    "按钮，或在对应审批卡片/视频下回复。")
        lines = ["无法确定这条意见属于哪张审批卡，**未记录**。"
                 "请直接在对应审批卡片下回复（或点卡片按钮）。当前待审批："]
        for r in pending:
            title = r.get("task_title") or "（无任务名）"
            lines.append(f"- Gate{r['gate']}｜{title}｜线程 `{r['thread_id']}`")
        return "\n".join(lines)

    def _handle_message(self, data: Any) -> None:
        try:
            ev = parse_message_event(data)
            info = dict(ev)
            if ev["chat_type"] != "p2p":
                return
            if ev["sender_open_id"] != self.owner_open_id:
                self._log.warning("非 owner 文本已忽略 sender=%s",
                                  ev["sender_open_id"])
                return
            text = (ev["text"] or "").strip()
            if not text:
                return
            # H-1：只认 PENDING 行；必须能凭回复上下文唯一定位，否则不落库。
            row, pending = self._locate_pending_by_reply(ev)
            refs = {ev.get("parent_id"), ev.get("root_id"), ev.get("thread_id")}
            refs.discard(None)
            if not pending:
                if refs:
                    # 明确回复了某张卡/视频但对应审批已终态（行已不在 PENDING）：
                    # 回执告知未记录，避免操作者误以为意见已进入裁决。
                    self._log.info(
                        "p2p 回复无 PENDING 审批可定位（审批已结束？），回执不落库 "
                        "chat=%s refs=%d", ev["chat_id"], len(refs))
                    try:
                        self.send_text(ev["chat_id"],
                                       self._ambiguity_text([]))
                    except FeishuAPIError:
                        self._log.warning("p2p 歧义回执发送失败", exc_info=True)
                    self._on_card_safe({**info, "accepted": False,
                                        "reason": "ambiguous", "candidates": 0})
                else:
                    # 无任何待审批且非回复上下文（闲聊飘消息）：静默不回复，
                    # 探针 B 仍可经回调观察到达事件。
                    self._log.info(
                        "p2p 文本无 PENDING 审批行，静默不回复 chat=%s",
                        ev["chat_id"])
                    self._on_card_safe({**info, "accepted": False,
                                        "reason": "no_pending"})
                return
            if row is None:
                # 0 或 >1 匹配：回歧义提示，明确告知未记录
                self._log.info("p2p 文本无法唯一定位审批卡（候选 %d），不落库",
                               len(pending))
                try:
                    self.send_text(ev["chat_id"], self._ambiguity_text(pending))
                except FeishuAPIError:
                    self._log.warning("p2p 歧义回执发送失败", exc_info=True)
                self._on_card_safe({**info, "accepted": False,
                                    "reason": "ambiguous",
                                    "candidates": len(pending)})
                return
            age_ms = now_ms() - int(row["created_at"])
            if age_ms > self.SESSION_WINDOW_MS:
                self._log.info(
                    "p2p 文本超出 10 分钟会话窗（%dms），忽略 thread=%s gate=%s",
                    age_ms, row["thread_id"], row["gate"])
                info.update({"accepted": False, "reason": "outside_window"})
                if self._on_p2p_text:
                    self._on_card_safe(info)
                return
            stamp = datetime.now().strftime("%H:%M:%S")
            line = f"[文本 {stamp}] {text}"
            # H-1：message_id 同事务去重（飞书重投/SDK 重放不产生重复意见），
            # 且仅 PENDING 行可追加。
            ok = self.store.append_human_note(
                row["thread_id"], int(row["gate"]), line,
                message_id=ev["message_id"], open_chat_id=ev["chat_id"])
            if not ok:
                self._log.info("p2p 重复投递或行已终态，忽略 message_id=%s",
                               ev["message_id"])
                self._on_card_safe({**info, "accepted": False,
                                    "reason": "duplicate_or_decided"})
                return
            task_name = row.get("task_title") or "（无任务名）"
            receipt = (f"已记录到 Gate{row['gate']} 审批（线程 {row['thread_id']}，"
                       f"任务：{task_name}）：{len(text)} 字")
            self._log.info("p2p 意见已记录 %d 字 thread=%s gate=%s",
                           len(text), row["thread_id"], row["gate"])
            try:
                self.send_text(ev["chat_id"], receipt)
            except FeishuAPIError:
                # 回执失败不影响意见已落库；静默不报错（通道②主流程不依赖回执）
                self._log.warning("p2p 回执发送失败（意见已落库）", exc_info=True)
            info.update({"accepted": True, "line": line,
                         "thread_id": row["thread_id"], "gate": row["gate"],
                         "status": "PENDING"})
            if self._on_p2p_text:
                self._on_card_safe(info)
        except Exception:
            self._log.exception("message 回调异常（已吞掉以保护 WS 线程）")

    def _on_card_safe(self, info: dict) -> None:
        if self._on_p2p_text:
            try:
                self._on_p2p_text(info)
            except Exception:
                self._log.warning("on_p2p_text 探针回调异常（已忽略）", exc_info=True)

    # ---------- Event 绑定 / 阻塞等待 ----------

    def _reopen_round(self, thread_id: str, gate: int) -> bool:
        """Gate 打回后新一轮请求：归档终态行、清旧 Event（Task7 D10 多轮语义）。

        不归档时（无终态行，如极端竞态下只有无 card 的 PENDING/无行）原样继续；
        归档后旧卡片消息 id 只存在于 history，_find_pending_by_card 匹配不到
        PENDING 行，旧卡点击自动失效。
        """
        archived = self.store.archive_decided(thread_id, gate)
        if archived:
            self._event_for(thread_id, gate).clear()
            self._log.info("Gate%s 新一轮请求：上一轮决策已归档重发卡片 thread=%s",
                           gate, thread_id)
        return archived

    # claim 冲突裁决观察的消息列（标准列 + 双媒体两列 + Task6 四媒体四列 + 卡片列）。
    _CLAIM_OBSERVE_COLUMNS = (
        "card_message_id", "media_message_id",
        "media_message_id_v1", "media_message_id_v2",
        "media_message_id_v1a", "media_message_id_v1b",
        "media_message_id_v2a", "media_message_id_v2b")

    @classmethod
    def _claim_snapshot(cls, row: dict) -> tuple:
        return tuple(row[c] for c in cls._CLAIM_OBSERVE_COLUMNS)

    def _observe_claimed_row(
        self, thread_id: str, gate: int, *, baseline: tuple
    ) -> str:
        """全空占位行存活观察窗（QA-2 MEDIUM），纯读 + tick 等待，绝不发送。

        返回三态：
        - "alive"：窗内四列中任一列相对 baseline 变化（他方在秒级上传/发卡）；
        - "stale"：观察窗结束行列值毫无变化（判他方崩溃，调用方接管补发）；
        - "gone"：行消失，或状态已翻转为 DECIDED/EXPIRED（他方裁决归档的极端
          窗口，调用方 fail-loud）。

        R-LOW-2：状态翻转优先于消息列观察——对手可经 CLI/P2P 文本通道不经任何
        消息列就把行裁定为 DECIDED/EXPIRED；只比对四列会误判 stale/alive，接管
        补发即成孤儿媒体/孤儿卡。
        """
        deadline = time.monotonic() + CLAIM_OBSERVE_WINDOW_S
        while True:
            time.sleep(CLAIM_OBSERVE_TICK_S)
            row = self.store.get_pending(thread_id, gate)
            if row is None:
                return "gone"
            # R-LOW-2：先查状态再比对消息列，任何非 PENDING 终结态一律 gone。
            if row["status"] != "PENDING":
                self._log.warning(
                    "Gate%s 占位行观察窗内状态翻转为 %s（他方已终结此行，四消息列"
                    "可能仍全空），放弃接管 thread=%s",
                    gate, row["status"], thread_id)
                return "gone"
            if self._claim_snapshot(row) != baseline:
                return "alive"
            if time.monotonic() >= deadline:
                return "stale"

    def _claim_loss_action(self, thread_id: str, gate: int) -> str:
        """claim_pending 返回 False 后的统一裁决（纯读，零发送）。

        - "wait"：他方卡片已发（既有快路），或全空占位行观察窗内长出列
          （他方存活）——本次零发送，直接 _await_decision 等同一裁决；
        - "take"：部分消息列已回填的崩溃现场（立即接管，零等待），或全空行
          观察窗零变化（陈旧接管，缺啥补啥）；
        - "gone"：行消失或已非 PENDING——调用方必须 fail-loud，拒绝补发
          孤儿媒体/孤儿卡（reviewer LOW-4 同面）。
        """
        existing = self.store.get_pending(thread_id, gate)
        if existing is None or existing["status"] != "PENDING":
            return "gone"
        if existing["card_message_id"]:
            return "wait"
        snapshot = self._claim_snapshot(existing)
        if any(snapshot[1:]):  # card 必空；任一媒体列已回填=部分送达的崩溃现场
            return "take"
        verdict = self._observe_claimed_row(thread_id, gate, baseline=snapshot)
        if verdict == "gone":
            return "gone"
        if verdict == "alive":
            self._log.warning(
                "Gate%s 全空占位行观察窗内有列增长（他方存活上传中），本次零发送"
                "纯等待 thread=%s", gate, thread_id)
            return "wait"
        self._log.warning(
            "Gate%s 全空占位行观察窗内零变化（判他方崩溃遗留），本次接管补发 "
            "thread=%s", gate, thread_id)
        return "take"

    def _event_for(self, thread_id: str, gate: int) -> threading.Event:
        key = (thread_id, gate)
        with self._events_lock:
            ev = self._events.get(key)
            if ev is None:
                ev = threading.Event()
                self._events[key] = ev
            return ev

    def _set_event(self, thread_id: str, gate: int) -> None:
        self._event_for(thread_id, gate).set()

    def _await_decision(
        self, thread_id: str, gate: int, *, max_wait: "float | None" = None
    ) -> dict:
        ev = self._event_for(thread_id, gate)
        start = time.monotonic()
        while True:
            ev.wait(self.WAIT_TICK_S)
            row = self.store.get_pending(thread_id, gate)
            if row is not None and row["status"] == "DECIDED":
                return {"decision": row["decision"],
                        "choice": row["choice"],
                        "human_notes": row["human_notes"],
                        "source": row["source"]}
            # H-6：提醒节奏落库（reminder_count/last_reminder_at），调度锚点用
            # 行 created_at 的墙钟时间——进程重启后 8h/16h 节奏不重置、不重复发。
            count, _last = self.store.get_reminder_state(thread_id, gate)
            if (row is not None and row["status"] == "PENDING"
                    and count < len(self.REMIND_AFTER_S)):
                due_at_ms = int(row["created_at"]) + self.REMIND_AFTER_S[count] * 1000
                if now_ms() >= due_at_ms:
                    task_title = row.get("task_title")
                    if self._send_reminder(gate, count + 1, thread_id, task_title):
                        # 仅发送成功才计数；失败下一节拍重试，不跳过该档。
                        self.store.bump_reminder(thread_id, gate)
            elapsed = time.monotonic() - start
            if max_wait is not None and elapsed >= max_wait:
                # 绝不代决：超时仅抛出，由上层决定是否继续等待
                raise TimeoutError(
                    f"Gate{gate} 等待 {elapsed:.0f}s 未决策（禁止自动批准）")

    def _send_reminder(
        self,
        gate: int,
        nth: int,
        thread_id: "str | None" = None,
        task_title: "str | None" = None,
    ) -> bool:
        """发提醒卡（H-6：卡片自带任务名与线程 ID）。成功返回 True。"""
        try:
            waited_hours = 8 if nth == 1 else 16
            self._send_interactive(
                self.owner_open_id,
                build_reminder_card(gate, nth, waited_hours,
                                    task_title=task_title, thread_id=thread_id),
                build_reminder_card_v2(gate, nth, waited_hours,
                                       task_title=task_title, thread_id=thread_id))
            self._log.warning("Gate%s 第 %d 次等待提醒卡片已发送 thread=%s",
                              gate, nth, thread_id)
            return True
        except FeishuAPIError:
            self._log.warning("Gate%s 第 %d 次提醒发送失败（下一节拍重试计数不增）",
                              gate, nth, exc_info=True)
            return False

    # ---------- R-LOW-3：发送窗状态复查 + 孤儿回填留痕 ----------

    def _attach_ids_or_warn(
        self, thread_id: str, gate: int, *, sent_kind: str,
        **cols: "str | None",
    ) -> "int | None":
        """回填消息 id；回填 0 行说明占位行已终结——刚发出的是孤儿，warning 留痕。

        返回 store.attach_message_ids 原值：None=本次未传填充列（未尝试回填）；
        1=PENDING 占位行正常回填（COALESCE 全 no-op 也算 1，行仍存活）；
        0=行不存在或已非 PENDING——sent_kind 已实际发出但 id 无处落账（孤儿
        媒体/孤儿卡），日志带 thread/gate/所发 ids 供运维召回。发送与回填之间
        天然有秒级竞态窗，本告警只做 best-effort recall，不保证强一致。
        """
        n = self.store.attach_message_ids(thread_id, gate, **cols)
        if n == 0:
            ids = {k: v for k, v in cols.items() if v is not None}
            self._log.warning(
                "Gate%s 占位行已终结但 %s 已发出（孤儿，回填 0 行）"
                "thread=%s gate=%s ids=%s",
                gate, sent_kind, thread_id, gate, ids)
        return n

    def _fresh_pending_or_abort(
        self, thread_id: str, gate: int, *, phase: str, orphan_hint: str
    ) -> dict:
        """发送序列内两个副作用之间的 fresh 状态复查（best-effort 闭合秒级窗）。

        行仍 PENDING：返回 fresh 行；行消失或已被他方终结：先 warning（与观察窗
        gone 路径同形）再 raise RuntimeError——未发的件不再发送，此前已发的件
        靠 _attach_ids_or_warn 的 0 行孤儿告警留痕。
        """
        row = self.store.get_pending(thread_id, gate)
        if row is not None and row["status"] == "PENDING":
            return row
        status = row["status"] if row else None
        self._log.warning(
            "Gate%s %s前 fresh 复查：占位行已终结（status=%s），未发件不发；"
            "此前已发消息可能为孤儿，见回填 0 行告警 thread=%s",
            gate, phase, status, thread_id)
        raise RuntimeError(
            f"Gate{gate} 占位行在{phase}前已终结（status={status}），未发件不发；"
            f"{orphan_hint}，请走 resume 读终态 thread={thread_id}")

    # ---------- 对外主入口 ----------

    def request_gate1_approval(
        self,
        thread_id: str,
        task_title: str,
        proposals: list[dict[str, Any]],
        *,
        max_wait: "float | None" = None,
    ) -> dict:
        """发卡（或重启重挂）并阻塞等待 Gate1 决策。禁止任何硬编码 return approve。"""
        gate = 1
        row = self.store.get_pending(thread_id, gate)
        if row is not None and row["status"] == "PENDING" and row["card_message_id"]:
            # 重启重挂路径：已有 PENDING 行且卡片已发——不重发，复用 message_id
            self._log.info("Gate1 重启重挂 thread=%s message_id=%s（不重发卡片）",
                           thread_id, row["card_message_id"])
        else:
            # Task7 D10：上一轮已决策（打回）时先归档终态行，保证是新卡片新点击
            self._reopen_round(thread_id, gate)
            sent = self._send_interactive(
                self.owner_open_id,
                build_gate1_card(task_title, proposals),
                build_gate1_card_v2(task_title, proposals),
                form_required=True,
            )
            self.store.upsert_pending(
                thread_id, gate,
                card_message_id=sent["message_id"],
                open_chat_id=sent["chat_id"],
                task_title=task_title)
            # 极端竞态：upsert 前行已存在但无 message_id，attach 再保底回填一次
            # （R-LOW-3：0 行=卡片刚发出行即终结，走孤儿告警留痕）
            self._attach_ids_or_warn(
                thread_id, gate, sent_kind="Gate1 卡片",
                card_message_id=sent["message_id"],
                open_chat_id=sent["chat_id"])
            self._log.info("Gate1 卡片已发出 thread=%s schema=%s",
                           thread_id, sent["schema"])
        self._event_for(thread_id, gate)
        return self._await_decision(thread_id, gate, max_wait=max_wait)

    @staticmethod
    def _build_trim_note(
        trim: "dict | None",
        ladder_step: "int | None",
    ) -> str:
        """H-2：卡片/video_detail 的原片时长与裁剪区间注记。

        - 原片时长只要调用方给了就始终展示（含 step<4 与未裁剪视频）；
        - 只有阶梯实际走到 step4（发生裁剪）才追加「裁剪区间 …（接触时刻…/
          分屏无单一接触）」；调用方未给 trim 时按默认 0–30s、无单一接触呈现。
        """
        parts: list[str] = []
        if ladder_step == 4:
            if trim is None:
                trim = {"trim_start": 0.0, "trim_seconds": 30.0,
                        "contact_moment_s": None, "source_duration_s": None}
            parts.append(trim_interval_note(trim))
        if trim is not None:
            note = source_duration_note(trim.get("source_duration_s"))
            if note:
                parts.append(note)
        return "，".join(parts)

    def request_gate2_approval(
        self,
        thread_id: str,
        task_title: str,
        metrics: dict[str, Any],
        video_path: "str | None" = None,
        intranet_url: "str | None" = None,
        *,
        duration_ms: "int | None" = None,
        cover_png: "str | None" = None,
        explanation: "dict[str, str] | None" = None,
        trim: "dict | None" = None,
        max_wait: "float | None" = None,
    ) -> dict:
        """先发原生视频（五级阶梯，可能降级）再发 Gate2 卡片，阻塞等待验收结论。

        explanation 为卡片顶部的人话说明块（dict：task/problem/video 三问，
        见 cards._EXPLAIN_LABELS），由 Task7 真人 Gate2 打回逐字要求引入
        （2026-10-15：「说明这个仿真任务是做什么的，解决什么问题，发来的
        视频是什么意思」）。

        H-2/C1：trim 为 pipeline lane 计算的调用方裁剪区间（严格四键，见
        video.TRIM_KEYS）；本 lane 只透传到 step4 ffmpeg 与卡片文案，不自行
        计算接触时刻。H-4：发视频/卡片之前先在库内占位（PENDING 空行），
        推送失败也保留占位供下次重挂，任何路径不重复推视频；消息 id 在发送
        成功后只做一次 COALESCE 回填（不再 upsert+attach 双写）。
        QA-2：claim 失败遇全空占位行进存活观察窗（默认 8s/0.2s tick）——
        窗内他方长出消息列则本次零发送纯等待，零变化才判崩溃接管，行消失
        fail-loud（见 _claim_loss_action）。
        """
        gate = 2
        # 在任何副作用之前校验 trim（非法契约直接抛错，不推视频、不写占位）。
        trim = validate_trim(trim)
        row = self.store.get_pending(thread_id, gate)
        if row is not None and row["status"] == "PENDING" and row["card_message_id"]:
            self._log.info("Gate2 重启重挂 thread=%s message_id=%s（不重发卡片）",
                           thread_id, row["card_message_id"])
        else:
            # 打回后的新一轮：先归档上一轮终态行，否则旧 reject 会被本轮
            # _await_decision 首 tick 读到而秒判第二次打回（见 store._DDL_HISTORY）。
            self._reopen_round(thread_id, gate)
            # H-4：占位先行。claim 失败说明主表已有行——若已带 card_message_id
            # 视为他方发卡完成，直接进入等待、绝不重复推视频；无 card id 的
            # claim 失败交 _claim_loss_action 三分裁决（QA-2 MEDIUM）：
            # 部分媒体列已回填=崩溃现场，立即接管零等待；四列全空=双活歧义，
            # 进存活观察窗（alive 零发送纯等待 / stale 接管补发）；行消失
            # fail-loud，拒绝孤儿媒体/孤儿卡（reviewer LOW-4）。
            claimed = self.store.claim_pending(thread_id, gate,
                                                task_title=task_title)
            other_card_ready = False
            if not claimed:
                existing = self.store.get_pending(thread_id, gate)
                if existing is not None and existing["status"] == "PENDING" \
                        and existing["card_message_id"]:
                    other_card_ready = True
                    self._log.warning(
                        "Gate2 占位已被他方持有且卡片已发，本次不重复推视频 "
                        "thread=%s card=%s", thread_id,
                        existing["card_message_id"])
                else:
                    claim_action = self._claim_loss_action(thread_id, gate)
                    if claim_action == "gone":
                        raise RuntimeError(
                            "Gate2 占位行在 claim 后消失，疑似他方已裁决归档；"
                            "拒绝发送孤儿媒体/孤儿卡，请走 resume 读终态 "
                            f"thread={thread_id}")
                    if claim_action == "wait":
                        # 观察窗内他方长出列（存活上传中）：本次零发送，落到
                        # 下方 _await_decision 纯等待同一裁决。
                        other_card_ready = True
            if not other_card_ready:
                # R-LOW-2：首个副作用（推视频/发卡）之前对行状态做一次 fresh
                # SELECT——claim 与观察窗读到的都是更早的快照，对手可能在两读
                # 之间把行裁定为 DECIDED/EXPIRED（TOCTOU）。状态已翻转即放弃
                # 接管，拒绝补发孤儿媒体/孤儿卡，与 claim 损失 gone 分支同语义。
                fresh = self.store.get_pending(thread_id, gate)
                if fresh is None or fresh["status"] != "PENDING":
                    # R-LOW-3 NIT：raise 前补 warning（与观察窗 gone 路径同形）。
                    self._log.warning(
                        "Gate%s 首次发送前 fresh 复查：占位行已终结（status=%s），"
                        "拒绝发送孤儿媒体/孤儿卡 thread=%s",
                        gate, fresh["status"] if fresh else None, thread_id)
                    raise RuntimeError(
                        "Gate2 占位行在首次发送前已终结（status="
                        f"{fresh['status'] if fresh else None}），疑似他方已裁决；"
                        "拒绝发送孤儿媒体/孤儿卡，请走 resume 读终态 "
                        f"thread={thread_id}")
                media_message_id = None
                video_fallback = False
                video_detail = "未提供视频"
                ladder_step = None
                stale = None if claimed else fresh
                reattach_media = bool(
                    stale is not None and stale["status"] == "PENDING"
                    and not stale["card_message_id"] and stale["media_message_id"])
                if reattach_media:
                    # H-4 重挂：占位行已带 media_message_id（视频此前已送达，
                    # 仅卡片发送失败/进程崩溃）——只补发卡片，绝不重复推视频。
                    media_message_id = stale["media_message_id"]
                    video_detail = "复用已发送视频（重挂卡片，未重复推送）"
                    # 实际裁剪档未知：只展示原片时长，不虚构裁剪区间。
                    trim_note = self._build_trim_note(trim, None)
                elif video_path:
                    vr = self.send_video_native(video_path, duration_ms=duration_ms,
                                                cover_png=cover_png, trim=trim)
                    video_fallback = bool(vr["video_fallback"])
                    media_message_id = vr["media_message_id"]
                    video_detail = vr["detail"]
                    ladder_step = vr["ladder_step"]
                    # H-4：视频送达后立刻回填 media_message_id——即便随后发卡失败/
                    # 进程崩溃，重挂分支也只补卡片、绝不重复推视频。
                    # R-LOW-3：翻转落在秒级上传窗内时回填 0 行，封装告警留痕，
                    # 下方卡片前复查随即 raise，卡片不发。
                    if media_message_id is not None:
                        self._attach_ids_or_warn(
                            thread_id, gate, sent_kind="Gate2 视频",
                            media_message_id=media_message_id)
                    trim_note = self._build_trim_note(trim, ladder_step)
                    if trim_note:
                        video_detail += f"｜{trim_note}"
                    if intranet_url is None and video_fallback:
                        video_detail += "（且未提供 intranet_url，仅能查看证据目录原片）"
                else:
                    trim_note = self._build_trim_note(trim, None)
                # R-LOW-3：卡片发送（序列里最后一个副作用）之前再做一次 fresh
                # 复查，闭合秒级上传窗内的翻转——翻转发生在视频 send/attach 之后
                # 时视频列已落账（非孤儿）；发生在视频上传窗内时上面的 attach 0
                # 行已告警，此处 raise 保证卡片不发（含 H-4 reattach_media 只补卡
                # 路径：不重发视频但发卡前同样过此复查）。
                if reattach_media:
                    orphan_hint = ("复用的历史视频本次未重发（若其早已成孤儿，"
                                   "见视频发送当时的 0 行回填告警）")
                elif video_path:
                    orphan_hint = "视频可能已在上传窗内发出（孤儿，见 0 行回填告警）"
                else:
                    orphan_hint = "本次无媒体在途，卡片未发"
                self._fresh_pending_or_abort(
                    thread_id, gate, phase="卡片发送", orphan_hint=orphan_hint)
                url = intranet_url
                sent = self._send_interactive(
                    self.owner_open_id,
                    build_gate2_card(task_title, metrics, url,
                                     video_fallback=video_fallback,
                                     explanation=explanation,
                                     trim_note=trim_note),
                    build_gate2_card_v2(task_title, metrics, url,
                                        video_fallback=video_fallback,
                                        explanation=explanation,
                                        trim_note=trim_note),
                    form_required=True,
                )
                # H-4：占位行已在推视频前落库；media_message_id 在视频送达后
                # 已先回填一次，这里回填 card_message_id/open_chat_id（全部
                # COALESCE，重复调用幂等；若发卡抛异常，占位+视频 id 保留，
                # 下次走重挂分支只补卡片，绝不重推视频）。
                # R-LOW-3：回填 0 行=卡片在发送秒级窗内变成孤儿，封装已告警；
                # 占位行既已终结，fail-loud 交 resume 读终态，不再进入等待。
                attached = self._attach_ids_or_warn(
                    thread_id, gate, sent_kind="Gate2 卡片",
                    card_message_id=sent["message_id"],
                    media_message_id=media_message_id,
                    open_chat_id=sent["chat_id"])
                if attached == 0:
                    raise RuntimeError(
                        "Gate2 卡片刚发出但占位行已终结（孤儿卡片，回填 0 行，"
                        "已告警留痕），请走 resume 读终态 "
                        f"thread={thread_id}")
                self._log.info(
                    "Gate2 卡片已发出 thread=%s schema=%s video=%s step=%s",
                    thread_id, sent["schema"], video_detail, ladder_step)
        self._event_for(thread_id, gate)
        return self._await_decision(thread_id, gate, max_wait=max_wait)

    # ---------- Phase 2 Task3：双媒体 Diff Gate2 ----------

    # 列标签行只在本次实际推送该列视频时发送，从不落 store（重挂不重发标签）。
    _DIFF_LABEL_V1 = "① V1 历史视频（冻结档案）"
    _DIFF_LABEL_V2 = "② V2 本次视频（候选实证）"

    def _send_diff_column(
        self,
        thread_id: str,
        column: str,
        label: str,
        existing_id: "str | None",
        video_path: "str | None",
        trim: "dict | None",
        *,
        duration_ms: "int | None",
        cover_png: "str | None",
    ) -> "tuple[str | None, bool, str, int | None]":
        """推送双媒体中的一列并回填该列消息 id；返回 (id, fallback, detail, step)。

        existing_id 非空（H-4 占位行已记账）= 该列此前已送达：不重发标签、不重发
        视频。缺列且本次没给路径 → fail-closed 抛错，不用单媒体冒充双媒体看板。
        """
        if existing_id:
            return (existing_id, False,
                    f"复用已发送 {column} 视频（重挂接管，未重复推送）", None)
        if not video_path:
            raise ValueError(
                f"Gate2(diff) {column} 列无已送达消息 id 且未提供视频路径，"
                "无法补发；禁止以单媒体冒充双媒体 Diff 看板")
        # 标签行紧贴该列视频之前；两列严格顺序由调用方 v1→v2 串行保证。
        self.send_text(self.owner_open_id, label)
        vr = self.send_video_native(video_path, duration_ms=duration_ms,
                                    cover_png=cover_png, trim=trim)
        media_id = vr["media_message_id"]
        if media_id is not None:
            # H-4：送达即刻单列回填——即便随后崩在第二列/卡片，接管方也只补缺件。
            # R-LOW-3：回填 0 行（行列在秒级上传窗内被他方终结）走孤儿告警留痕。
            self._attach_ids_or_warn(
                thread_id, 2,
                sent_kind=f"Gate2(diff) {column.upper()} 列视频",
                **{f"media_message_id_{column}": media_id})
        return media_id, bool(vr["video_fallback"]), vr["detail"], vr["ladder_step"]

    def request_gate2_diff_approval(
        self,
        thread_id: str,
        task_title: str,
        v1_video: str,
        v2_video: str,
        metrics_diff: list[dict[str, Any]],
        pareto_verdict: "str | dict[str, Any]",
        *,
        duration_ms: "int | None" = None,
        cover_png: "str | None" = None,
        trim: "dict | None" = None,
        explanation: "dict[str, str] | None" = None,
        max_wait: "float | None" = None,
    ) -> dict:
        """Task3 双媒体 Diff Gate2：V1 历史视频 + V2 本次视频 + Δ 裁决卡。

        严格顺序（H-4 占位先行，LOW-6 崩溃接管）：
        claim 占位 → 标签「① V1 历史视频」→ 原生视频 V1（成功即回填 v1 列）
        → 标签「② V2 本次视频」→ 原生视频 V2（回填 v2 列）→ Diff 表单卡 →
        阻塞等待人类最终裁决。任一已送达列不重发、其标签行也不重发（标签从不
        持久化）；两列都在则只补卡片；占位被他方持有且卡片已发 → 只等待。
        QA-2：claim 失败遇全空占位行进存活观察窗（CLAIM_OBSERVE_WINDOW_S/
        TICK_S），窗内他方长列→零标签零视频零卡片纯等待，零变化→判崩溃接管，
        行消失→RuntimeError fail-loud。
        metrics_diff/pareto_verdict 在任何副作用之前先过卡片构造校验（fail-closed）。
        duration_ms/cover_png/trim 两列同参（V1/V2 是否同截窗由调用方决定）。
        """
        gate = 2
        trim = validate_trim(trim)
        # 预检：非法 Δ 行/裁决值在占位与推送之前抛错（构造为纯函数，零副作用）。
        build_gate2_card_diff_v2(task_title, metrics_diff, pareto_verdict,
                                 explanation=explanation)
        row = self.store.get_pending(thread_id, gate)
        if row is not None and row["status"] == "PENDING" and row["card_message_id"]:
            self._log.info(
                "Gate2(diff) 重启重挂 thread=%s message_id=%s（不重发卡片）",
                thread_id, row["card_message_id"])
        else:
            self._reopen_round(thread_id, gate)
            claimed = self.store.claim_pending(thread_id, gate,
                                                task_title=task_title)
            other_card_ready = False
            if not claimed:
                existing = self.store.get_pending(thread_id, gate)
                if existing is not None and existing["status"] == "PENDING" \
                        and existing["card_message_id"]:
                    other_card_ready = True
                    self._log.warning(
                        "Gate2(diff) 占位已被他方持有且卡片已发，本次不重复推视频 "
                        "thread=%s card=%s", thread_id,
                        existing["card_message_id"])
                else:
                    # QA-2 MEDIUM：无 card 的 claim 失败三分裁决（与标准 Gate2
                    # 同构）——部分回填立即接管；全空行进观察窗区分双活/崩溃；
                    # 行消失 fail-loud，零标签零视频零卡片（reviewer LOW-4）。
                    claim_action = self._claim_loss_action(thread_id, gate)
                    if claim_action == "gone":
                        raise RuntimeError(
                            "Gate2(diff) 占位行在 claim 后消失，疑似他方已裁决"
                            "归档；拒绝发送孤儿媒体/孤儿卡，请走 resume 读终态 "
                            f"thread={thread_id}")
                    if claim_action == "wait":
                        # 他方存活上传中：本次零发送，纯等待同一裁决。
                        other_card_ready = True
            if not other_card_ready:
                # R-LOW-2：首个 _send_diff_column 副作用（标签/V1 视频）之前
                # fresh 重读行状态，理由与标准 Gate2 同（claim/观察快照之后的
                # TOCTOU 翻转）。非 PENDING 即走同一 gone 语义：RuntimeError
                # fail-loud，零标签零视频零卡片，不回填任何 *_message_id_v1/v2。
                fresh = self.store.get_pending(thread_id, gate)
                if fresh is None or fresh["status"] != "PENDING":
                    # R-LOW-3 NIT：raise 前补 warning（与观察窗 gone 路径同形）。
                    self._log.warning(
                        "Gate%s(diff) 首次发送前 fresh 复查：占位行已终结"
                        "（status=%s），拒绝发送孤儿媒体/孤儿卡 thread=%s",
                        gate, fresh["status"] if fresh else None, thread_id)
                    raise RuntimeError(
                        "Gate2(diff) 占位行在首次发送前已终结（status="
                        f"{fresh['status'] if fresh else None}），疑似他方已裁决；"
                        "拒绝发送孤儿媒体/孤儿卡，请走 resume 读终态 "
                        f"thread={thread_id}")
                stale = None if claimed else fresh
                v1_existing = stale["media_message_id_v1"] if stale else None
                v2_existing = stale["media_message_id_v2"] if stale else None
                # 严格 v1→v2 串行：标签/视频成对，缺哪列补哪列，顺序不可调换。
                v1_id, fb1, detail1, _ = self._send_diff_column(
                    thread_id, "v1", self._DIFF_LABEL_V1, v1_existing,
                    v1_video, trim, duration_ms=duration_ms,
                    cover_png=cover_png)
                # R-LOW-3：V2 标签/视频发送前 fresh 复查。翻转落在 V1 上传窗内
                # 时，V1 的 attach 0 行已告警（v1 列保持 NULL），此处 raise 保证
                # V2 标签/视频与卡片不发。
                self._fresh_pending_or_abort(
                    thread_id, gate, phase="V2 媒体发送",
                    orphan_hint="V1 列视频可能已在上传窗内发出（孤儿，见 0 行回填告警）")
                v2_id, fb2, detail2, step2 = self._send_diff_column(
                    thread_id, "v2", self._DIFF_LABEL_V2, v2_existing,
                    v2_video, trim, duration_ms=duration_ms,
                    cover_png=cover_png)
                video_fallback = fb1 or fb2
                # 卡片注记以 V2 本次视频的实际阶梯为准（复用列 step=None）。
                trim_note = self._build_trim_note(trim, step2)
                # R-LOW-3：卡片发送前最后一次 fresh 复查；翻转落在 V2 上传窗内
                # 时两列 attach 0 行已告警，此处保证卡片不发。
                self._fresh_pending_or_abort(
                    thread_id, gate, phase="卡片发送",
                    orphan_hint="V1/V2 视频可能已在上传窗内发出（孤儿，见 0 行回填告警）")
                sent = self._send_interactive(
                    self.owner_open_id,
                    build_gate2_card_diff(task_title, metrics_diff,
                                          pareto_verdict,
                                          video_fallback=video_fallback,
                                          explanation=explanation,
                                          trim_note=trim_note),
                    build_gate2_card_diff_v2(task_title, metrics_diff,
                                             pareto_verdict,
                                             video_fallback=video_fallback,
                                             explanation=explanation,
                                             trim_note=trim_note),
                    form_required=True,
                )
                # R-LOW-3：回填 0 行=卡片在发送秒级窗内变成孤儿，封装已告警；
                # fail-loud 交 resume 读终态，不再进入等待。
                attached = self._attach_ids_or_warn(
                    thread_id, gate, sent_kind="Gate2(diff) 卡片",
                    card_message_id=sent["message_id"],
                    open_chat_id=sent["chat_id"],
                    media_message_id_v1=v1_id, media_message_id_v2=v2_id)
                if attached == 0:
                    raise RuntimeError(
                        "Gate2(diff) 卡片刚发出但占位行已终结（孤儿卡片，回填 0 行，"
                        "已告警留痕），请走 resume 读终态 "
                        f"thread={thread_id}")
                self._log.info(
                    "Gate2(diff) 卡片已发出 thread=%s schema=%s v1=%s v2=%s",
                    thread_id, sent["schema"], detail1, detail2)
        self._event_for(thread_id, gate)
        return self._await_decision(thread_id, gate, max_wait=max_wait)

    # ---------- Phase 2 Task6：ascent Gate1 升阶假设卡 ----------

    def request_gate1_ascent_approval(
        self,
        thread_id: str,
        task_title: str,
        proposals: list[dict[str, Any]],
        v1_summary: "str | None" = None,
        *,
        max_wait: "float | None" = None,
    ) -> dict:
        """Task6 ascent Gate1：升阶假设卡（V1 冻结指标 + ascent_claims 对照）。

        等待/重挂/claim 语义与标准 Gate1 完全一致（upsert 占位、不重发、打回
        归档重开），仅卡片内容换成 build_gate1_card_ascent。No Fake Approval：
        本方法阻塞等待真人点击，任何分支都没有硬编码 approve。
        """
        gate = 1
        row = self.store.get_pending(thread_id, gate)
        if row is not None and row["status"] == "PENDING" and row["card_message_id"]:
            # 重启重挂：PENDING 行且卡已发，不重发（与标准 Gate1 同）。
            self._log.info(
                "Gate1(ascent) 重启重挂 thread=%s message_id=%s（不重发卡片）",
                thread_id, row["card_message_id"])
        else:
            # Task7 D10：上一轮打回先归档，保证新卡片新点击。
            self._reopen_round(thread_id, gate)
            sent = self._send_interactive(
                self.owner_open_id,
                build_gate1_card_ascent_v1(task_title, proposals, v1_summary),
                build_gate1_card_ascent(task_title, proposals, v1_summary),
                form_required=True,
            )
            self.store.upsert_pending(
                thread_id, gate,
                card_message_id=sent["message_id"],
                open_chat_id=sent["chat_id"],
                task_title=task_title)
            # 极端竞态保底回填（R-LOW-3：0 行=卡片刚发出行即终结，孤儿告警）。
            self._attach_ids_or_warn(
                thread_id, gate, sent_kind="Gate1(ascent) 卡片",
                card_message_id=sent["message_id"],
                open_chat_id=sent["chat_id"])
            self._log.info("Gate1(ascent) 卡片已发出 thread=%s schema=%s",
                           thread_id, sent["schema"])
        self._event_for(thread_id, gate)
        return self._await_decision(thread_id, gate, max_wait=max_wait)

    # ---------- Phase 2 Task6：四媒体 Diff Gate2（V1/V2 各两 split） ----------

    # 列标签行只在本次实际推送该列视频时发送，从不落 store（重挂不重发标签）。
    # 顺序钉死：V1 split1 → V1 split2 → V2 split1 → V2 split2；与
    # cards._DIFF4_INTRO 同序同名（task6 出生测试对质）。
    _DIFF4_LABEL_V1A = "① V1 历史视频 A（冻结档案 split1：normal vs ice_nominal）"
    _DIFF4_LABEL_V1B = "② V1 历史视频 B（冻结档案 split2：ice_nominal vs ice_dr）"
    _DIFF4_LABEL_V2A = "③ V2 本次视频 A（候选实证 split1：normal vs ice_nominal）"
    _DIFF4_LABEL_V2B = "④ V2 本次视频 B（候选实证 split2：ice_nominal vs ice_dr）"

    def request_gate2_diff4_approval(
        self,
        thread_id: str,
        task_title: str,
        v1a_video: str,
        v1b_video: str,
        v2a_video: str,
        v2b_video: str,
        metrics_diff: list[dict[str, Any]],
        pareto_verdict: "str | dict[str, Any]",
        *,
        duration_ms: "int | None" = None,
        cover_png: "str | None" = None,
        trim: "dict | None" = None,
        explanation: "dict[str, str] | None" = None,
        max_wait: "float | None" = None,
    ) -> dict:
        """Task6 四媒体 Diff Gate2：V1/V2 各两张 split 严格顺序推送 + 一张 Δ 卡。

        顺序（claim 占位，LOW-6 崩溃接管，R-LOW-3 每副作用间 fresh 复查）：
        claim → ①标签+V1 split1（回填 v1a）→ ②标签+V1 split2（回填 v1b）
        → ③标签+V2 split1（回填 v2a）→ ④标签+V2 split2（回填 v2b）→ Diff 卡
        → 阻塞等待人类最终裁决。任一已送达列不重发、标签不重发；缺路径且无
        已送达 id 即 fail-closed（禁止用少媒体冒充四媒体看板）。claim 冲突的
        wait/take/gone 三分与双媒体路径同构（观察列含本路径四列）。
        metrics_diff/pareto_verdict 在任何副作用之前先过卡片构造校验（纯函数）。
        """
        gate = 2
        trim = validate_trim(trim)
        # 预检：非法 Δ 行/裁决值在占位与推送之前抛错（构造为纯函数，零副作用）。
        build_gate2_card_diff4_v2(task_title, metrics_diff, pareto_verdict,
                                  explanation=explanation)
        columns = (
            ("v1a", self._DIFF4_LABEL_V1A, v1a_video, "V1 split1 媒体发送"),
            ("v1b", self._DIFF4_LABEL_V1B, v1b_video, "V1 split2 媒体发送"),
            ("v2a", self._DIFF4_LABEL_V2A, v2a_video, "V2 split1 媒体发送"),
            ("v2b", self._DIFF4_LABEL_V2B, v2b_video, "V2 split2 媒体发送"),
        )
        row = self.store.get_pending(thread_id, gate)
        if row is not None and row["status"] == "PENDING" and row["card_message_id"]:
            self._log.info(
                "Gate2(diff4) 重启重挂 thread=%s message_id=%s（不重发卡片）",
                thread_id, row["card_message_id"])
        else:
            self._reopen_round(thread_id, gate)
            claimed = self.store.claim_pending(thread_id, gate,
                                                task_title=task_title)
            other_card_ready = False
            if not claimed:
                existing = self.store.get_pending(thread_id, gate)
                if existing is not None and existing["status"] == "PENDING" \
                        and existing["card_message_id"]:
                    other_card_ready = True
                    self._log.warning(
                        "Gate2(diff4) 占位已被他方持有且卡片已发，本次不重复推视频 "
                        "thread=%s card=%s", thread_id,
                        existing["card_message_id"])
                else:
                    # QA-2 MEDIUM：无 card 的 claim 失败三分裁决（观察列含 v1a/b、
                    # v2a/b：任一已回填=部分送达的崩溃现场，立即接管缺啥补啥）。
                    claim_action = self._claim_loss_action(thread_id, gate)
                    if claim_action == "gone":
                        raise RuntimeError(
                            "Gate2(diff4) 占位行在 claim 后消失，疑似他方已裁决"
                            "归档；拒绝发送孤儿媒体/孤儿卡，请走 resume 读终态 "
                            f"thread={thread_id}")
                    if claim_action == "wait":
                        other_card_ready = True
            if not other_card_ready:
                # R-LOW-2：首个副作用前 fresh 重读（claim/观察快照之后的 TOCTOU）。
                fresh = self.store.get_pending(thread_id, gate)
                if fresh is None or fresh["status"] != "PENDING":
                    self._log.warning(
                        "Gate%s(diff4) 首次发送前 fresh 复查：占位行已终结"
                        "（status=%s），拒绝发送孤儿媒体/孤儿卡 thread=%s",
                        gate, fresh["status"] if fresh else None, thread_id)
                    raise RuntimeError(
                        "Gate2(diff4) 占位行在首次发送前已终结（status="
                        f"{fresh['status'] if fresh else None}），疑似他方已裁决；"
                        "拒绝发送孤儿媒体/孤儿卡，请走 resume 读终态 "
                        f"thread={thread_id}")
                stale = None if claimed else fresh
                sent_cols: dict[str, "str | None"] = {}
                video_fallback = False
                last_step = None
                details: list[str] = []
                for column, label, video_path, phase in columns:
                    # R-LOW-3：每列标签/视频（一个发送序列）前都 fresh 复查，
                    # 闭合上一列上传窗内的状态翻转。
                    self._fresh_pending_or_abort(
                        thread_id, gate, phase=phase,
                        orphan_hint="此前列视频可能已在上传窗内发出（孤儿，见 0 行回填告警）")
                    existing_id = stale[column] if stale else None
                    cid, fb, detail, step = self._send_diff_column(
                        thread_id, column, label, existing_id,
                        video_path, trim, duration_ms=duration_ms,
                        cover_png=cover_png)
                    sent_cols[column] = cid
                    video_fallback = video_fallback or fb
                    if step is not None:
                        last_step = step
                    details.append(f"{column}={detail}")
                trim_note = self._build_trim_note(trim, last_step)
                # 卡片（序列最后一个副作用）前最后一次 fresh 复查。
                self._fresh_pending_or_abort(
                    thread_id, gate, phase="卡片发送",
                    orphan_hint="四列视频可能已在上传窗内发出（孤儿，见 0 行回填告警）")
                sent = self._send_interactive(
                    self.owner_open_id,
                    build_gate2_card_diff4(task_title, metrics_diff,
                                           pareto_verdict,
                                           video_fallback=video_fallback,
                                           explanation=explanation,
                                           trim_note=trim_note),
                    build_gate2_card_diff4_v2(task_title, metrics_diff,
                                              pareto_verdict,
                                              video_fallback=video_fallback,
                                              explanation=explanation,
                                              trim_note=trim_note),
                    form_required=True,
                )
                attached = self._attach_ids_or_warn(
                    thread_id, gate, sent_kind="Gate2(diff4) 卡片",
                    card_message_id=sent["message_id"],
                    open_chat_id=sent["chat_id"],
                    media_message_id_v1a=sent_cols["v1a"],
                    media_message_id_v1b=sent_cols["v1b"],
                    media_message_id_v2a=sent_cols["v2a"],
                    media_message_id_v2b=sent_cols["v2b"])
                if attached == 0:
                    raise RuntimeError(
                        "Gate2(diff4) 卡片刚发出但占位行已终结（孤儿卡片，回填 0 行，"
                        "已告警留痕），请走 resume 读终态 "
                        f"thread={thread_id}")
                self._log.info(
                    "Gate2(diff4) 卡片已发出 thread=%s schema=%s %s",
                    thread_id, sent["schema"], " ".join(details))
        self._event_for(thread_id, gate)
        return self._await_decision(thread_id, gate, max_wait=max_wait)

    def close(self) -> None:
        self.stop_ws()
        self.store.close()


def get_gateway(app_ctx: dict[str, Any]) -> FeishuGateway:
    """显式工厂（基础计划 L328 模块级单例不利重启与测试；计划 §1.1/§2 Task1 改为本函数）。

    app_ctx 必含：app_id / app_secret / owner_open_id；
    可选：db_path / evidence_dir / run_dir / start_ws / on_p2p_text / on_card_action。
    """
    required = ("app_id", "app_secret", "owner_open_id")
    missing = [k for k in required if not app_ctx.get(k)]
    if missing:
        raise ValueError(f"app_ctx 缺少必填项: {missing}")
    return FeishuGateway(
        app_ctx["app_id"], app_ctx["app_secret"], app_ctx["owner_open_id"],
        db_path=app_ctx.get("db_path"),
        evidence_dir=app_ctx.get("evidence_dir"),
        run_dir=app_ctx.get("run_dir"),
        start_ws=app_ctx.get("start_ws", True),
        on_p2p_text=app_ctx.get("on_p2p_text"),
        on_card_action=app_ctx.get("on_card_action"),
        require_approval_notes=app_ctx.get("require_approval_notes", False),
    )


# ---------- Phase 2 Task3：run_mode 纯选择器（真实图缝 Task6 才接） ----------
# run_mode 字面量必须与 orchestrator.graph.ascent.STANDARD/ASCENT 同值；依赖方向
# 是 graph → feishu_gateway（见 graph/__init__.py），client 禁止反向 import graph
# 形成环，故在此钉死同值常量，漂移由 tests/milestone2/test_task3_diff_cards.py
# 的选择器测试对质。
RUN_MODE_STANDARD = "standard"
RUN_MODE_ASCENT = "ascent"


def _run_mode(state: Any) -> str:
    if not isinstance(state, dict):
        return RUN_MODE_STANDARD
    return state.get("run_mode") or RUN_MODE_STANDARD


def select_gate1_card_view(state: Any) -> str:
    """按 state.run_mode 选 Gate1 卡视图：ascent→'ascent' 升阶假设卡，余标准。"""
    return ("ascent" if _run_mode(state) == RUN_MODE_ASCENT
            else RUN_MODE_STANDARD)


def select_gate2_requestor(state: Any) -> str:
    """按 state.run_mode 选 Gate2 请求器：ascent→'diff' 双媒体，余标准单视频。"""
    return ("diff" if _run_mode(state) == RUN_MODE_ASCENT
            else RUN_MODE_STANDARD)


def gateway_from_env(**overrides: Any) -> FeishuGateway:
    """cli/main 入口专用：从 FEISHU_* 环境变量构造；缺失即报错（禁止默认批准兜底）。"""
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    owner = os.environ.get("FEISHU_RECEIVE_ID", "")
    if not (app_id and app_secret and owner):
        raise RuntimeError(
            "FEISHU_APP_ID/FEISHU_APP_SECRET/FEISHU_RECEIVE_ID 未配置全；"
            "无 --cli 旁路时禁止启动（不存在默认批准）")
    overrides.setdefault("start_ws", True)
    return get_gateway({"app_id": app_id, "app_secret": app_secret,
                        "owner_open_id": owner, **overrides})
