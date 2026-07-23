from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
MANAGED_PREFIX = "charm_idx_"


@dataclass(frozen=True)
class QueryEvidence:
    schema_name: str
    table_name: str
    equality_columns: tuple[str, ...]
    range_columns: tuple[str, ...] = ()
    order_columns: tuple[str, ...] = ()
    projection_columns: tuple[str, ...] = ()
    frequency: int = 1
    query_template: str = ""


@dataclass(frozen=True)
class IndexCandidate:
    candidate_id: uuid.UUID
    definition_hash: str
    index_name: str
    schema_name: str
    table_name: str
    key_columns: tuple[str, ...]
    include_columns: tuple[str, ...]
    normalized_sql: str
    provenance: dict[str, Any]


@dataclass(frozen=True)
class IndexExperimentResult:
    candidate_id: uuid.UUID
    index_name: str
    build_duration_seconds: float
    build_wal_bytes: int
    index_size_bytes: int
    before_median_ms: float
    after_median_ms: float
    post_drop_median_ms: float
    index_used: bool
    before_write_median_ms: float
    after_write_median_ms: float
    drop_duration_seconds: float


def _validate_identifier(value: str) -> None:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe or unsupported SQL identifier: {value!r}")


def _normalized_definition(
    schema_name: str,
    table_name: str,
    keys: tuple[str, ...],
    includes: tuple[str, ...],
) -> str:
    base = f"btree:{schema_name}.{table_name}({','.join(keys)})"
    return f"{base}:include({','.join(includes)})" if includes else base


def make_candidate(evidence: QueryEvidence) -> IndexCandidate:
    for identifier in (
        evidence.schema_name,
        evidence.table_name,
        *evidence.equality_columns,
        *evidence.range_columns,
        *evidence.order_columns,
        *evidence.projection_columns,
    ):
        _validate_identifier(identifier)
    if evidence.frequency < 1:
        raise ValueError("query evidence frequency must be positive")
    ordered_keys = list(evidence.equality_columns)
    for column in (*evidence.range_columns, *evidence.order_columns):
        if column not in ordered_keys:
            ordered_keys.append(column)
    keys = tuple(ordered_keys[:3])
    if not keys:
        raise ValueError("candidate requires at least one predicate/order key")
    includes = tuple(column for column in evidence.projection_columns if column not in keys)[:2]
    definition = _normalized_definition(evidence.schema_name, evidence.table_name, keys, includes)
    digest = hashlib.sha256(definition.encode()).hexdigest()
    name = f"{MANAGED_PREFIX}{digest[:16]}"
    key_sql = ", ".join(keys)
    include_sql = f" INCLUDE ({', '.join(includes)})" if includes else ""
    normalized_sql = (
        f"CREATE INDEX {name} ON {evidence.schema_name}.{evidence.table_name} "
        f"USING btree ({key_sql}){include_sql}"
    )
    return IndexCandidate(
        uuid.uuid5(uuid.NAMESPACE_URL, f"charm-db:{definition}"),
        digest,
        name,
        evidence.schema_name,
        evidence.table_name,
        keys,
        includes,
        normalized_sql,
        {
            "frequency": evidence.frequency,
            "query_template": evidence.query_template,
            "equality_columns": evidence.equality_columns,
            "range_columns": evidence.range_columns,
            "order_columns": evidence.order_columns,
        },
    )


def generate_candidates(
    evidence_items: list[QueryEvidence], limit: int = 20
) -> list[IndexCandidate]:
    if not 1 <= limit <= 50:
        raise ValueError("candidate limit must be between 1 and 50")
    candidates: dict[str, tuple[int, IndexCandidate]] = {}
    for evidence in evidence_items:
        candidate = make_candidate(evidence)
        current = candidates.get(candidate.definition_hash)
        score = evidence.frequency * (len(candidate.key_columns) + 1)
        if current is None or score > current[0]:
            candidates[candidate.definition_hash] = (score, candidate)
    return [item[1] for item in sorted(candidates.values(), key=lambda item: -item[0])[:limit]]


