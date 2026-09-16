module github.com/platform-factory/svc-hello

// pgx v5.11.0 declares `go 1.25.0` in its own go.mod, so this module cannot ask
// for less. Verified 2026-09-16 against
// https://raw.githubusercontent.com/jackc/pgx/v5.11.0/go.mod.
go 1.25.0

require github.com/jackc/pgx/v5 v5.11.0

require (
	github.com/jackc/pgpassfile v1.0.0 // indirect
	github.com/jackc/pgservicefile v0.0.0-20240606120523-5a60cdf6a761 // indirect
	github.com/jackc/puddle/v2 v2.2.2 // indirect
	golang.org/x/sync v0.17.0 // indirect
	golang.org/x/text v0.29.0 // indirect
)
