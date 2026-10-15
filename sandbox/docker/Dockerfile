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
COPY requirements-container.txt /tmp/requirements-container.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements-container.txt

# 4b) m-d：容器侧依赖一致性在构建期锁死——pip check 零冲突 + 关键包精确版本断言，
#     防止容器内隐式升级（如 numpy 2.x）导致探针/torch 运行期才爆。
RUN python3 -m pip check
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
