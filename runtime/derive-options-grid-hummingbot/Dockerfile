# DERIVE_OPTIONS_ADAPTIVE_GRID_MANAGED
#
# Build a separate, reviewed bot image.  Never replace the upstream image
# tag: the digest and source hashes below are part of the contract.
FROM hummingbot/hummingbot@sha256:632d2b07aa156b761310f2f7258a78c9660a1c28b6df4b33874e09a0c7d06c85

COPY patch_derive_connector.py /opt/derive-options-grid-runtime/patch_derive_connector.py
COPY verify_runtime_contract.py /opt/derive-options-grid-runtime/verify_runtime_contract.py

WORKDIR /home/hummingbot
ENV PYTHONPATH="/home/hummingbot"
# This value is part of the authenticated runtime identity.  It must equal the
# immutable local tag used for every canary and controller deployment built
# from this Dockerfile; a mismatched tag fails the same-runtime preflight.
ENV DERIVE_OPTIONS_GRID_RUNTIME_IMAGE_REF="local/derive-options-grid-hummingbot:connector-v3-lifecycle"

RUN . /opt/conda/etc/profile.d/conda.sh \
    && conda activate hummingbot \
    && python /opt/derive-options-grid-runtime/patch_derive_connector.py \
    && python /opt/derive-options-grid-runtime/verify_runtime_contract.py