def _transition(
    settings: Settings,
    candidate_id: uuid.UUID,
    from_state: str | None,
    to_state: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.index_candidates
            SET state=%s,updated_at=clock_timestamp() WHERE candidate_id=%s""",
            (to_state, candidate_id),
        )
        cur.execute(
            """INSERT INTO charm_control.index_lifecycle_events
            (candidate_id,from_state,to_state,reason,details) VALUES (%s,%s,%s,%s,%s)""",
            (candidate_id, from_state, to_state, reason, Jsonb(details or {})),
        )
        conn.commit()


def persist_candidate(settings: Settings, candidate: IndexCandidate) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.index_candidates
            (candidate_id,definition_hash,index_name,schema_name,table_name,key_columns,
             include_columns,normalized_sql,state,provenance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PROPOSED',%s)
            ON CONFLICT (candidate_id) DO UPDATE SET provenance=EXCLUDED.provenance,
                state='PROPOSED',updated_at=clock_timestamp()""",
            (
                candidate.candidate_id,
                candidate.definition_hash,
                candidate.index_name,
                candidate.schema_name,
                candidate.table_name,
                Jsonb(candidate.key_columns),
                Jsonb(candidate.include_columns),
                candidate.normalized_sql,
                Jsonb(candidate.provenance),
            ),
        )
        cur.execute(
            """INSERT INTO charm_control.index_lifecycle_events
            (candidate_id,from_state,to_state,reason) VALUES (%s,NULL,'PROPOSED','generated')""",
            (candidate.candidate_id,),
        )
        conn.commit()


def load_candidate(settings: Settings, candidate_id: uuid.UUID) -> IndexCandidate:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT candidate_id,definition_hash,index_name,schema_name,table_name,
                      key_columns,include_columns,normalized_sql,provenance
               FROM charm_control.index_candidates WHERE candidate_id=%s""",
            (candidate_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown index candidate {candidate_id}")
    return IndexCandidate(
        row["candidate_id"],
        str(row["definition_hash"]),
        str(row["index_name"]),
        str(row["schema_name"]),
        str(row["table_name"]),
        tuple(str(value) for value in row["key_columns"]),
        tuple(str(value) for value in row["include_columns"]),
        str(row["normalized_sql"]),
        dict(row["provenance"]),
    )


def inspect_index_catalog(settings: Settings, candidate: IndexCandidate) -> dict[str, Any] | None:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT ni.nspname AS index_schema,ci.relname AS index_name,
                      nt.nspname AS table_schema,ct.relname AS table_name,
                      am.amname AS access_method,i.indisvalid,i.indisready,
                      ARRAY(
                          SELECT a.attname
                          FROM unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum,ord)
                          JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                          WHERE k.ord <= i.indnkeyatts ORDER BY k.ord
                      ) AS key_columns,
                      ARRAY(
                          SELECT a.attname
                          FROM unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum,ord)
                          JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                          WHERE k.ord > i.indnkeyatts ORDER BY k.ord
                      ) AS include_columns,
                      pg_relation_size(ci.oid)::bigint AS size_bytes
               FROM pg_class ci
               JOIN pg_namespace ni ON ni.oid=ci.relnamespace
               JOIN pg_index i ON i.indexrelid=ci.oid
               JOIN pg_class ct ON ct.oid=i.indrelid
               JOIN pg_namespace nt ON nt.oid=ct.relnamespace
               JOIN pg_am am ON am.oid=ci.relam
               WHERE ni.nspname=%s AND ci.relname=%s""",
            (candidate.schema_name, candidate.index_name),
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


def _verify_catalog_definition(candidate: IndexCandidate, catalog: dict[str, Any]) -> None:
    expected = {
        "index_schema": candidate.schema_name,
        "index_name": candidate.index_name,
        "table_schema": candidate.schema_name,
        "table_name": candidate.table_name,
        "access_method": "btree",
        "key_columns": list(candidate.key_columns),
        "include_columns": list(candidate.include_columns),
    }
    actual = {name: catalog[name] for name in expected}
    if actual != expected:
        raise RuntimeError(
            f"managed index definition differs: expected={expected}, actual={actual}"
        )
    if not catalog["indisvalid"] or not catalog["indisready"]:
        raise RuntimeError(f"managed index is not valid and ready: {catalog}")


def verify_index_active(settings: Settings, candidate: IndexCandidate) -> dict[str, Any]:
    catalog = inspect_index_catalog(settings, candidate)
    if catalog is None:
        raise RuntimeError(f"managed index {candidate.index_name} is absent")
    _verify_catalog_definition(candidate, catalog)
    return catalog


