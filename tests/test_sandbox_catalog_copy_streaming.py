"""S5: the catalog copy streams. No table is held in memory on either side; prod's export runs under
SET LOCAL limits and a deadline. These tests need no database; the Postgres ones are in
tests/test_sandbox_catalog_copy_postgres.py."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import random
import zlib

import pytest

from jobs import sandbox_catalog_copy as C

# --- the record splitter ------------------------------------------------------------------------

_CELLS = ["plain", 'say "hi"', "multi\nline", '"', '""', "a,b", "\n", "", "한국어 텍스트", "日本語\n改行",
          "emoji 😀", 'end"\n"x', "\r\n", "x" * 300]


def _csv(rows):
    out = io.StringIO()
    csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(rows)
    return out.getvalue().encode("utf-8")


def _corpus(seed=7, n=60):
    rng = random.Random(seed)
    return [[rng.choice(_CELLS) for _ in range(3)] for _ in range(n)]


def _split_all(data: bytes, cuts):
    splitter = C.RecordSplitter("t")
    blocks, start = [], 0
    for cut in list(cuts) + [len(data)]:
        block = splitter.feed(data[start:cut])
        if block:
            blocks.append(block)
        start = cut
    splitter.finish()
    return blocks


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 64, 4096])
def test_any_chunking_gives_back_exactly_the_records(size):
    rows = _corpus()
    data = _csv(rows)
    blocks = _split_all(data, range(size, len(data), size))
    assert b"".join(blocks) == data
    got = []
    for block in blocks:  # every block holds whole records only
        got.extend(csv.reader(io.StringIO(block.decode("utf-8"), newline="")))
    assert got == rows


def test_random_cuts_never_split_a_record_or_a_character():
    rows = _corpus(seed=11, n=200)
    data = _csv(rows)
    rng = random.Random(3)
    for _ in range(50):
        cuts = sorted(rng.sample(range(1, len(data)), 40))
        blocks = _split_all(data, cuts)
        for block in blocks:
            block.decode("utf-8")  # a cut inside a multi-byte character would raise
            assert block.endswith(b"\n") and block.count(b'"') % 2 == 0
        assert b"".join(blocks) == data


def test_unquoted_records_as_copy_writes_them_split_too():
    data = b'm1,__null__,12.5\nm2,"a\nb",3\n'
    assert _split_all(data, [17, 22]) == [b"m1,__null__,12.5\n", b'm2,"a\nb",3\n']  # 22: inside "a\nb"
    assert _split_all(data, [4, 13]) == [data]  # nothing whole until the last chunk


def test_a_stream_cut_off_inside_a_record_is_refused():
    splitter = C.RecordSplitter("catalog_offers")
    assert splitter.feed(b'"a","b\n') == b""  # the newline is inside a quoted field
    with pytest.raises(C.CopyRefused, match="catalog_offers: the CSV stream ends inside a record"):
        splitter.finish()
    splitter = C.RecordSplitter("catalog_offers")
    splitter.feed(b'"a"\n"b"')
    with pytest.raises(C.CopyRefused, match="ends inside a record"):
        splitter.finish()


def test_one_record_longer_than_the_cap_is_refused_and_a_record_at_the_cap_is_not():
    splitter = C.RecordSplitter("t", max_record=8)
    assert splitter.feed(b'"123456"') == b""  # 8 bytes, unfinished: still allowed
    assert splitter.feed(b"\n") == b'"123456"\n'
    with pytest.raises(C.CopyRefused, match="longer than 8 bytes"):
        splitter.feed(b'"12345678')
    splitter = C.RecordSplitter("t", max_record=8)
    with pytest.raises(C.CopyRefused, match="longer than 8 bytes"):
        splitter.feed(b'"a"\n"12345678')  # the tail after a whole record counts too


def test_the_default_cap_is_the_module_constant(monkeypatch):
    monkeypatch.setattr(C, "MAX_RECORD_BYTES", 3)
    with pytest.raises(C.CopyRefused, match="longer than 3 bytes"):
        C.RecordSplitter("t").feed(b"abcd")


# --- the bounded gunzip -------------------------------------------------------------------------


def _chunks(data, size):
    return [data[i:i + size] for i in range(0, len(data), size)]


def test_a_small_file_that_inflates_hugely_comes_out_in_bounded_steps(monkeypatch):
    monkeypatch.setattr(C, "INFLATE_STEP_BYTES", 1 << 16)
    bomb = gzip.compress(b"\0" * (16 << 20))  # 16 MiB from about 16 KiB
    assert len(bomb) < 64 << 10
    pieces = list(C._inflated([bomb], "t"))
    assert max(len(p) for p in pieces) <= 1 << 16
    assert sum(len(p) for p in pieces) == 16 << 20


@pytest.mark.parametrize("size", [1, 5, 1000])
def test_gunzip_in_any_chunking_is_exact(size):
    raw = _csv(_corpus(n=300))
    assert b"".join(C._inflated(_chunks(gzip.compress(raw), size), "t")) == raw


@pytest.mark.parametrize("damage,match", [
    (lambda z: z[:-5], "cut off"),
    (lambda z: z + b"junk", "after its gzip stream"),
    (lambda z: z + gzip.compress(b"second member\n"), "after its gzip stream"),
    (lambda z: b"not gzip at all", "not valid gzip"),
    (lambda z: b"", "cut off"),
])
def test_a_damaged_file_is_refused(damage, match):
    data = damage(gzip.compress(b'"a"\n'))
    for size in (1, 3, 4096):
        with pytest.raises(C.CopyRefused, match=match):
            list(C._inflated(_chunks(data, size), "catalog_offers"))


# --- the export sink ----------------------------------------------------------------------------


class _Writer:
    def __init__(self):
        self.data = bytearray()
        self.flushed = 0

    def write(self, data):
        self.data += data

    def flush(self):
        self.flushed += 1


async def test_the_sink_scrubs_checks_and_gzips_record_blocks(_stand_in_text_column):
    writer = _Writer()
    sink = C._ExportSink("catalog_merchants", ["merchant_id", "merchant_name"], "__N__", writer)
    raw = b'm1,"call +1 415 555 0100 now"\nm2,__N__\n'
    for i in range(len(raw)):
        await sink(raw[i:i + 1])
    await sink.finish()
    assert sink.records == 2 and writer.flushed == 1
    assert gzip.decompress(bytes(writer.data)) == b'"m1","call [redacted] now"\n"m2",\n'
    assert sink.sha.hexdigest() == hashlib.sha256(bytes(writer.data)).hexdigest()
    assert sink.size == len(writer.data)


async def test_a_refusal_inside_the_copy_is_kept_and_raised_after_it(_stand_in_text_column):
    writer = _Writer()
    sink = C._ExportSink("catalog_merchants", ["merchant_id", "merchant_name"], "__N__", writer)
    await sink(b'"m1","x"\n"owner@realbrand.example","y"\n')  # does not raise inside the COPY
    await sink(b'"m3","ok"\n')  # dropped unread
    assert isinstance(sink.error, C.CopyRefused)
    with pytest.raises(C.CopyRefused, match="column merchant_id holds an email-shaped value") as excinfo:
        await sink.finish()
    assert "owner@realbrand.example" not in str(excinfo.value)
    assert bytes(writer.data) == b""  # nothing of the refused block left


@pytest.fixture
def _stand_in_text_column(monkeypatch):
    from jobs import sandbox_catalog_columns as COLS

    merged = {t: dict(c) for t, c in COLS.COLUMNS.items()}
    merged["catalog_merchants"]["merchant_name"] = COLS.TEXT
    monkeypatch.setattr(COLS, "COLUMNS", merged)


# --- export limits and the deadline (fake connection) ---------------------------------------------


class _FakeConn:
    def __init__(self, rows=b"m1\n", copy=None):
        self.executed = []
        self.rows = rows
        self.copy = copy
        self.terminated = False

    def transaction(self, **kw):
        conn = self

        class T:
            async def __aenter__(self):
                conn.executed.append(("BEGIN", kw))

            async def __aexit__(self, *a):
                conn.executed.append(("END",))
                return False
        return T()

    async def execute(self, *a):
        self.executed.append(a)
        return "SELECT 1"

    async def fetch(self, *a):
        return [{"column_name": "merchant_id", "type_name": "varchar", "type_kind": "b",
                 "base_name": None, "elem_name": None, "base_elem_name": None}]

    async def fetchval(self, *a):
        return self.rows.count(b"\n")

    async def copy_from_query(self, query, output, **kw):
        if self.copy:
            return await self.copy(self, output)
        await output(self.rows)

    def terminate(self):
        self.terminated = True


_SPEC = (C.TableSpec("catalog_merchants", "x"),)


async def test_the_limits_are_set_local_inside_the_snapshot(tmp_path):
    conn = _FakeConn()
    await C.export_catalog(conn, C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=_SPEC,
                           statement_timeout="90s", lock_timeout="250ms", idle_timeout="45s")
    begin = conn.executed.index(("BEGIN", {"isolation": "repeatable_read", "readonly": True}))
    sets = [a[1:] for a in conn.executed[begin + 1:begin + 5]]
    assert all(a[0] == "SELECT pg_catalog.set_config($1, $2, true)" for a in conn.executed[begin + 1:begin + 5])
    assert sets == [("statement_timeout", "90s"), ("lock_timeout", "250ms"),
                    ("idle_in_transaction_session_timeout", "45s"), ("application_name", "sandbox_catalog_export")]


@pytest.mark.parametrize("major,expected", [(15, True), (14, True), (13, False)])
async def test_the_client_check_is_set_where_the_server_has_it(tmp_path, major, expected):
    import types

    conn = _FakeConn()
    conn.get_server_version = lambda: types.SimpleNamespace(major=major)
    await C.export_catalog(conn, C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=_SPEC)
    assert (("SELECT pg_catalog.set_config($1, $2, true)", "client_connection_check_interval", "5s")
            in conn.executed) is expected


def test_the_defaults_bound_prod():
    assert (C.EXPORT_STATEMENT_TIMEOUT, C.EXPORT_LOCK_TIMEOUT, C.EXPORT_IDLE_IN_TRANSACTION_TIMEOUT) == \
        ("20min", "5s", "2min")
    assert C.EXPORT_DEADLINE_SECONDS == 45 * 60 <= C.MAX_EXPORT_DEADLINE_SECONDS == 2 * 60 * 60
    assert C.UPLOAD_CHUNK_BYTES % (256 << 10) == 0


@pytest.mark.parametrize("kw", [
    {"statement_timeout": "0"}, {"statement_timeout": "15min'; RESET ALL; --"}, {"lock_timeout": "5 s"},
    {"idle_timeout": ""}, {"lock_timeout": "1h"}, {"deadline_seconds": 0}, {"deadline_seconds": -1},
    {"deadline_seconds": 2 * 60 * 60 + 1},
])
async def test_a_bad_limit_is_refused_before_anything_is_read(tmp_path, kw):
    class NoDb:
        def __getattr__(self, name):
            raise AssertionError("read the source")

    with pytest.raises(C.CopyRefused, match="must"):
        await C.export_catalog(NoDb(), C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=_SPEC, **kw)


async def test_the_deadline_terminates_the_session_and_writes_nothing(tmp_path):
    import asyncio

    async def stall(conn, output):
        await output(b"m1\n")
        while not conn.terminated:
            await asyncio.sleep(0.01)
        raise ConnectionError("connection was closed in the middle of operation")

    conn = _FakeConn(copy=stall)
    store = C.LocalDirStore(tmp_path)
    with pytest.raises(C.CopyRefused, match="passed its 0.2s deadline; nothing was written"):
        await C.export_catalog(conn, store, stamp="20261127T0100Z", specs=_SPEC, deadline_seconds=0.2)
    assert conn.terminated
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


async def test_a_record_count_that_differs_from_the_snapshot_is_refused_and_nothing_is_written(tmp_path):
    class Short(_FakeConn):
        async def fetchval(self, *a):
            return self.rows.count(b"\n") + 1

    with pytest.raises(C.CopyRefused, match="CSV record count differs from the snapshot's"):
        await C.export_catalog(Short(), C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=_SPEC)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


async def test_a_deadline_that_fires_as_the_export_returns_still_refuses(tmp_path, monkeypatch):
    import asyncio

    async def late(conn, output):
        await output(b"m1\n")
        await asyncio.sleep(0.3)  # the deadline fires; this fake connection ignores the terminate

    conn = _FakeConn(copy=late)
    with pytest.raises(C.CopyRefused, match="deadline"):
        await C.export_catalog(conn, C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=_SPEC,
                               deadline_seconds=0.1)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


@pytest.mark.parametrize("sqlstate,limit", [("57014", "statement_timeout"), ("55P03", "lock_timeout"),
                                            ("25P03", "idle_in_transaction_session_timeout")])
async def test_a_server_limit_is_a_refusal_naming_it(tmp_path, sqlstate, limit):
    class LimitHit(Exception):
        pass

    async def hit(conn, output):
        exc = LimitHit("canceling statement")
        exc.sqlstate = sqlstate
        raise exc

    with pytest.raises(C.CopyRefused, match="hit its %s on the source; nothing was written" % limit):
        await C.export_catalog(_FakeConn(copy=hit), C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=_SPEC)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


async def test_any_other_error_propagates_as_itself(tmp_path):
    async def boom(conn, output):
        raise RuntimeError("other")

    with pytest.raises(RuntimeError, match="other"):
        await C.export_catalog(_FakeConn(copy=boom), C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=_SPEC)


async def test_no_file_exists_until_every_table_has_passed(tmp_path):
    """Table 1 passes, table 2 refuses: table 1's upload is never committed."""
    calls = []

    async def second_fails(conn, output):
        calls.append(1)
        await output(b"m1\n" if len(calls) == 1 else b"\"a@b.example\"\n")

    specs = (C.TableSpec("catalog_merchants", "x"), C.TableSpec("catalog_products", "x"))

    class Conn(_FakeConn):
        async def fetchval(self, *a):
            return 1

    with pytest.raises(C.CopyRefused, match="email-shaped"):
        await C.export_catalog(Conn(copy=second_fails), C.LocalDirStore(tmp_path), stamp="20261127T0100Z",
                               specs=specs)
    assert len(calls) == 2
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


