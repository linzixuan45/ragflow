from api.db.db_models import TransactionAwareConnectionContext


class FakeDatabase:
    def __init__(self, *, closed: bool):
        self.closed = closed
        self.connect_calls = 0
        self.close_calls = 0

    def is_closed(self):
        return self.closed

    def connect(self):
        self.connect_calls += 1
        self.closed = False

    def close(self):
        self.close_calls += 1
        self.closed = True


def test_connection_context_closes_connection_it_opened():
    database = FakeDatabase(closed=True)

    with TransactionAwareConnectionContext(database):
        assert not database.is_closed()

    assert database.connect_calls == 1
    assert database.close_calls == 1


def test_connection_context_preserves_outer_transaction_connection():
    database = FakeDatabase(closed=False)

    with TransactionAwareConnectionContext(database):
        assert not database.is_closed()

    assert database.connect_calls == 0
    assert database.close_calls == 0


def test_decorated_call_uses_a_fresh_connection_context_each_time():
    database = FakeDatabase(closed=True)
    context = TransactionAwareConnectionContext(database)

    @context
    def operation():
        assert not database.is_closed()

    operation()
    operation()

    assert database.connect_calls == 2
    assert database.close_calls == 2
    assert not context.opened
