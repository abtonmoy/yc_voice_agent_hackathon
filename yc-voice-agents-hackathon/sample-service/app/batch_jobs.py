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
