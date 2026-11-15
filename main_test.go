package main

// These tests are what lets CI tell a good dependency bump from a bad one. Each
// one pins behaviour that a change in pgx, its pool library (puddle), x/sync or
// x/text could plausibly move: the DSN parse and pool limits, the SQL round
// trip, the error text that reaches clients, the pool under contention, and
// SCRAM password login. On the releases replayed in M4 none is expected to
// fail; they exist so that the `go-test` check is a real check.
//
// Two Postgres servers are needed, and the CI job (`go-test` in
// .github/workflows/validate.yml) starts both as service containers:
//
//   - TEST_DB_HOST / TEST_DB_PORT: a server with `trust` authentication. The
//     service never sends a password (the Cloud SQL Auth Proxy does the IAM
//     login in production), so trust is what lets the real newPool connect
//     unchanged, as user postgres.
//   - TEST_SCRAM_HOST / TEST_SCRAM_PORT / TEST_SCRAM_PASSWORD: a server that
//     asks for a SCRAM-SHA-256 password. pgx normalises a password with
//     x/text/secure/precis before SCRAM, and that is the only path through
//     which this service reaches x/text at all; a non-ASCII password makes the
//     normalisation matter.
//
// Without those variables the database tests skip, so `go test ./...` works on
// a laptop with no database. A skip is a pass to `go test`, so the CI job fails
// on any "--- SKIP" line: an unconfigured job must not report success.
//
// The tests share one database and drop the notes table first, so they must
// not use t.Parallel.

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func testCfg(t *testing.T) config {
	t.Helper()
	host := os.Getenv("TEST_DB_HOST")
	if host == "" {
		t.Skip("TEST_DB_HOST not set")
	}
	return config{port: "0", dbEnabled: true, dbHost: host, dbPort: env("TEST_DB_PORT", "5432"), dbUser: "postgres", dbName: "postgres"}
}