# --- the local store's writer -------------------------------------------------------------------


def test_a_local_upload_is_invisible_until_commit_and_never_overwrites(tmp_path):
    store = C.LocalDirStore(tmp_path)
    w = store.open_writer("20261127T0100Z/a.csv.gz")
    w.write(b"one")
    w.flush()
    assert store.list("") == [] and not (tmp_path / "20261127T0100Z/a.csv.gz").exists()
    w.commit()
    assert store.list("") == ["20261127T0100Z/a.csv.gz"]
    assert b"".join(store.open_reader("20261127T0100Z/a.csv.gz", None)) == b"one"
    again = store.open_writer("20261127T0100Z/a.csv.gz")
    again.write(b"two")
    with pytest.raises(C.CopyRefused, match="never overwrites"):
        again.commit()
    assert store.get("20261127T0100Z/a.csv.gz") == b"one"
    gone = store.open_writer("20261127T0100Z/b.csv.gz")
    gone.write(b"x")
    gone.abort()
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == ["a.csv.gz"]


# --- the GCS store, against a fake GCS ------------------------------------------------------------


SESSION = "https://storage.googleapis.com/upload/storage/v1/b/pivota-sandbox-catalog-drop/o?upload_id=SESSIONSECRET"


class _R:
    def __init__(self, code, headers=None, body=None):
        self.status_code = code
        self.headers = headers or {}
        self._body = body or {}
        self.content = b"secret body"

    def json(self):
        return self._body


