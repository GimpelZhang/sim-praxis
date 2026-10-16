FROM nvidia/cuda:12.4.1-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 LANG=C.UTF-8

# 1) apt 换清华源（jammy）；兼容 sources.list 与 deb822 两种布局
RUN set -eux; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i -E 's@https?://(archive|security|ports).ubuntu.com/ubuntu@https://mirrors.tuna.tsinghua.edu.cn/ubuntu@g' /etc/apt/sources.list; \
    fi; \
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
      sed -i -E 's@https?://(archive|security).ubuntu.com/ubuntu@https://mirrors.tuna.tsinghua.edu.cn/ubuntu@g' /etc/apt/sources.list.d/ubuntu.sources; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config git curl wget ca-certificates gnupg \
      python3-dev python3-pip python3-setuptools \
      libegl1 libegl1-mesa-dev libgl1 libgl1-mesa-dev libgles2 libgles2-mesa-dev \
      libglvnd-dev libglu1-mesa-dev libglx0 \
      libosmesa6 libosmesa6-dev \
      libvulkan1 libvulkan-dev vulkan-tools \
      mesa-utils mesa-utils-extra x11-utils xvfb \
      ffmpeg \
      libxrandr2 libxrandr-dev libxinerama-dev libxcursor-dev libxi-dev libxext6 libx11-6 \
    && rm -rf /var/lib/apt/lists/*

# 2) pip 清华源（pypi.org 在本网络直连超时）
RUN python3 -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && python3 -m pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# 3) torch：先试官方 cu124 wheel 索引；失败回退清华 PyPI 的默认 manylinux wheel
#    （默认 wheel 自带 CUDA 运行时库，在 580 驱动上 torch.cuda.is_available() 同样成立）
RUN set -eux; \
    python3 -m pip install --no-cache-dir torch==2.4.1 torchvision==0.19.1 \
      --index-url https://download.pytorch.org/whl/cu124 \
    || python3 -m pip install --no-cache-dir torch==2.4.1 torchvision==0.19.1

# 4) 渲染探针依赖
# 锁内适配（2026-10-15 Task8 构建实测，规则8）：
# (a) BuildKit 默认 bridge 命名空间内 pip 取清华索引整体失败
#     （numpy==1.26.4 from versions: none），--network host 直连同机实测
#     /simple/ 200（1.3MB/23s）→ compose build.network=host；
# (b) 裸 ARG 不会成为 pip 环境变量，必须在 RUN 行内展开为命令前缀 env，
#     否则 PIP_DEFAULT_TIMEOUT/PIP_RETRIES 对 pip 不生效（pip 15s 默认超时
#     下大索引页反复中断）；
# (c) 代理（127.0.0.1:10900）长会话传输中途停滞，构建命令显式 unset 代理
#     走直连。以上只影响构建取包路径，不改镜像内容/不松 pin。
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=8
COPY requirements-container.txt /tmp/requirements-container.txt
RUN PIP_DEFAULT_TIMEOUT=$PIP_DEFAULT_TIMEOUT PIP_RETRIES=$PIP_RETRIES \
    python3 -m pip install --no-cache-dir -r /tmp/requirements-container.txt

# 4b) m-d：容器侧依赖一致性在构建期锁死——关键包精确版本断言，
#     防止容器内隐式升级（如 numpy 2.x）导致探针/torch 运行期才爆。
# Task8 D2（用户书面裁定 B′，2026-10-15）：torch 2.4.1 精确钉 nvrtc/nvjitlink
#     ==12.4.99 与 gs-madrona 0.0.10 要求 >=12.8 的元数据矛盾在任一方向都无法
#     让 pip check 归零；终态 12.9.86（双向运行时实测等价），构建期改白名单：
#     仅允许这两条已知 torch 冲突，任何其他冲突即失败。
COPY pip_check_whitelist.py /tmp/pip_check_whitelist.py
RUN python3 /tmp/pip_check_whitelist.py
# Task7：追加 mujoco/mani_skill/sapien 精确版本断言（探针 attempt2 钉死，只加不放）
RUN python3 -c "import numpy, imageio, OpenGL; \
    assert numpy.__version__ == '1.26.4', numpy.__version__; \
    assert imageio.__version__ == '2.36.1', imageio.__version__; \
    assert OpenGL.__version__ == '3.1.7', OpenGL.__version__; \
    print('container versions OK')"
RUN python3 -c "import mujoco, mani_skill, sapien, gymnasium; \
    assert mujoco.__version__ == '3.15.0', mujoco.__version__; \
    assert mani_skill.__version__ == '3.0.1', mani_skill.__version__; \
    assert sapien.__version__ == '3.0.3', sapien.__version__; \
    assert gymnasium.__version__ == '1.4.0', gymnasium.__version__; \
    print('container task7 versions OK')"
# Task8：genesis-world 1.3.3 闭包版本断言。锁内适配（2026-10-15 构建实测）：
# 构建期 RUN 无 GPU/显示设备，import genesis 会经 pyglet 触发
# "'NoneType' object has no attribute 'Window'"，故只用 importlib.metadata 断言；
# 真正 import genesis/gs_madrona/igl + 建场/接触/EGL 出帧由运行期探针 verify，
# 见 sandbox/probes/probe_genesis.py。
RUN python3 -c "import importlib.metadata as m; \
    assert m.version('genesis-world') == '1.3.3', m.version('genesis-world'); \
    assert m.version('gs-madrona') == '0.0.10'; \
    assert m.version('quadrants') == '1.3.0'; \
    assert m.version('numba') == '0.68.0'; \
    assert m.version('nvidia-cuda-nvrtc-cu12') == '12.9.86'; \
    assert m.version('nvidia-nvjitlink-cu12') == '12.9.86'; \
    assert m.version('pillow') == '11.3.0'; \
    assert m.version('pyglet') == '3.0.dev11'; \
    assert m.version('libigl') == '2.6.4.dev1'; \
    print('container task8 versions OK')"

# 5) NVIDIA 无头渲染声明 + 镜像/网络默认
# 注意：HF_TOKEN 绝不作为 --build-arg 传入、绝不写进镜像层（m11）；
#       构建期无需鉴权（权重在运行期由 compose 从宿主环境变量注入）。
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute,display \
    PYOPENGL_PLATFORM=egl \
    HF_ENDPOINT=https://hf-mirror.com
COPY entrypoint.sh /usr/local/bin/simpraxis-entrypoint.sh
RUN chmod +x /usr/local/bin/simpraxis-entrypoint.sh
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/simpraxis-entrypoint.sh"]
CMD ["sleep", "infinity"]
