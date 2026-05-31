#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Mock backend data for the incident-triage voice agent.

Pure standard-library data layer. No pipecat, no third-party imports — this
module is imported by both the bot tools and the test suite, and must stay
cloud-safe (no filesystem access required to serve repo content).

Three exported structures:

``ENGINEERS``
    The on-call directory, keyed by engineer id. Each entry carries enough to
    do follow-the-sun routing: an IANA ``timezone`` (resolved with stdlib
    ``zoneinfo``), local ``working_hours`` as a ``(start, end)`` 24h tuple, and
    the ``teams`` the engineer owns. Phones are E.164 placeholders.

``INCIDENTS``
    Four ground-truth incidents (``inc-1`` .. ``inc-4``). Each bundles the
    paging alert, deploy history, pre-correlated logs/metrics, the affected
    team, the ground-truth root cause + proposed remediation, and a ``fix``.
    ``fix`` is a staged patch (``before``/``after``) for the three code-fixable
    incidents and ``None`` for the ops-only cert incident (#3).

``REPO_FILES``
    Maps a repo-relative path to the full buggy file content, so a
    ``read_repo_file`` tool can serve source straight from data without
    touching the filesystem. INVARIANT: each ``fix["before"]`` appears verbatim
    inside ``REPO_FILES[fix["file"]]`` so a fix is applied by a plain string
    replace. The ``sample-service/`` git repo mirrors these files for
    ``get_deploy_history`` realism (deploy ids are real commits).

Keep this file in sync with ``docs/cekura-eval-plan.md`` §1a/§1b — the Cekura
metrics reference these exact ground-truth strings.
"""

# ---------------------------------------------------------------------------
# Engineer directory (cekura-eval-plan.md §1a)
#
# At DEMO_NOW = 2026-05-30 03:00 America/Los_Angeles the on-shift set
# (9 <= local_hour < 18) is {priya, lena, raj}; mei is the 18:00 edge (off),
# sam (06:00) and diego (03:00) are off-shift.
# ---------------------------------------------------------------------------
ENGINEERS = {
    "fardin": {
        "name": "Fardin",
        "phone": "+17653506634",  # the live on-call engineer dialed in the demo
        "location": "Indianapolis",
        "timezone": "America/Indiana/Indianapolis",
        "working_hours": (0, 24),  # currently on-call / always available
        "teams": ["payments"],
    },
    "priya": {
        "name": "Priya",
        "phone": "+442071234567",
        "location": "London",
        "timezone": "Europe/London",
        "working_hours": (9, 18),
        "teams": ["payments", "database"],
    },
    "lena": {
        "name": "Lena",
        "phone": "+4915123456789",
        "location": "Berlin",
        "timezone": "Europe/Berlin",
        "working_hours": (9, 18),
        "teams": ["networking", "infra"],
    },
    "raj": {
        "name": "Raj",
        "phone": "+919812345678",
        "location": "Bangalore",
        "timezone": "Asia/Kolkata",
        "working_hours": (9, 18),
        "teams": ["database", "backend"],
    },
    "mei": {
        "name": "Mei",
        "phone": "+6581234567",
        "location": "Singapore",
        "timezone": "Asia/Singapore",
        "working_hours": (9, 18),
        "teams": ["infra", "platform"],
    },
    "sam": {
        "name": "Sam",
        "phone": "+12125550198",
        "location": "New York",
        "timezone": "America/New_York",
        "working_hours": (9, 18),
        "teams": ["payments", "backend"],
    },
    "diego": {
        "name": "Diego",
        # The one engineer dialed live in the demo points at your own phone.
        "phone": "+14155550143",
        "location": "San Francisco",
        "timezone": "America/Los_Angeles",
        "working_hours": (9, 18),
        "teams": ["frontend", "web"],
    },
}

# ---------------------------------------------------------------------------
# Buggy source for the monitored service (mirrored in sample-service/).
#
# CRITICAL: each INCIDENTS[*]["fix"]["before"] string must appear verbatim
# below so apply_code_fix can patch by a simple str.replace.
# ---------------------------------------------------------------------------
REPO_FILES = {
    "app/db.py": '''\
"""Database engine + session setup for payments-api."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_URL = os.environ["PAYMENTS_DB_URL"]

# Connection pool for payments-db. Sized at deploy abc123 (14:30).
engine = create_engine(DB_URL)            # no pool cap (abc123)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_session():
    """Yield a scoped DB session, closing it on exit."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
''',
    "app/batch_jobs.py": '''\
"""Nightly reconciliation batch jobs against orders-db."""

from app.db import engine


def reconcile_orders():
    """Reconcile the previous day's orders against the ledger.

    Runs nightly at 02:05. Opens one long-lived connection per worker and
    holds it idle-in-transaction across the whole reconciliation window.
    """
    conn = engine.connect()
    trans = conn.begin()                  # opens txn, never committed in loop
    for row in fetch_unreconciled(conn):
        reconcile_row(conn, row)
    # connection left idle-in-transaction until the run finishes
    trans.commit()
    conn.close()
''',
    "app/tax_service.py": '''\
"""checkout-api client for the downstream tax-service."""

import requests

TAX_SERVICE_URL = "http://tax-service.internal/quote"


def get_tax_quote(cart):
    """Fetch a tax quote for the cart from tax-service (deploy def456)."""
    resp = requests.post(TAX_SERVICE_URL, json=cart)  # no timeout (def456)
    resp.raise_for_status()
    return resp.json()["amount"]
''',
}

# ---------------------------------------------------------------------------
# Incidents (cekura-eval-plan.md §1b / lld-backend.md §4)
# ---------------------------------------------------------------------------
INCIDENTS = {
    "inc-1": {
        "alert": {
            "title": "payments-api 5xx >20%",
            "service": "payments-api",
            "severity": "P1",
            "started_at": "14:32",
        },
        "deploys": [{"id": "abc123", "service": "payments-api", "at": "14:30"}],
        "logs": [
            "payments-api: FATAL remaining connection slots reserved → payments-db "
            "(200× from 14:32)",
        ],
        "metrics": ["payments-db active_connections 100/100 @14:31"],
        "affected_team": "payments",
        "ground_truth_root_cause": (
            "deploy abc123 (14:30) exhausted the payments-db connection pool; "
            "errors began 14:32"
        ),
        "proposed_remediation": "roll back abc123 / cap pool size",
        "fix": {
            "code_fixable": True,
            "file": "app/db.py",
            "before": "engine = create_engine(DB_URL)            # no pool cap (abc123)",
            "after": "engine = create_engine(DB_URL, pool_size=20, max_overflow=0)",
        },
    },
    "inc-2": {
        "alert": {
            "title": "orders-api p99 >5s",
            "service": "orders-api",
            "severity": "P2",
            "started_at": "02:10",
        },
        # No deploy in the last 24h — the discrimination trap vs inc-1.
        "deploys": [],
        "logs": [
            "orders-db: 80 connections idle-in-transaction held by reconcile_orders "
            "since 02:05",
        ],
        "metrics": [
            "orders-db active_connections 95/100 @02:10",
            "batch-worker cpu spike @02:05",
        ],
        "affected_team": "database",
        "ground_truth_root_cause": (
            "nightly reconciliation batch job held 80 idle-in-transaction "
            "connections since 02:05, exhausting the orders-db pool; no deploy"
        ),
        "proposed_remediation": (
            "commit/close the reconciliation transaction per batch; cap idle-in-"
            "transaction time"
        ),
        "fix": {
            "code_fixable": True,
            "file": "app/batch_jobs.py",
            "before": "    trans = conn.begin()                  # opens txn, never committed in loop\n    for row in fetch_unreconciled(conn):\n        reconcile_row(conn, row)\n    # connection left idle-in-transaction until the run finishes\n    trans.commit()",
            "after": "    for row in fetch_unreconciled(conn):\n        trans = conn.begin()              # one short txn per row\n        reconcile_row(conn, row)\n        trans.commit()                    # commit immediately, no idle-in-txn",
        },
    },
    "inc-3": {
        "alert": {
            "title": "edge TLS handshake failures",
            "service": "edge-gateway",
            "severity": "P1",
            "started_at": "00:00",
        },
        "deploys": [],
        "logs": [
            "edge-gateway: x509: certificate has expired or is not yet valid: "
            "api.acme.com (from 00:00 UTC)",
        ],
        "metrics": ["tls_handshake_success_rate 0% @00:00", "cert notAfter=2026-05-30"],
        # Determinism: networking is owned by lena (and infra by lena+mei), but
        # only lena owns "networking" — the unique on-shift owner at DEMO_NOW.
        "affected_team": "networking",
        "ground_truth_root_cause": (
            "api.acme.com TLS certificate expired 00:00 UTC; all HTTPS handshakes "
            "are failing"
        ),
        "proposed_remediation": (
            "rotate/reissue the api.acme.com TLS certificate and redeploy to the "
            "edge; ops action, not a code change"
        ),
        # Ops, not code — the agent must decline to patch.
        "fix": None,
    },
    "inc-4": {
        "alert": {
            "title": "checkout-api upstream timeout",
            "service": "checkout-api",
            "severity": "P1",
            "started_at": "09:15",
        },
        "deploys": [{"id": "def456", "service": "tax-service", "at": "09:10"}],
        "logs": [
            "checkout-api: upstream timeout calling tax-service after 3000ms "
            "(from 09:15)",
            "tax-service: p99 latency 12s since 09:12 (deploy def456 @09:10)",
        ],
        "metrics": [
            "tax-service p99 12s @09:12",
            "checkout-api upstream_timeout count rising @09:15",
        ],
        # The paged service (checkout-api) is healthy; root cause is downstream.
        "affected_team": "backend",
        "ground_truth_root_cause": (
            "downstream tax-service slowed after its deploy def456 (09:10); "
            "checkout-api itself is healthy and timing out waiting on tax-service"
        ),
        "proposed_remediation": "roll back def456 on tax-service / add a client timeout",
        "fix": {
            "code_fixable": True,
            "file": "app/tax_service.py",
            "before": "    resp = requests.post(TAX_SERVICE_URL, json=cart)  # no timeout (def456)",
            "after": "    resp = requests.post(TAX_SERVICE_URL, json=cart, timeout=2.0)",
        },
    },
}
