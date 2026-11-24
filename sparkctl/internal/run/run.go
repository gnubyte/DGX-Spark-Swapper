// Package run runs the host's own tools (docker, systemctl, journalctl, …) the way a person would at a shell:
// with a deadline on every call, the output kept, and root obtained through sudo only when the action needs it.
package run

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// Result is what a finished command left behind.
type Result struct {
	Cmd      string
	Stdout   string
	Stderr   string
	Code     int
	Duration time.Duration
}

// Err is a command that ran and failed: its exit code and what it said on stderr.
type Err struct {
	Cmd    string
	Code   int
	Stderr string
}

func (e *Err) Error() string {
	msg := strings.TrimSpace(e.Stderr)
	if msg == "" {
		msg = fmt.Sprintf("exit status %d", e.Code)
	}
	if lines := strings.Split(msg, "\n"); len(lines) > 6 {
		msg = strings.Join(lines[len(lines)-6:], "\n")
	}
	return fmt.Sprintf("%s: %s", e.Cmd, msg)
}

// ErrNotFound: the program isn't installed on this machine.
var ErrNotFound = errors.New("not installed")

// Runner runs programs. The real one execs them; tests use a fake.
type Runner interface {
	// Run runs name with args and waits, at most until ctx is done.
	Run(ctx context.Context, priv bool, name string, args ...string) (Result, error)
	// Stream starts name with args and sends its output (stdout and stderr merged), a line at a time, to lines
	// until ctx is done or it exits; the channel is closed at the end and the returned error says how it ended.
	Stream(ctx context.Context, priv bool, lines chan<- string, name string, args ...string) error
	// Has reports whether a program is on PATH.
	Has(name string) bool
	// Argv is the command line Run would exec (with sudo in front when priv needs it), for an interactive hand-off.
	Argv(priv bool, name string, args ...string) []string
}

// Privilege describes how this process gets root.
type Privilege int

const (
	// Root: we are root already.
	Root Privilege = iota
	// SudoReady: sudo works without asking (NOPASSWD, or a cached sign-in).
	SudoReady
	// SudoAsks: sudo exists but needs the password: the UI hands the terminal to `sudo -v` once.
	SudoAsks
	// NoRoot: no way to become root here.
	NoRoot
)

func (p Privilege) String() string {
	switch p {
	case Root:
		return "root"
	case SudoReady:
		return "sudo"
	case SudoAsks:
		return "sudo (password needed)"
	default:
		return "no root"
	}
}

// Host runs programs on this machine.
type Host struct {
	mu   sync.Mutex
	priv Privilege
	at   time.Time
	// Env is added to every command (LC_ALL=C so tools print what the parsers expect).
	Env []string
}

// NewHost returns a Runner for this machine.
func NewHost() *Host {
	return &Host{Env: []string{"LC_ALL=C", "SYSTEMD_PAGER=", "SYSTEMD_COLORS=0", "DOCKER_CLI_HINTS=false"}}
}

// Privilege reports (and caches for 30 s) how this process gets root.
func (h *Host) Privilege() Privilege {
	h.mu.Lock()
	defer h.mu.Unlock()
	if !h.at.IsZero() && time.Since(h.at) < 30*time.Second {
		return h.priv
	}
	h.priv = detect()
	h.at = time.Now()
	return h.priv
}

// Cached is the privilege known now, without asking sudo again (for drawing the screen).
func (h *Host) Cached() Privilege {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.at.IsZero() {
		if os.Geteuid() == 0 {
			return Root
		}
		return SudoAsks
	}
	return h.priv
}

// Forget drops the cached privilege (after the person signed in to sudo).
func (h *Host) Forget() {
	h.mu.Lock()
	h.at = time.Time{}
	h.mu.Unlock()
}