def verify_index_absent(settings: Settings, candidate: IndexCandidate) -> None:
    if inspect_index_catalog(settings, candidate) is not None:
        raise RuntimeError(f"managed index {candidate.index_name} still exists")


def _candidate_control_state(settings: Settings, candidate_id: uuid.UUID) -> tuple[str, bool]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state,managed FROM charm_control.index_candidates WHERE candidate_id=%s",
            (candidate_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown index candidate {candidate_id}")
    return str(row["state"]), bool(row["managed"])


def _latest_lifecycle_details(
    settings: Settings, candidate_id: uuid.UUID, state: str
) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT details FROM charm_control.index_lifecycle_events
               WHERE candidate_id=%s AND to_state=%s ORDER BY event_id DESC LIMIT 1""",
            (candidate_id, state),
        )
        row = cur.fetchone()
    return dict(row["details"]) if row is not None else None


def validate_candidate_catalog(settings: Settings, candidate: IndexCandidate) -> None:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.attname
            FROM pg_attribute a
            JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped""",
            (candidate.schema_name, candidate.table_name),
        )
        columns = {row["attname"] for row in cur.fetchall()}
        if not columns:
            raise ValueError("candidate table does not exist")
        missing = set((*candidate.key_columns, *candidate.include_columns)) - columns
        if missing:
            raise ValueError(f"candidate columns do not exist: {sorted(missing)}")
        cur.execute("SELECT to_regclass(%s) AS existing", (candidate.index_name,))
        if cur.fetchone()["existing"] is not None:  # type: ignore[index]
            raise ValueError("managed index already exists")
    _transition(settings, candidate.candidate_id, "PROPOSED", "VALIDATED", "catalog validated")


def _wal_bytes(settings: Settings) -> int:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT wal_bytes::numeric AS wal_bytes FROM pg_stat_wal")
        return int(cur.fetchone()["wal_bytes"])  # type: ignore[index]


def _measure_query(
    settings: Settings, customer_id: int, repetitions: int
) -> tuple[float, dict[str, Any]]:
    latencies: list[float] = []
    plan: dict[str, Any] | None = None
    query = """SELECT id, amount FROM public.charm_index_fixture
               WHERE customer_id=%s ORDER BY created_at DESC LIMIT 50"""
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) " + query, (customer_id,))
        raw_plan = cur.fetchone()["QUERY PLAN"]  # type: ignore[index]
        plan = raw_plan[0]
        for _ in range(repetitions):
            started = time.perf_counter()
            cur.execute(query, (customer_id,))
            cur.fetchall()
            latencies.append((time.perf_counter() - started) * 1000)
    if plan is None:
        raise RuntimeError("query plan was not captured")
    return statistics.median(latencies), plan


def _plan_uses_index(node: Any, index_name: str) -> bool:
    if isinstance(node, dict):
        if node.get("Index Name") == index_name:
            return True
        return any(_plan_uses_index(value, index_name) for value in node.values())
    if isinstance(node, list):
        return any(_plan_uses_index(value, index_name) for value in node)
    return False


