# Add JudgmentLLM prerequisites without replacing the existing R CI image/tag.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY uv.tar.gz /tmp/judgmentllm-uv.tar.gz
COPY jsonlite.tar.gz /tmp/judgmentllm-jsonlite.tar.gz
RUN echo 'f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645  /tmp/judgmentllm-uv.tar.gz' | sha256sum -c - \
 && echo 'e947f4ce2c3a8194ae42c4015cdc557cec1c1cea06f85cd20da037aa93dfa5e4  /tmp/judgmentllm-jsonlite.tar.gz' | sha256sum -c - \
 && mkdir -p /opt/judgmentllm/tools /opt/judgmentllm/r-library \
 && tar -xzf /tmp/judgmentllm-uv.tar.gz -C /opt/judgmentllm/tools --strip-components=1 \
 && /opt/judgmentllm/tools/uv --version \
 && R CMD INSTALL --library=/opt/judgmentllm/r-library /tmp/judgmentllm-jsonlite.tar.gz \
 && rm /tmp/judgmentllm-uv.tar.gz /tmp/judgmentllm-jsonlite.tar.gz

ENV UV_PYTHON_INSTALL_DIR=/opt/judgmentllm/python \
    UV_PYTHON_INSTALL_BIN=0 \
    UV_PYTHON_INSTALL_REGISTRY=0 \
    R_LIBS_USER=/opt/judgmentllm/r-library
RUN /opt/judgmentllm/tools/uv python install --no-bin --no-registry 3.11.18 3.12.16

LABEL org.judgmentllm.local-ci.uv="0.13.6" \
      org.judgmentllm.local-ci.python="3.11.18,3.12.16" \
      org.judgmentllm.local-ci.jsonlite="2.0.0"
WORKDIR /state
