// svc-hello is the Platform Factory's canonical example service: the smallest
// thing that proves the paved road works end to end.
//
// The point it exists to make is ADR-0013's: the application holds NO database
// password. It connects to 127.0.0.1:5432, where the Cloud SQL Auth Proxy
// sidecar is listening. The proxy mints a short-lived IAM token from the pod's
// own Google identity (Kubernetes ServiceAccount svc-hello -> GSA
// svc-hello@platform-factory-ref.iam.gserviceaccount.com via Workload Identity)
// and does the TLS. So this process sends a Postgres startup packet with a
// username and no password, over plaintext loopback, and that is the whole
// credential story. There is no Secret mounted into this pod.
//
// Plain net/http and pgx. No framework: a reference implementation is read more
// often than it is run, and every dependency is a thing a reader has to know.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// config is everything the process reads from its environment. Defaults are the
// values k8s/deployment.yaml sets anyway — they exist so `go run .` works on a
// laptop with DB_ENABLED unset.
type config struct {
	port      string
	dbEnabled bool
	dbHost    string
	dbPort    string
	dbUser    string
	dbName    string
}

func loadConfig() config {
	return config{
		port: env("PORT", "8080"),
		// DB_ENABLED is the switch that lets this image run before the Database
		// claim exists. C-07's timing test deploys the service first and the
		// database second; without this flag the first deploy would crash-loop
		// and pollute the "PR -> usable database" clock with unrelated noise.
		dbEnabled: env("DB_ENABLED", "false") == "true",
		// The proxy sidecar, not the instance. The instance's private IP never
		// appears in this process's configuration.
		dbHost: env("DB_HOST", "127.0.0.1"),
		dbPort: env("DB_PORT", "5432"),
		// The Cloud SQL IAM database username for a service account is its
		// email with ".gserviceaccount.com" removed — Cloud SQL's own rule,
		// forced by the Postgres 63-byte username limit (ADR-0013). So this is
		// "svc-hello@platform-factory-ref.iam", not a full SA email.
		dbUser: env("DB_USER", ""),
		dbName: env("DB_NAME", "app"),
	}
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// server holds the process state the handlers share.
type server struct {
	cfg  config
	pool *pgxpool.Pool

	// schemaReady flips to true once the notes table exists. It is separate
	// from "the pool can reach the database" because the CREATE TABLE needs
	// privileges the Composition's GRANT job hands out asynchronously — see
	// bootstrapDatabase. /healthz must stay red until both are true, or Argo
	// would report the service Healthy before it can actually serve /notes.
	schemaReady atomic.Bool
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	cfg := loadConfig()

	srv := &server{cfg: cfg}

	// Signal handling before anything else: with a native sidecar the kubelet
	// stops this container first and the proxy second, so a clean shutdown here
	// means in-flight queries finish while the proxy is still up.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if cfg.dbEnabled {
		if cfg.dbUser == "" {
			log.Fatal("DB_ENABLED=true but DB_USER is empty; set it to the Cloud SQL IAM user, e.g. svc-hello@platform-factory-ref.iam")
		}

		pool, err := newPool(ctx, cfg)
		if err != nil {
			// A bad DSN is a config bug and should fail loudly. An unreachable
			// database is not: newPool does not connect (see below), so getting
			// here means the string itself was wrong.
			log.Fatalf("building the database pool: %v", err)
		}
		srv.pool = pool
		defer pool.Close()

		// Reaching the database is retried in the background rather than
		// blocking startup. On a first deploy the Database XR, the instance,
		// the IAM user and the GRANT job all land at their own pace; a pod that
		// exits because none of that is ready yet turns a normal ordering
		// window into CrashLoopBackOff.
		go srv.bootstrapDatabase(ctx)
	} else {
		log.Print("DB_ENABLED is not true; running without a database. /notes will return 503.")
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/", srv.handleRoot)
	mux.HandleFunc("/healthz", srv.handleHealthz)
	mux.HandleFunc("/notes", srv.handleNotes)

	httpSrv := &http.Server{
		Addr:              ":" + cfg.port,
		Handler:           logRequests(mux),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		log.Printf("listening on %s (db_enabled=%t)", httpSrv.Addr, cfg.dbEnabled)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Print("shutdown signal received; draining")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
	log.Print("stopped")
}

// newPool builds the connection pool. It deliberately does not dial: pgxpool is
// lazy by default, so a database that is not up yet costs nothing at startup.
func newPool(ctx context.Context, cfg config) (*pgxpool.Pool, error) {
	// Key/value DSN, not a postgres:// URL, because the username contains an
	// "@" (svc-hello@platform-factory-ref.iam) and would have to be
	// percent-encoded in a URL. Key/value form takes it verbatim.
	//
	// sslmode=disable is correct and not a shortcut: the hop being disabled is
	// the loopback hop to the proxy inside this pod's own network namespace.
	// The proxy then opens a mutually-authenticated TLS session to Cloud SQL.
	// No password key appears at all — that is the ADR-0013 property.
	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s dbname=%s sslmode=disable connect_timeout=5",
		cfg.dbHost, cfg.dbPort, cfg.dbUser, cfg.dbName,
	)

	poolCfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	// Small on purpose. The S size class is db-f1-micro, whose connection limit
	// is low; a reference service that exhausts it teaches the wrong lesson.
	poolCfg.MinConns = 0
	poolCfg.MaxConns = 4
	// IAM auth tokens are short-lived. The proxy refreshes them, but recycling
	// connections keeps a long-lived pool from pinning anything stale.
	poolCfg.MaxConnLifetime = 30 * time.Minute
	poolCfg.MaxConnIdleTime = 5 * time.Minute

	return pgxpool.NewWithConfig(ctx, poolCfg)
}

// bootstrapDatabase retries "connect, then create the table" until it works.
//
// The failure it is built around: the IAM user exists on the instance from the
// moment Crossplane creates it, but a fresh Cloud SQL IAM user has NO
// privileges. The Composition's GRANT job runs psql as the built-in postgres
// user and grants them (ADR-0013 §3). Until that job completes, this process
// can log in and then fail CREATE TABLE with "permission denied for schema
// public". Both states are expected and transient, so both are retried.
func (s *server) bootstrapDatabase(ctx context.Context) {
	const ddl = `
CREATE TABLE IF NOT EXISTS notes (
	id         BIGSERIAL PRIMARY KEY,
	body       TEXT NOT NULL,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)`

	backoff := 2 * time.Second
	const maxBackoff = 30 * time.Second

	for attempt := 1; ; attempt++ {
		attemptCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
		_, err := s.pool.Exec(attemptCtx, ddl)
		cancel()

		if err == nil {
			s.schemaReady.Store(true)
			log.Printf("database ready: table notes present (attempt %d)", attempt)
			return
		}

		if ctx.Err() != nil {
			return // shutting down
		}

		log.Printf("database not ready yet (attempt %d): %v", attempt, err)

		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff < maxBackoff {
			backoff *= 2
		}
	}
}

func (s *server) handleRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	host, _ := os.Hostname()
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	fmt.Fprintf(w, "hello from svc-hello\nhostname: %s\n", host)
}

