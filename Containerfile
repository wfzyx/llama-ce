# syntax=docker/dockerfile:1
ARG BACKEND=cpu
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:12.6.3-devel-ubuntu24.04
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:12.6.3-runtime-ubuntu24.04
ARG ROCM_DEVEL_IMAGE=docker.io/rocm/dev-ubuntu-24.04:7.2.4-complete
ARG BASE_IMAGE=ubuntu:24.04

FROM ${BASE_IMAGE} AS base-cpu
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build git ca-certificates pkg-config libcurl4-openssl-dev patchelf \
    && rm -rf /var/lib/apt/lists/*
ENV CMAKE_BACKEND_FLAGS="-DGGML_NATIVE=OFF"

FROM ${BASE_IMAGE} AS base-vulkan
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build git ca-certificates pkg-config libcurl4-openssl-dev patchelf \
    libvulkan-dev glslc spirv-headers \
    && rm -rf /var/lib/apt/lists/*
ENV CMAKE_BACKEND_FLAGS="-DGGML_VULKAN=ON -DGGML_NATIVE=OFF"

FROM ${CUDA_DEVEL_IMAGE} AS base-cuda
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build git ca-certificates pkg-config libcurl4-openssl-dev patchelf \
    && rm -rf /var/lib/apt/lists/*
ENV CMAKE_BACKEND_FLAGS="-DGGML_CUDA=ON -DGGML_NATIVE=OFF"

FROM ${ROCM_DEVEL_IMAGE} AS base-rocm
ARG GPU_TARGETS=gfx1100
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake ninja-build git ca-certificates pkg-config libcurl4-openssl-dev patchelf \
    && rm -rf /var/lib/apt/lists/*
ENV CMAKE_BACKEND_FLAGS="-DGGML_HIP=ON -DGPU_TARGETS=${GPU_TARGETS} -DGGML_NATIVE=OFF"
FROM base-${BACKEND} AS builder
ARG CMAKE_EXTRA_FLAGS=""
WORKDIR /src
COPY . .
RUN cmake -B /build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
    -DCMAKE_INSTALL_RPATH='$ORIGIN' \
    -DCMAKE_EXE_LINKER_FLAGS="-Wl,-rpath,\$ORIGIN" \
    -DCMAKE_SHARED_LINKER_FLAGS="-Wl,-rpath,\$ORIGIN" \
    -DLLAMA_USE_PREBUILT_UI=OFF \
    -DLLAMA_BUILD_TESTS=OFF \
    ${CMAKE_BACKEND_FLAGS} \
    ${CMAKE_EXTRA_FLAGS} \
    && cmake --build /build --target llama-app llama-cli llama-server llama-bench -j"$(nproc)" \
    && if [ -d /opt/rocm/lib ]; then \
         cp -a /opt/rocm/lib/libamdhip64.so* /opt/rocm/lib/libhipblas.so* /opt/rocm/lib/librocblas.so* \
               /opt/rocm/lib/libhsa-runtime64.so* /opt/rocm/lib/libamd_comgr.so* \
               /opt/rocm/lib/libhiprtc.so* /opt/rocm/lib/librocprofiler-register.so* \
               /opt/rocm/lib/librocsolver.so* /opt/rocm/lib/libhipblaslt.so* \
               /opt/rocm/lib/libroctx64.so* /opt/rocm/lib/librocroller.so* /build/bin/ 2>/dev/null || true; \
         mkdir -p /build/bin/rocblas/library; \
         cp -a /opt/rocm/lib/rocblas/library/*gfx1100* /opt/rocm/lib/rocblas/library/TensileManifest* \
               /opt/rocm/lib/rocblas/library/Kernels.so* /build/bin/rocblas/library/ 2>/dev/null || true; \
       fi \
    && for f in /build/bin/*; do [ -f "$f" ] && [ ! -L "$f" ] && patchelf --set-rpath '$ORIGIN' "$f" 2>/dev/null || true; done
FROM scratch AS export
COPY --from=builder /build/bin/ /
