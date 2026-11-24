package run

import (
	"context"
	"strings"
	"sync"
)

// Fake is a Runner for tests: each command line (without sudo) maps to a canned result.
type Fake struct {
	mu       sync.Mutex
	Results  map[string]Result
	Errors   map[string]error
	Missing  map[string]bool
	Calls    []string
	Streamed map[string][]string
}

func NewFake() *Fake {
	return &Fake{Results: map[string]Result{}, Errors: map[string]error{}, Missing: map[string]bool{}, Streamed: map[string][]string{}}
}

func key(name string, args []string) string {
	return strings.Join(append([]string{name}, args...), " ")
}

// On sets what a command line returns.
func (f *Fake) On(line, stdout string) *Fake {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Results[line] = Result{Cmd: line, Stdout: stdout}
	return f
}

// Fail makes a command line fail.
func (f *Fake) Fail(line string, err error) *Fake {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Errors[line] = err
	return f
}

func (f *Fake) Run(ctx context.Context, priv bool, name string, args ...string) (Result, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	k := key(name, args)
	f.Calls = append(f.Calls, k)
	if f.Missing[name] {
		return Result{Cmd: k}, ErrNotFound
	}
	if err, ok := f.Errors[k]; ok {
		return Result{Cmd: k}, err
	}
	if r, ok := f.Results[k]; ok {
		return r, nil
	}
	return Result{Cmd: k}, nil
}

func (f *Fake) Stream(ctx context.Context, priv bool, lines chan<- string, name string, args ...string) error {
	defer close(lines)
	f.mu.Lock()
	k := key(name, args)
	f.Calls = append(f.Calls, k)
	out := f.Streamed[k]
	f.mu.Unlock()
	for _, l := range out {
		select {
		case lines <- l:
		case <-ctx.Done():
			return nil
		}
	}
	return nil
}

func (f *Fake) Has(name string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return !f.Missing[name]
}

func (f *Fake) Argv(priv bool, name string, args ...string) []string {
	return append([]string{name}, args...)
}

// Called reports whether a command line ran.
func (f *Fake) Called(line string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, c := range f.Calls {
		if c == line {
			return true
		}
	}
	return false
}