// handleHealthz backs the readiness probe. When the database is in play it
// reports ready only when the service can actually do its job, because that is
// the second of the two timestamps ADR-0013 says C-07(a) records: the XR going
// Ready, and this probe going green.
func (s *server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")

	if !s.cfg.dbEnabled {
		w.WriteHeader(http.StatusOK)
		io.WriteString(w, "ok (no database configured)\n")
		return
	}

	if !s.schemaReady.Load() {
		w.WriteHeader(http.StatusServiceUnavailable)
		io.WriteString(w, "waiting for the database: schema not created yet\n")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	var one int
	if err := s.pool.QueryRow(ctx, "SELECT 1").Scan(&one); err != nil {
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprintf(w, "database check failed: %v\n", err)
		return
	}

	w.WriteHeader(http.StatusOK)
	io.WriteString(w, "ok\n")
}

// handleNotes is the proof that the credential path works: a write and a read
// through an identity that was never handed a password.
//
//	POST /notes  with a plain-text body -> inserts one row
//	GET  /notes                          -> lists the rows
func (s *server) handleNotes(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")

	if !s.cfg.dbEnabled || !s.schemaReady.Load() {
		w.WriteHeader(http.StatusServiceUnavailable)
		io.WriteString(w, "no database available yet\n")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	switch r.Method {
	case http.MethodPost:
		// 64 KiB is plenty for a note and stops a malformed client from
		// streaming the process out of memory.
		raw, err := io.ReadAll(io.LimitReader(r.Body, 64*1024))
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprintf(w, "reading the request body: %v\n", err)
			return
		}
		body := strings.TrimSpace(string(raw))
		if body == "" {
			w.WriteHeader(http.StatusBadRequest)
			io.WriteString(w, "the request body is the note text; it was empty\n")
			return
		}

		var id int64
		var createdAt time.Time
		// $1 is a bound parameter, so the note text is data and never SQL.
		err = s.pool.QueryRow(ctx,
			"INSERT INTO notes (body) VALUES ($1) RETURNING id, created_at",
			body,
		).Scan(&id, &createdAt)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			fmt.Fprintf(w, "insert failed: %v\n", err)
			return
		}

		w.WriteHeader(http.StatusCreated)
		fmt.Fprintf(w, "note %d written at %s\n", id, createdAt.UTC().Format(time.RFC3339))

	case http.MethodGet:
		rows, err := s.pool.Query(ctx,
			"SELECT id, body, created_at FROM notes ORDER BY id DESC LIMIT 100")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			fmt.Fprintf(w, "query failed: %v\n", err)
			return
		}
		defer rows.Close()

		count := 0
		for rows.Next() {
			var id int64
			var body string
			var createdAt time.Time
			if err := rows.Scan(&id, &body, &createdAt); err != nil {
				fmt.Fprintf(w, "scan failed: %v\n", err)
				return
			}
			fmt.Fprintf(w, "%d\t%s\t%s\n", id, createdAt.UTC().Format(time.RFC3339), body)
			count++
		}
		if err := rows.Err(); err != nil {
			fmt.Fprintf(w, "iteration failed: %v\n", err)
			return
		}
		if count == 0 {
			io.WriteString(w, "no notes yet; POST one to /notes\n")
		}

	default:
		w.Header().Set("Allow", "GET, POST")
		w.WriteHeader(http.StatusMethodNotAllowed)
		io.WriteString(w, "use GET to list notes or POST to write one\n")
	}
}

// logRequests keeps one line per request on stdout. Cloud Logging picks stdout
// up from the node, so this needs no agent and no config — and it is what makes
// the C-07 timings readable after the fact.
func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		// The readiness probe hits /healthz every few seconds; logging it would
		// bury everything else.
		if r.URL.Path != "/healthz" {
			log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(start).Round(time.Millisecond))
		}
	})
}
