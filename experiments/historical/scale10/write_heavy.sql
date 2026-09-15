\set aid random(1, 1000000)
\set delta random(-100, 100)
UPDATE pgbench_accounts SET abalance = abalance + :delta WHERE aid = :aid;
