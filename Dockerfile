FROM buildpack-deps:bookworm-scm@sha256:5bac6b3020afcf0358de87990605d5dc4c3f060629381912266b9b9278375af3 AS upstream
WORKDIR /checkout
RUN git init -q && \
    git fetch --quiet --depth=1 https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa && \
    git checkout -q --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = eb11aec4be8cb2122afe713a4816662c3f0fb2fa && \
    mkdir /pinned && git archive HEAD | tar -x -C /pinned

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS indexed
COPY --from=upstream /pinned /opt/sqlglot
COPY scripts /app/scripts
COPY maps /app/maps
RUN python /app/scripts/build_map.py verify /opt/sqlglot /app/maps/index.zlib --observations /app/maps/observations.zlib

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY --from=indexed /opt/sqlglot /opt/sqlglot
COPY --from=indexed /app/maps /app/maps
COPY selector /app/selector
RUN python -m compileall -q /app/selector && chmod -R a+rX /app /opt/sqlglot
CMD ["python", "/app/selector/main.py"]
