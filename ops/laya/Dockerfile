FROM public.ecr.aws/docker/library/python:3.11-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    USE_TF=0 \
    USE_TORCH=1 \
    TOKENIZERS_PARALLELISM=false \
    HF_HOME=/home/laya/.cache/huggingface \
    LD_LIBRARY_PATH=/usr/local/nvidia/lib64

RUN pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cu129
RUN pip install 'laya[serve]==0.4.4' transformers==4.57.6 && pip check
RUN groupadd --gid 1000 laya \
    && useradd --uid 1000 --gid 1000 --create-home laya \
    && mkdir -p /home/laya/.cache/huggingface /usr/local/nvidia/lib64 \
    && chown -R laya:laya /home/laya/.cache

COPY entrypoint.py /opt/laya/entrypoint.py
USER 1000:1000
WORKDIR /home/laya
ENTRYPOINT ["python", "/opt/laya/entrypoint.py"]