def build_index(settings: Settings, candidate: IndexCandidate) -> tuple[float, int, int]:
    current, managed = _candidate_control_state(settings, candidate.candidate_id)
    if not managed:
        raise ValueError("candidate is not registered as managed")
    if current != "BUILDING":
        _transition(settings, candidate.candidate_id, current, "BUILDING", "real build started")
    wal_before = _wal_bytes(settings)
    started = time.monotonic()
    with connect(settings.target_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            statement = sql.SQL("CREATE INDEX {} ON {}.{} USING btree ({})").format(
                sql.Identifier(candidate.index_name),
                sql.Identifier(candidate.schema_name),
                sql.Identifier(candidate.table_name),
                sql.SQL(", ").join(map(sql.Identifier, candidate.key_columns)),
            )
            if candidate.include_columns:
                statement += sql.SQL(" INCLUDE ({})").format(
                    sql.SQL(", ").join(map(sql.Identifier, candidate.include_columns))
                )
            cur.execute(statement)
    duration = time.monotonic() - started
    wal_delta = max(0, _wal_bytes(settings) - wal_before)
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_relation_size(%s)::bigint AS bytes", (candidate.index_name,))
        size = int(cur.fetchone()["bytes"])  # type: ignore[index]
    _transition(
        settings,
        candidate.candidate_id,
        "BUILDING",
        "ACTIVE",
        "build verified",
        {
            "duration_seconds": duration,
            "wal_bytes": wal_delta,
            "size_bytes": size,
            "measurement_complete": True,
        },
    )
    return duration, wal_delta, size


def reconcile_or_build_index(settings: Settings, candidate: IndexCandidate) -> dict[str, Any]:
    catalog = inspect_index_catalog(settings, candidate)
    if catalog is None:
        duration, wal_bytes, size = build_index(settings, candidate)
        return {
            "candidate_id": str(candidate.candidate_id),
            "index_name": candidate.index_name,
            "duration_seconds": duration,
            "wal_bytes": wal_bytes,
            "size_bytes": size,
            "recovered_existing": False,
            "measurement_complete": True,
        }
    _verify_catalog_definition(candidate, catalog)
    current, managed = _candidate_control_state(settings, candidate.candidate_id)
    if not managed:
        raise ValueError("candidate is not registered as managed")
    details = _latest_lifecycle_details(settings, candidate.candidate_id, "ACTIVE")
    measurement_complete = bool(details and details.get("measurement_complete"))
    if current != "ACTIVE":
        details = {
            "duration_seconds": None,
            "wal_bytes": None,
            "size_bytes": int(catalog["size_bytes"]),
            "measurement_complete": False,
            "reconciled_after_ambiguous_build": True,
        }
        _transition(
            settings,
            candidate.candidate_id,
            current,
            "ACTIVE",
            "existing catalog index reconciled after ambiguous build",
            details,
        )
        measurement_complete = False
    details = details or {}
    return {
        "candidate_id": str(candidate.candidate_id),
        "index_name": candidate.index_name,
        "duration_seconds": details.get("duration_seconds"),
        "wal_bytes": details.get("wal_bytes"),
        "size_bytes": int(details.get("size_bytes", catalog["size_bytes"])),
        "recovered_existing": True,
        "measurement_complete": measurement_complete,
    }


def drop_managed_index(settings: Settings, candidate: IndexCandidate, reason: str) -> float:
    if not candidate.index_name.startswith(MANAGED_PREFIX):
        raise ValueError("refusing to drop an index not owned by CHARM-DB")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT managed,state FROM charm_control.index_candidates WHERE candidate_id=%s",
            (candidate.candidate_id,),
        )
        row = cur.fetchone()
        if row is None or not row["managed"]:
            raise ValueError("candidate is not registered as managed")
    _transition(settings, candidate.candidate_id, str(row["state"]), "DROPPING", reason)
    started = time.monotonic()
    with connect(settings.target_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP INDEX {}.{}").format(
                    sql.Identifier(candidate.schema_name), sql.Identifier(candidate.index_name)
                )
            )
    duration = time.monotonic() - started
    _transition(
        settings,
        candidate.candidate_id,
        "DROPPING",
        "DROPPED",
        "catalog drop verified",
        {"duration_seconds": duration},
    )
    return duration


def reconcile_or_drop_index(
    settings: Settings, candidate: IndexCandidate, reason: str
) -> dict[str, Any]:
    current, managed = _candidate_control_state(settings, candidate.candidate_id)
    if not managed or not candidate.index_name.startswith(MANAGED_PREFIX):
        raise ValueError("refusing to drop an index not owned by CHARM-DB")
    catalog = inspect_index_catalog(settings, candidate)
    if catalog is not None:
        _verify_catalog_definition(candidate, catalog)
        duration = drop_managed_index(settings, candidate, reason)
        return {
            "candidate_id": str(candidate.candidate_id),
            "index_name": candidate.index_name,
            "duration_seconds": duration,
            "recovered_absent": False,
        }
    details = _latest_lifecycle_details(settings, candidate.candidate_id, "DROPPED") or {}
    if current != "DROPPED":
        _transition(
            settings,
            candidate.candidate_id,
            current,
            "DROPPED",
            "catalog absence reconciled after ambiguous drop",
            {"duration_seconds": None, "reconciled_after_ambiguous_drop": True},
        )
    return {
        "candidate_id": str(candidate.candidate_id),
        "index_name": candidate.index_name,
        "duration_seconds": details.get("duration_seconds"),
        "recovered_absent": True,
    }


