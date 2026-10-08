FROM postgres:16-alpine
COPY e2e/init.sql /docker-entrypoint-initdb.d/readonly.sql
