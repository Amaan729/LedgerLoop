from sqlalchemy import delete, insert, update

from ledgerloop import audit, db


def write_chain(engine, n=5):
    chain = audit.AuditChain()
    rows = [chain.next_row(i, "decision", f"d{i}", {"action": "released", "amount_cents": i * 100}) for i in range(1, n + 1)]
    with engine.begin() as conn:
        conn.execute(insert(db.audit_log), rows)
    return chain


def test_clean_chain_verifies(engine):
    chain = write_chain(engine)
    with engine.connect() as conn:
        res = audit.verify(conn)
    assert res.ok and res.entries == 5
    assert res.head_hash == chain.head_hash


def test_edited_body_is_detected(engine):
    write_chain(engine)
    with engine.begin() as conn:
        conn.execute(
            update(db.audit_log).where(db.audit_log.c.position == 3).values(body={"action": "released", "amount_cents": 1})
        )
    with engine.connect() as conn:
        res = audit.verify(conn)
    assert not res.ok
    assert res.first_bad_position == 3
    assert "content" in res.reason


def test_deleted_row_is_detected(engine):
    write_chain(engine)
    with engine.begin() as conn:
        conn.execute(delete(db.audit_log).where(db.audit_log.c.position == 2))
    with engine.connect() as conn:
        res = audit.verify(conn)
    assert not res.ok and res.first_bad_position == 2


def test_chain_resumes_from_stored_head(engine):
    write_chain(engine, n=3)
    with engine.begin() as conn:
        chain = audit.AuditChain.load(conn)
        assert chain.position == 3
        conn.execute(insert(db.audit_log), [chain.next_row(4, "decision", "d4", {"x": 1})])
    with engine.connect() as conn:
        assert audit.verify(conn).ok