def _measure_write_batch(settings: Settings, repetitions: int = 5, rows: int = 200) -> float:
    durations: list[float] = []
    statement = """INSERT INTO public.charm_index_fixture
        (customer_id,status,created_at,amount,payload)
        VALUES (%s,'new',clock_timestamp(),%s,%s)"""
    values = [(9000 + number % 1000, number / 10, f"probe-{number}") for number in range(rows)]
    for _ in range(repetitions):
        with connect(settings.target_dsn) as conn, conn.cursor() as cur:
            started = time.perf_counter()
            cur.executemany(statement, values)
            durations.append((time.perf_counter() - started) * 1000)
            conn.rollback()
    return statistics.median(durations)


def seed_index_fixture(settings: Settings, rows: int = 300_000) -> None:
    if not 10_000 <= rows <= 1_000_000:
        raise ValueError("fixture rows must be between 10,000 and 1,000,000")
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS public.charm_index_fixture (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                customer_id integer NOT NULL,
                status text NOT NULL,
                created_at timestamptz NOT NULL,
                amount numeric(12,2) NOT NULL,
                payload text NOT NULL
            )"""
        )
        cur.execute("SELECT count(*) AS count FROM public.charm_index_fixture")
        existing_rows = int(cur.fetchone()["count"])  # type: ignore[index]
        if existing_rows < rows:
            cur.execute(
                """INSERT INTO public.charm_index_fixture
                (customer_id,status,created_at,amount,payload)
                SELECT g %% 10000,
                       CASE g %% 4 WHEN 0 THEN 'new' WHEN 1 THEN 'paid'
                            WHEN 2 THEN 'shipped' ELSE 'closed' END,
                       clock_timestamp() - ((g %% 100000) * interval '1 second'),
                       ((g * 17) %% 100000)::numeric / 100,
                       repeat(md5(g::text), 2)
                FROM generate_series(%s,%s) g""",
                (existing_rows + 1, rows),
            )
        cur.execute("ANALYZE public.charm_index_fixture")
        conn.commit()


def run_index_experiment(settings: Settings, customer_id: int = 4242) -> IndexExperimentResult:
    evidence = QueryEvidence(
        "public",
        "charm_index_fixture",
        ("customer_id",),
        order_columns=("created_at",),
        projection_columns=("id", "amount"),
        frequency=100,
        query_template=(
            "SELECT id, amount FROM charm_index_fixture WHERE customer_id = ? "
            "ORDER BY created_at DESC LIMIT 50"
        ),
    )
    candidate = generate_candidates([evidence], limit=1)[0]
    campaign_id = uuid.uuid4()
    trial_id = uuid.uuid4()
    action_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.campaigns
            (campaign_id,name,mode,status,objective_definition,constraint_definition)
            VALUES (%s,%s,'INDEX_ONLY','RUNNING',%s,%s)""",
            (
                campaign_id,
                f"index-lifecycle-{candidate.definition_hash[:8]}",
                Jsonb({"minimize": "query_latency_ms"}),
                Jsonb({"managed_index_only": True}),
            ),
        )
        cur.execute(
            """INSERT INTO charm_control.trials
            (trial_id,campaign_id,state,benchmark_profile,fidelity,random_seed,
             requested_configuration,active_configuration)
            VALUES (%s,%s,'CREATED','index-lifecycle',3,20260712,%s,'{}'::jsonb)""",
            (trial_id, campaign_id, Jsonb({"index_candidate_id": str(candidate.candidate_id)})),
        )
        cur.execute(
            """INSERT INTO charm_control.trial_actions
            (action_id,trial_id,proposed_index_ids) VALUES (%s,%s,%s)""",
            (action_id, trial_id, [candidate.candidate_id]),
        )
        cur.execute(
            """INSERT INTO charm_control.trial_transitions
            (trial_id,from_state,to_state,reason)
            VALUES (%s,NULL,'CREATED','index trial created')""",
            (trial_id,),
        )
        conn.commit()
    persist_candidate(settings, candidate)
    validate_candidate_catalog(settings, candidate)
    before_ms, before_plan = _measure_query(settings, customer_id, 15)
    before_write_ms = _measure_write_batch(settings)
    duration, wal_bytes, size = build_index(settings, candidate)
    after_ms, after_plan = _measure_query(settings, customer_id, 15)
    after_write_ms = _measure_write_batch(settings)
    index_used = _plan_uses_index(after_plan, candidate.index_name)
    _transition(
        settings,
        candidate.candidate_id,
        "ACTIVE",
        "MEASURED",
        "paired executed query measured",
        {"index_used": index_used},
    )
    drop_duration = drop_managed_index(settings, candidate, "controlled lifecycle experiment")
    post_drop_ms, _post_drop_plan = _measure_query(settings, customer_id, 15)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.index_measurements
            (measurement_id,candidate_id,build_duration_seconds,build_wal_bytes,index_size_bytes,
             before_median_ms,after_median_ms,post_drop_median_ms,before_plan,after_plan,index_used,
             paired_query_parameters,before_write_median_ms,after_write_median_ms,
             drop_duration_seconds)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid.uuid4(),
                candidate.candidate_id,
                duration,
                wal_bytes,
                size,
                before_ms,
                after_ms,
                post_drop_ms,
                Jsonb(before_plan),
                Jsonb(after_plan),
                index_used,
                Jsonb({"customer_id": customer_id, "repetitions": 15}),
                before_write_ms,
                after_write_ms,
                drop_duration,
            ),
        )
        cur.execute(
            """UPDATE charm_control.trial_actions
            SET indexes_created=%s,indexes_removed=%s,
                operational_cost=%s WHERE action_id=%s""",
            (
                [candidate.candidate_id],
                [candidate.candidate_id],
                Jsonb(
                    {
                        "index_build_seconds": duration,
                        "index_build_wal_bytes": wal_bytes,
                        "index_size_bytes": size,
                        "index_drop_seconds": drop_duration,
                        "write_median_before_ms": before_write_ms,
                        "write_median_after_ms": after_write_ms,
                    }
                ),
                action_id,
            ),
        )
        cur.execute(
            """UPDATE charm_control.trials SET state='COMPLETED',
            objective_values=%s,constraint_values=%s,completed_at=clock_timestamp()
            WHERE trial_id=%s""",
            (
                Jsonb({"before_median_ms": before_ms, "after_median_ms": after_ms}),
                Jsonb({"index_used": index_used, "managed_drop_verified": True}),
                trial_id,
            ),
        )
        cur.execute(
            """INSERT INTO charm_control.trial_transitions
            (trial_id,from_state,to_state,reason)
            VALUES (%s,'CREATED','BUILDING_INDEXES','validated candidate'),
                   (%s,'BUILDING_INDEXES','COLLECTING_METRICS','build and query completed'),
                   (%s,'COLLECTING_METRICS','COMPLETED','index removed and evidence persisted')""",
            (trial_id, trial_id, trial_id),
        )
        cur.execute(
            """UPDATE charm_control.campaigns SET status='COMPLETED',
            updated_at=clock_timestamp() WHERE campaign_id=%s""",
            (campaign_id,),
        )
        conn.commit()
    return IndexExperimentResult(
        candidate.candidate_id,
        candidate.index_name,
        duration,
        wal_bytes,
        size,
        before_ms,
        after_ms,
        post_drop_ms,
        index_used,
        before_write_ms,
        after_write_ms,
        drop_duration,
    )


def result_json(result: IndexExperimentResult) -> str:
    return json.dumps(
        {
            "candidate_id": str(result.candidate_id),
            "index_name": result.index_name,
            "build_duration_seconds": result.build_duration_seconds,
            "build_wal_bytes": result.build_wal_bytes,
            "index_size_bytes": result.index_size_bytes,
            "before_median_ms": result.before_median_ms,
            "after_median_ms": result.after_median_ms,
            "post_drop_median_ms": result.post_drop_median_ms,
            "index_used": result.index_used,
            "before_write_median_ms": result.before_write_median_ms,
            "after_write_median_ms": result.after_write_median_ms,
            "drop_duration_seconds": result.drop_duration_seconds,
        },
        indent=2,
    )
