\set aid random(1, 1000000)
SELECT abalance FROM pgbench_accounts WHERE aid = :aid;