// newDBServer builds a server on the real newPool and runs bootstrapDatabase
// synchronously, on an empty notes table.
func newDBServer(t *testing.T) *server {
	t.Helper()
	cfg := testCfg(t)
	ctx := context.Background()
	pool, err := newPool(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if _, err := pool.Exec(ctx, "DROP TABLE IF EXISTS notes"); err != nil {
		t.Fatal(err)
	}
	s := &server{cfg: cfg, pool: pool}
	bctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	s.bootstrapDatabase(bctx)
	if !s.schemaReady.Load() {
		t.Fatal("schema not ready")
	}
	return s
}

func do(h http.HandlerFunc, method, path, body string) (int, string, http.Header) {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	rr := httptest.NewRecorder()
	h(rr, req)
	b, _ := io.ReadAll(rr.Result().Body)
	return rr.Code, string(b), rr.Result().Header
}

func TestRootAndNotFound(t *testing.T) {
	t.Fatal("deliberate failure: go-test must fail once before it is made required (M4)")
	s := &server{}
	code, body, _ := do(s.handleRoot, "GET", "/", "")
	if code != 200 || !strings.HasPrefix(body, "hello from svc-hello\nhostname: ") {
		t.Fatalf("%d %q", code, body)
	}
	if code, _, _ := do(s.handleRoot, "GET", "/nope", ""); code != 404 {
		t.Fatal(code)
	}
}

func TestNoDatabaseMode(t *testing.T) {
	s := &server{cfg: config{}}
	if c, b, _ := do(s.handleHealthz, "GET", "/healthz", ""); c != 200 || b != "ok (no database configured)\n" {
		t.Fatalf("%d %q", c, b)
	}
	if c, b, _ := do(s.handleNotes, "GET", "/notes", ""); c != 503 || b != "no database available yet\n" {
		t.Fatalf("%d %q", c, b)
	}
}

// The production user name contains "@", which is why newPool builds a
// key/value DSN; a change in how pgx parses one, or in its TLS default for
// sslmode=disable, shows here. The pool is lazy, so no server is needed.
func TestNewPoolConfigAndKeyValueDSN(t *testing.T) {
	cfg := config{dbHost: "127.0.0.1", dbPort: "5432", dbUser: "svc-hello@platform-factory-ref.iam", dbName: "app"}
	p, err := newPool(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()
	c := p.Config()
	if c.MaxConns != 4 || c.MinConns != 0 || c.MaxConnLifetime != 30*time.Minute || c.MaxConnIdleTime != 5*time.Minute {
		t.Fatalf("pool limits changed: %+v", c)
	}
	cc := c.ConnConfig
	if cc.User != "svc-hello@platform-factory-ref.iam" || cc.Password != "" || cc.Host != "127.0.0.1" || cc.Port != 5432 || cc.Database != "app" || cc.TLSConfig != nil || cc.ConnectTimeout != 5*time.Second {
		t.Fatalf("dsn parse changed: user=%q pw=%q host=%q port=%d db=%q tls=%v ct=%v", cc.User, cc.Password, cc.Host, cc.Port, cc.Database, cc.TLSConfig, cc.ConnectTimeout)
	}
}

func TestBootstrapIdempotentAndHealthz(t *testing.T) {
	s := newDBServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	s.schemaReady.Store(false)
	if c, _, _ := do(s.handleHealthz, "GET", "/healthz", ""); c != 503 {
		t.Fatal("not ready must be 503", c)
	}
	s.bootstrapDatabase(ctx) // the table exists already: must be a no-op
	if c, b, _ := do(s.handleHealthz, "GET", "/healthz", ""); c != 200 || b != "ok\n" {
		t.Fatalf("%d %q", c, b)
	}
}

var postRe = regexp.MustCompile(`^note (\d+) written at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n$`)

// Parameter binding, text encoding and the timestamptz scan, end to end.
func TestNotesRoundTrip(t *testing.T) {
	s := newDBServer(t)
	if c, b, _ := do(s.handleNotes, "GET", "/notes", ""); c != 200 || b != "no notes yet; POST one to /notes\n" {
		t.Fatalf("%d %q", c, b)
	}
	for _, n := range []string{"first", "  padded  ", "unicode: héllo 日本語 🙂", "'); DROP TABLE notes;--"} {
		c, b, _ := do(s.handleNotes, "POST", "/notes", n)
		if c != 201 || !postRe.MatchString(b) {
			t.Fatalf("%d %q", c, b)
		}
	}
	c, b, _ := do(s.handleNotes, "GET", "/notes", "")
	lines := strings.Split(strings.TrimSuffix(b, "\n"), "\n")
	if c != 200 || len(lines) != 4 {
		t.Fatalf("%d %q", c, b)
	}
	want := []string{"'); DROP TABLE notes;--", "unicode: héllo 日本語 🙂", "padded", "first"}
	for i, l := range lines {
		f := strings.SplitN(l, "\t", 3)
		if len(f) != 3 || f[2] != want[i] || fmt.Sprint(4-i) != f[0] {
			t.Fatalf("line %d: %q", i, l)
		}
	}
}

func TestNotesValidationAndMethods(t *testing.T) {
	s := newDBServer(t)
	if c, _, _ := do(s.handleNotes, "POST", "/notes", "  \n"); c != 400 {
		t.Fatal(c)
	}
	c, _, h := do(s.handleNotes, "PUT", "/notes", "x")
	if c != 405 || h.Get("Allow") != "GET, POST" {
		t.Fatal(c, h)
	}
}

// Pins today's behaviour, which svc-hello#16 records as a defect: a body over
// 64 KiB is truncated and stored, with 201 and no 413.
func TestNotesBodyLimitTruncatesSilently(t *testing.T) {
	s := newDBServer(t)
	c, _, _ := do(s.handleNotes, "POST", "/notes", strings.Repeat("a", 64*1024+500))
	if c != 201 {
		t.Fatal(c)
	}
	var n int
	if err := s.pool.QueryRow(context.Background(), "SELECT length(body) FROM notes").Scan(&n); err != nil || n != 64*1024 {
		t.Fatal(n, err)
	}
}

// Pins that pgx's error text, SQLSTATE suffix included, reaches the client (also
// recorded in svc-hello#16). A pgx change to PgError's formatting fails here.
func TestNotesDatabaseErrorText(t *testing.T) {
	s := newDBServer(t)
	if _, err := s.pool.Exec(context.Background(), "DROP TABLE notes"); err != nil {
		t.Fatal(err)
	}
	c, b, _ := do(s.handleNotes, "POST", "/notes", "x")
	if c != 500 || !strings.HasPrefix(b, "insert failed: ") || !strings.Contains(b, `relation "notes" does not exist (SQLSTATE 42P01)`) {
		t.Fatalf("%d %q", c, b)
	}
	c, b, _ = do(s.handleNotes, "GET", "/notes", "")
	if c != 500 || !strings.HasPrefix(b, "query failed: ") || !strings.Contains(b, "SQLSTATE 42P01") {
		t.Fatalf("%d %q", c, b)
	}
}

// 32 concurrent writes through a pool of 4. This is the test that goes through
// puddle's acquire path, which is where x/sync's semaphore is used.
func TestPoolContentionMaxConns4(t *testing.T) {
	s := newDBServer(t)
	var wg sync.WaitGroup
	codes := make([]int, 32)
	for i := range codes {
		wg.Add(1)
		go func() { defer wg.Done(); codes[i], _, _ = do(s.handleNotes, "POST", "/notes", fmt.Sprint("n", i)) }()
	}
	wg.Wait()
	for i, c := range codes {
		if c != 201 {
			t.Fatal(i, c)
		}
	}
	if s.pool.Stat().MaxConns() != 4 || s.pool.Stat().TotalConns() > 4 {
		t.Fatal(s.pool.Stat().TotalConns())
	}
}

// SCRAM login with a non-ASCII password, and a wrong one refused. This does not
// use newPool, which never sends a password; it is here only because it is the
// one path into x/text/secure/precis.
func TestScramPasswordAuth(t *testing.T) {
	host := os.Getenv("TEST_SCRAM_HOST")
	if host == "" {
		t.Skip("TEST_SCRAM_HOST not set")
	}
	password := os.Getenv("TEST_SCRAM_PASSWORD")
	if !hasNonASCII(password) {
		t.Fatal("TEST_SCRAM_PASSWORD must contain a non-ASCII character, or this test does not exercise password normalisation")
	}
	dsn := func(pw string) string {
		return fmt.Sprintf("host=%s port=%s user=postgres dbname=postgres password=%s sslmode=disable connect_timeout=5",
			host, env("TEST_SCRAM_PORT", "5432"), dsnQuote(pw))
	}
	ctx := context.Background()
	p, err := pgxpool.New(ctx, dsn(password))
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()
	var one int
	if err := p.QueryRow(ctx, "SELECT 1").Scan(&one); err != nil || one != 1 {
		t.Fatal(err)
	}
	var method string
	if err := p.QueryRow(ctx, "SELECT left(rolpassword, 13) FROM pg_authid WHERE rolname = 'postgres'").Scan(&method); err != nil || method != "SCRAM-SHA-256" {
		t.Fatalf("the server must store a SCRAM verifier for postgres, got %q, %v", method, err)
	}
	p2, err := pgxpool.New(ctx, dsn(password+"x"))
	if err != nil {
		t.Fatal(err)
	}
	defer p2.Close()
	if err := p2.QueryRow(ctx, "SELECT 1").Scan(&one); err == nil || !strings.Contains(err.Error(), "28P01") {
		t.Fatal("a wrong password must be refused with SQLSTATE 28P01, got", err)
	}
}

func hasNonASCII(s string) bool {
	for _, r := range s {
		if r > 127 {
			return true
		}
	}
	return false
}

// dsnQuote quotes a key/value DSN value the way libpq does: single quotes, with
// backslash and single quote escaped.
func dsnQuote(v string) string {
	return "'" + strings.NewReplacer(`\`, `\\`, `'`, `\'`).Replace(v) + "'"
}