class _FakeGcs:
    """Answers like the JSON API's resumable upload: 308 with the persisted range, 200 on the last."""

    def __init__(self, init=200, final=200, short=False):
        self.puts, self.deletes, self.posts = [], [], []
        self.init, self.final, self.short = init, final, short
        self.stored = bytearray()

    def get(self, url, **kw):
        return _R(200, body={"access_token": "t", "generation": "1764200000000001"})

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return _R(self.init, {"location": SESSION})

    def put(self, url, **kw):
        assert url == SESSION
        span = kw["headers"]["Content-Range"]
        self.puts.append((span, len(kw["content"])))
        self.stored += kw["content"]
        if span.endswith("/*"):
            end = len(self.stored) - 1 - (1 if self.short else 0)
            return _R(308, {"range": "bytes=0-%d" % end})
        return _R(self.final)

    def delete(self, url, **kw):
        self.deletes.append(url)
        return _R(499)


@pytest.fixture
def gcs(monkeypatch):
    import httpx

    fake = _FakeGcs()
    for method in ("get", "post", "put", "delete"):
        monkeypatch.setattr(httpx, method, getattr(fake, method))
    return fake


def _store():
    return C.GcsStore("gs://pivota-sandbox-catalog-drop/catalog")


def test_a_gcs_upload_goes_in_whole_256k_pieces_and_is_created_only_by_the_last(gcs, monkeypatch):
    monkeypatch.setattr(C, "UPLOAD_CHUNK_BYTES", 4 * C._GcsUpload.QUANTUM)
    q = C._GcsUpload.QUANTUM
    w = _store().open_writer("20261127T0100Z/catalog_offers.csv.gz")
    url, kw = gcs.posts[0]
    assert kw["params"] == {"uploadType": "resumable", "name": "catalog/20261127T0100Z/catalog_offers.csv.gz",
                            "ifGenerationMatch": "0"}
    w.write(b"a" * (5 * q + 7))  # one full chunk goes, the rest waits
    assert gcs.puts == [("bytes 0-%d/*" % (4 * q - 1), 4 * q)]
    w.flush()  # every whole piece goes; less than one stays
    assert gcs.puts[-1] == ("bytes %d-%d/*" % (4 * q, 5 * q - 1), q)
    w.write(b"b" * 3)
    w.commit()
    assert gcs.puts[-1] == ("bytes %d-%d/%d" % (5 * q, 5 * q + 9, 5 * q + 10), 10)
    assert bytes(gcs.stored) == b"a" * (5 * q + 7) + b"bbb"
    assert all(n % q == 0 for span, n in gcs.puts[:-1])