func detect() Privilege {
	if os.Geteuid() == 0 {
		return Root
	}
	if _, err := exec.LookPath("sudo"); err != nil {
		return NoRoot
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if exec.CommandContext(ctx, "sudo", "-n", "true").Run() == nil {
		return SudoReady
	}
	return SudoAsks
}

func (h *Host) Has(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}

func (h *Host) Argv(priv bool, name string, args ...string) []string {
	argv := append([]string{name}, args...)
	if priv && os.Geteuid() != 0 {
		return append([]string{"sudo", "--preserve-env=LC_ALL"}, argv...)
	}
	return argv
}

// argv for a non-interactive run: sudo must never stop to ask (-n), or the call would hang behind the UI.
func (h *Host) runArgv(priv bool, name string, args ...string) ([]string, error) {
	if _, err := exec.LookPath(name); err != nil {
		return nil, fmt.Errorf("%s: %w", name, ErrNotFound)
	}
	argv := append([]string{name}, args...)
	if !priv || os.Geteuid() == 0 {
		return argv, nil
	}
	switch h.Privilege() {
	case SudoReady, SudoAsks:
		return append([]string{"sudo", "-n", "--preserve-env=LC_ALL"}, argv...), nil
	default:
		return nil, fmt.Errorf("%s needs root, and sudo isn't available: run sparkctl as root", name)
	}
}

func (h *Host) command(ctx context.Context, argv []string) *exec.Cmd {
	c := exec.CommandContext(ctx, argv[0], argv[1:]...)
	c.Env = append(os.Environ(), h.Env...)
	c.Stdin = nil
	c.WaitDelay = 3 * time.Second
	setProcessGroup(c)
	return c
}

func (h *Host) Run(ctx context.Context, priv bool, name string, args ...string) (Result, error) {
	argv, err := h.runArgv(priv, name, args...)
	if err != nil {
		return Result{Cmd: strings.Join(append([]string{name}, args...), " ")}, err
	}
	res := Result{Cmd: strings.Join(argv, " ")}
	var out, errb bytes.Buffer
	c := h.command(ctx, argv)
	c.Stdout, c.Stderr = &limited{w: &out, n: 32 << 20}, &limited{w: &errb, n: 4 << 20}
	start := time.Now()
	err = c.Run()
	res.Duration = time.Since(start)
	res.Stdout, res.Stderr = out.String(), errb.String()
	if err != nil {
		var exit *exec.ExitError
		switch {
		case ctx.Err() == context.DeadlineExceeded:
			return res, fmt.Errorf("%s: timed out after %s", res.Cmd, res.Duration.Round(time.Second))
		case ctx.Err() != nil:
			return res, ctx.Err()
		case errors.As(err, &exit):
			res.Code = exit.ExitCode()
			stderr := res.Stderr
			if priv && strings.Contains(stderr, "a password is required") {
				stderr = "root needed: sign in to sudo first (press S), or run sparkctl with sudo"
			}
			return res, &Err{Cmd: res.Cmd, Code: res.Code, Stderr: stderr}
		default:
			return res, fmt.Errorf("%s: %w", res.Cmd, err)
		}
	}
	return res, nil
}

func (h *Host) Stream(ctx context.Context, priv bool, lines chan<- string, name string, args ...string) error {
	defer close(lines)
	argv, err := h.runArgv(priv, name, args...)
	if err != nil {
		return err
	}
	c := h.command(ctx, argv)
	pr, pw := io.Pipe()
	c.Stdout, c.Stderr = pw, pw
	if err := c.Start(); err != nil {
		return err
	}
	done := make(chan error, 1)
	go func() {
		err := c.Wait()
		pw.Close()
		done <- err
	}()
	sc := bufio.NewScanner(pr)
	sc.Buffer(make([]byte, 64*1024), 1<<20)
	for sc.Scan() {
		select {
		case lines <- sc.Text():
		case <-ctx.Done():
			// keep draining so the process can exit
		}
	}
	err = <-done
	if ctx.Err() != nil {
		return nil
	}
	return err
}

// limited writes at most n bytes and drops the rest (a runaway command can't eat the memory).
type limited struct {
	w io.Writer
	n int64
}

func (l *limited) Write(p []byte) (int, error) {
	if l.n <= 0 {
		return len(p), nil
	}
	q := p
	if int64(len(q)) > l.n {
		q = q[:l.n]
	}
	n, err := l.w.Write(q)
	l.n -= int64(n)
	if err != nil {
		return n, err
	}
	return len(p), nil
}

// Quote renders argv as a shell line a person could paste.
func Quote(argv []string) string {
	out := make([]string, len(argv))
	for i, a := range argv {
		if a == "" || strings.ContainsAny(a, " \t\n'\"$`\\|&;<>()*?[]{}!#~") {
			out[i] = "'" + strings.ReplaceAll(a, "'", `'\''`) + "'"
		} else {
			out[i] = a
		}
	}
	return strings.Join(out, " ")
}
