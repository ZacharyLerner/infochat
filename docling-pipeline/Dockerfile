# syntax=docker/dockerfile:1
# Docling -> Markdown converter for AWS Lambda (container image, deployed via ECR).
FROM public.ecr.aws/lambda/python:3.12

ARG DOCLING_MODELS_DIR=/opt/docling-models

# CPU-only torch keeps the image ~GBs smaller than the default CUDA wheels
# (Lambda has no GPU). Install it first so docling doesn't pull CUDA torch.
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt \
    # Some deps pull GUI opencv, which needs X11 libs absent from the Lambda base
    # image. Replace it with the headless build (same cv2 module, no X11).
    && pip uninstall -y opencv-python opencv-python-headless \
    && pip install --no-cache-dir opencv-python-headless

# Bake all model weights into the image so cold starts never hit the network.
RUN docling-tools models download -o ${DOCLING_MODELS_DIR} \
    && chmod -R a+rX ${DOCLING_MODELS_DIR}

# Runtime config. Lambda's filesystem is read-only except /tmp, so point any
# cache/home dirs there, and force offline mode so nothing tries to download.
ENV DOCLING_ARTIFACTS_PATH=${DOCLING_MODELS_DIR} \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/hf \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/mpl \
    PYTHONUNBUFFERED=1

COPY app/ ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
