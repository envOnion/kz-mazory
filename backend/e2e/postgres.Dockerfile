ARG POSTGRES_BASE_IMAGE=postgres:16-alpine
FROM ${POSTGRES_BASE_IMAGE}
COPY e2e/init.sql /docker-entrypoint-initdb.d/readonly.sql
