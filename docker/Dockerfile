# Test image only: the Mac has no Python toolchain. PY=3.9 or PY=3.13.
ARG PY=3.13
FROM python:${PY}-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore PYTHONDONTWRITEBYTECODE=1
WORKDIR /repo/packages/python
