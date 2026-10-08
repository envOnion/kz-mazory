CREATE USER e2e_analytics WITH PASSWORD 'isolated-readonly-fixture';
GRANT CONNECT ON DATABASE mazory_e2e TO e2e_analytics;
GRANT USAGE ON SCHEMA public TO e2e_analytics;
ALTER DEFAULT PRIVILEGES FOR ROLE mazory_e2e IN SCHEMA public GRANT SELECT ON TABLES TO e2e_analytics;
ALTER ROLE e2e_analytics SET default_transaction_read_only = on;