def test_a_gcs_upload_ending_on_a_piece_boundary_finishes_with_an_empty_last_piece(gcs, monkeypatch):
    q = C._GcsUpload.QUANTUM
    w = _store().open_writer("20261127T0100Z/x.csv.gz")
    w.write(b"a" * q)
    w.flush()
    w.commit()
    assert gcs.puts == [("bytes 0-%d/*" % (q - 1), q), ("bytes */%d" % q, 0)]


def test_gcs_keeping_less_than_was_sent_is_a_refusal(monkeypatch):
    import httpx

    fake = _FakeGcs(short=True)
    for method in ("get", "post", "put", "delete"):
        monkeypatch.setattr(httpx, method, getattr(fake, method))
    w = _store().open_writer("20261127T0100Z/x.csv.gz")
    with pytest.raises(C.CopyRefused, match="HTTP 308 for upload") as excinfo:
        w.write(b"a" * C.UPLOAD_CHUNK_BYTES)
    assert "SESSIONSECRET" not in str(excinfo.value)


@pytest.mark.parametrize("where", ["init", "final"])
def test_an_existing_object_is_never_overwritten_by_an_upload(monkeypatch, where):
    import httpx

    fake = _FakeGcs(**{where: 412})
    for method in ("get", "post", "put", "delete"):
        monkeypatch.setattr(httpx, method, getattr(fake, method))
    with pytest.raises(C.CopyRefused, match="never overwrites"):
        w = _store().open_writer("20261127T0100Z/x.csv.gz")
        w.write(b"x")
        w.commit()


