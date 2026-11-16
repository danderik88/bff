# BFF slicer: headless OrcaSlicer CLI behind slicerd.py. x86_64 only (OrcaSlicer ships no Linux ARM build).
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git python3 libmspack0t64 libgtk-3-0t64 libwebkit2gtk-4.1-0 libgl1 libegl1 \
      libglu1-mesa libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libsoup-3.0-0 libosmesa6 \
      libavformat61 libavcodec61 libswscale8 libavutil59 libsm6 libice6 \
    && rm -rf /var/lib/apt/lists/*
ARG ORCA_VERSION=2.5.0
# AppImage extracted: no FUSE needed in a container.
RUN cd /opt && curl -fsSL -o orca.AppImage \
      "https://github.com/SoftFever/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage" \
    && chmod +x orca.AppImage && ./orca.AppImage --appimage-extract >/dev/null && mv squashfs-root orca \
    && rm -f orca.AppImage && chmod -R a+rX orca \
    && test "$(ldd /opt/orca/bin/orca-slicer | grep -c 'not found')" -eq 0
COPY tools/slicer/slicerd.py tools/slicer/flatten_profiles.py /opt/slicer/
# Official Bambu Lab profiles of the same OrcaSlicer tag, flattened to standalone JSON.
RUN git clone -q --depth 1 --branch "v${ORCA_VERSION}" --filter=blob:none --sparse \
      https://github.com/SoftFever/OrcaSlicer.git /tmp/orca-src \
    && cd /tmp/orca-src && git sparse-checkout set --no-cone /resources/profiles/BBL/ /resources/profiles/BBL.json \
         /resources/profiles/Sovol/ /resources/profiles/Sovol.json /resources/profiles/Custom/ /resources/profiles/Custom.json \
         /resources/profiles/OrcaFilamentLibrary/ /resources/profiles/OrcaFilamentLibrary.json \
    && python3 /opt/slicer/flatten_profiles.py resources/profiles /opt/slicer/profiles \
    && rm -rf /tmp/orca-src && chmod -R a+rX /opt/slicer \
    && useradd -r -m -d /home/slicer -s /usr/sbin/nologin slicer
USER slicer
ENV HOME=/home/slicer ORCA_BIN=/opt/orca/AppRun SLICER_PROFILES=/opt/slicer/profiles PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python3", "/opt/slicer/slicerd.py"]
