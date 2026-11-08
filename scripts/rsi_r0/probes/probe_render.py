"""容器内 GPU/EGL 必要性探针：JSON 单行 stdout（运维侧，不进候选容器）。

判据（不许假通过）：
* gl_vendor 必须含 NVIDIA 且 gl_renderer 含 A800（GPU 车道）；
* 场景带光源+相机，渲染图必须非全黑且像素有方差（sum>0 且 std>0）；
* 任何异常原样回传，rc=2。
"""
import json, os, sys
os.environ.setdefault("MUJOCO_GL", "egl")
import numpy as np

SCENE = """
<mujoco>
  <visual><global offwidth="64" offheight="64"/></visual>
  <worldbody>
    <light pos="0 0 3" dir="0 0 -1"/>
    <camera name="cam" pos="0 -2 1" xyaxes="1 0 0 0 0 1"/>
    <body name="ball" pos="0 0 0.5">
      <freejoint/>
      <geom size="0.1" rgba="1 0 0 1"/>
    </body>
    <geom type="plane" size="2 2 0.1" rgba="0 1 0 1"/>
  </worldbody>
</mujoco>
"""

out = {"mujoco_gl": os.environ["MUJOCO_GL"],
       "egl_device_id": os.environ.get("MUJOCO_EGL_DEVICE_ID")}
try:
    import mujoco
    out["mujoco_version"] = mujoco.__version__
    m = mujoco.MjModel.from_xml_string(SCENE)
    d = mujoco.MjData(m)
    for _ in range(50):
        mujoco.mj_step(m, d)
    out["qpos_z"] = round(float(d.qpos[2]), 6)
    r = mujoco.Renderer(m, 64, 64)
    r.update_scene(d, camera="cam")
    img = r.render()
    # GL 字符串必须在 close 之前取（close 之后 context 已销毁，返回 None）
    import OpenGL.GL as gl
    out["gl_renderer"] = str(gl.glGetString(gl.GL_RENDERER))
    out["gl_vendor"] = str(gl.glGetString(gl.GL_VENDOR))
    out["gl_version"] = str(gl.glGetString(gl.GL_VERSION))
    r.close()
    out.update({"render_ok": True, "render_shape": list(img.shape),
                "pixel_sum": int(img.sum()), "pixel_std": float(np.std(img))})
    out["ok"] = (out["pixel_sum"] > 0 and out["pixel_std"] > 0.0)
    if not out["ok"]:
        out["reason"] = "render_all_black"
except BaseException as exc:  # noqa: BLE001
    out.update({"ok": False, "error_class": type(exc).__name__,
                "error": str(exc)[:400]})
print("PROBE_JSON " + json.dumps(out, ensure_ascii=False, sort_keys=True))
sys.exit(0 if out.get("ok") else 2)