def test_gcs_errors_name_the_status_never_the_session_or_body(monkeypatch):
    import httpx

    fake = _FakeGcs(final=503)
    for method in ("get", "post", "put", "delete"):
        monkeypatch.setattr(httpx, method, getattr(fake, method))
    store = _store()
    with pytest.raises(C.CopyRefused, match="HTTP 503 for upload") as excinfo:
        store.put("20261127T0100Z/x.csv.gz", b"x")
    assert "SESSIONSECRET" not in str(excinfo.value) and "secret body" not in str(excinfo.value)
    assert fake.deletes == [SESSION]  # the failed upload's session is cancelled
    fake.init = 403
    with pytest.raises(C.CopyRefused, match="HTTP 403 for create"):
        store.open_writer("20261127T0100Z/y.csv.gz")


def test_a_session_url_off_googleapis_is_refused(monkeypatch):
    import httpx

    fake = _FakeGcs()
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _R(200, {"location": "https://evil.example/upload"}))
    with pytest.raises(C.CopyRefused, match="no upload session"):
        _store().open_writer("20261127T0100Z/x.csv.gz")


def test_gcs_abort_tolerates_an_unreachable_gcs(monkeypatch):
    import httpx

    fake = _FakeGcs()
    for method in ("get", "post"):
        monkeypatch.setattr(httpx, method, getattr(fake, method))

    def down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "delete", down)
    _store().open_writer("20261127T0100Z/x.csv.gz").abort()


def test_the_gcs_reader_pins_the_generation_and_streams(monkeypatch):
    import contextlib

    import httpx

    seen = {}

    class Streamed:
        status_code = 200

        def iter_bytes(self, size):
            seen["size"] = size
            yield b"ab"
            yield b"cd"

    @contextlib.contextmanager
    def stream(method, url, **kw):
        seen.update(method=method, url=url, params=kw["params"])
        yield Streamed()

    fake = _FakeGcs()
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "stream", stream)
    store = _store()
    gen = store.generation("20261127T0100Z/x.csv.gz")
    assert gen == "1764200000000001"
    assert b"".join(store.open_reader("20261127T0100Z/x.csv.gz", gen)) == b"abcd"
    assert seen["params"] == {"alt": "media", "generation": "1764200000000001"}
    assert seen["url"].endswith("/o/catalog%2F20261127T0100Z%2Fx.csv.gz") and seen["size"] == C.READ_CHUNK_BYTES


def test_a_gcs_read_failure_is_a_refusal(monkeypatch):
    import contextlib

    import httpx

    class Denied:
        status_code = 403

    @contextlib.contextmanager
    def stream(method, url, **kw):
        yield Denied()

    @contextlib.contextmanager
    def broken(method, url, **kw):
        raise httpx.ReadTimeout("slow")
        yield

    monkeypatch.setattr(httpx, "get", _FakeGcs().get)
    monkeypatch.setattr(httpx, "stream", stream)
    with pytest.raises(C.CopyRefused, match="HTTP 403 for read"):
        list(_store().open_reader("20261127T0100Z/x.csv.gz", "1"))
    monkeypatch.setattr(httpx, "stream", broken)
    with pytest.raises(C.CopyRefused, match="unreachable for read .* \\(ReadTimeout\\)"):
        list(_store().open_reader("20261127T0100Z/x.csv.gz", "1"))


# --- the import's first pass ----------------------------------------------------------------------


def _entry(data: bytes, rows: int, sha=None):
    return {"name": "catalog_merchants", "file": "20261127T0100Z/catalog_merchants.csv.gz",
            "columns": ["merchant_id"], "rows": rows,
            "sha256": sha or hashlib.sha256(data).hexdigest()}


def _local(tmp_path, data):
    store = C.LocalDirStore(tmp_path)
    store.put("20261127T0100Z/catalog_merchants.csv.gz", data)
    return store


def test_the_check_streams_sha_rows_and_scrub(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "READ_CHUNK_BYTES", 3)
    data = gzip.compress(b'"m1"\n"m2"\n')
    C._check_file(_local(tmp_path, data), _entry(data, 2), None)
    with pytest.raises(C.CopyRefused, match="row count"):
        C._check_file(_local(tmp_path / "b", data), _entry(data, 3), None)


def test_a_sha_mismatch_is_reported_before_what_the_content_shows(tmp_path):
    data = gzip.compress(b'"owner@realbrand.example"\n')
    store = _local(tmp_path, data)
    with pytest.raises(C.CopyRefused, match="sha256"):
        C._check_file(store, _entry(data, 1, sha="0" * 64), None)
    with pytest.raises(C.CopyRefused, match="email-shaped"):
        C._check_file(store, _entry(data, 1), None)
    bad = b"\xff\xfe" + b"x\n"
    zbad = gzip.compress(bad)
    with pytest.raises(C.CopyRefused, match="not UTF-8"):
        C._check_file(_local(tmp_path / "u", zbad), _entry(zbad, 1), None)


def test_a_read_that_fails_is_the_refusal_not_a_sha_mismatch(tmp_path):
    class Flaky(C.LocalDirStore):
        def open_reader(self, name, generation):
            yield gzip.compress(b'"m1"\n')[:5]
            raise C.CopyRefused("GCS unreachable for read %s (ReadTimeout)" % name)

    data = gzip.compress(b'"m1"\n')
    with pytest.raises(C.CopyRefused, match="unreachable for read"):
        C._check_file(Flaky(tmp_path), _entry(data, 1), None)


async def test_the_load_rechecks_the_sha_at_its_end(tmp_path):
    data = gzip.compress(b'"m1"\n')
    store = _local(tmp_path, data)
    got = [b async for b in C._load_stream(store, _entry(data, 1), None)]
    assert b"".join(got) == b'"m1"\n'
    with pytest.raises(C.CopyRefused, match="changed between the check and the load"):
        [b async for b in C._load_stream(store, _entry(data, 1, sha="0" * 64), None)]


def test_the_whole_file_forms_keep_their_answers():
    assert C.scrub_csv("catalog_merchants", b"m1,__N__\n", "__N__", ["merchant_id", "metadata_json"]) == b'"m1",\n'
    assert C.assert_scrubbed("catalog_merchants", b'"m1",\n', ["merchant_id", "metadata_json"]) == 0
    with pytest.raises(C.CopyRefused, match="has 1 fields, expected 2"):
        C.assert_scrubbed("catalog_merchants", b'"m1"\n', ["merchant_id", "metadata_json"])


def test_zlib_gzip_framing_reads_back_with_the_standard_library():
    z = zlib.compressobj(6, zlib.DEFLATED, 31)
    data = z.compress(b"abc\n") + z.flush()
    assert gzip.decompress(data) == b"abc\n"
