// Package systemd reads and drives systemd units with systemctl and journalctl.
package systemd

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
)

// Unit is what `systemctl show` says about a unit.
type Unit struct {
	Name        string
	User        bool   // a user unit (systemctl --user, of the invoking user or the one named in Owner)
	Owner       string // the user whose manager runs it (user units)
	Description string
	Load        string // loaded, not-found, masked …
	Active      string // active, inactive, failed, activating …
	Sub         string // running, exited, dead, waiting …
	Enabled     string // enabled, disabled, static …
	Since       time.Time
	MainPID     int
	MemoryBytes uint64
	Restarts    int
	Result      string
	// For timers: when it fires next and last.
	NextRun time.Time
	LastRun time.Time
}

// Exists reports whether the unit file is on this machine.
func (u Unit) Exists() bool { return u.Load != "" && u.Load != "not-found" }

// Failed reports whether the unit is in the failed state.
func (u Unit) Failed() bool { return u.Active == "failed" }

// Client runs systemctl and journalctl.
type Client struct {
	R run.Runner
}

const props = "Id,Description,LoadState,ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,MainPID,MemoryCurrent,NRestarts,Result,NextElapseUSecRealtime,LastTriggerUSec"

func (c *Client) userArgs(u Unit, args ...string) (string, []string, bool) {
	if !u.User {
		return "systemctl", args, false
	}
	if u.Owner != "" {
		// another user's manager: through systemd's own machine transport (needs root)
		return "systemctl", append([]string{"--user", "-M", u.Owner + "@"}, args...), true
	}
	return "systemctl", append([]string{"--user"}, args...), false
}

// Show reads one unit.
func (c *Client) Show(ctx context.Context, u Unit) (Unit, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	name, args, priv := c.userArgs(u, "show", "--property="+props, "--", u.Name)
	res, err := c.R.Run(ctx, priv, name, args...)
	if err != nil {
		return u, err
	}
	return ParseShow(u, res.Stdout), nil
}

// ParseShow fills u from `systemctl show` output (KEY=value lines).
func ParseShow(u Unit, out string) Unit {
	for _, line := range strings.Split(out, "\n") {
		k, v, ok := strings.Cut(strings.TrimSpace(line), "=")
		if !ok {
			continue
		}
		switch k {
		case "Description":
			u.Description = v
		case "LoadState":
			u.Load = v
		case "ActiveState":
			u.Active = v
		case "SubState":
			u.Sub = v
		case "UnitFileState":
			u.Enabled = v
		case "ActiveEnterTimestamp":
			u.Since = parseTime(v)
		case "MainPID":
			u.MainPID, _ = strconv.Atoi(v)
		case "MemoryCurrent":
			if n, err := strconv.ParseUint(v, 10, 64); err == nil && n < 1<<62 {
				u.MemoryBytes = n
			}
		case "NRestarts":
			u.Restarts, _ = strconv.Atoi(v)
		case "Result":
			u.Result = v
		case "NextElapseUSecRealtime":
			u.NextRun = parseTime(v)
		case "LastTriggerUSec":
			u.LastRun = parseTime(v)
		}
	}
	return u
}

// systemd prints "Thu 2026-11-20 10:31:22 UTC"; empty or "n/a" when unset.
func parseTime(v string) time.Time {
	v = strings.TrimSpace(v)
	if v == "" || v == "n/a" || v == "0" {
		return time.Time{}
	}
	for _, layout := range []string{"Mon 2006-01-02 15:04:05 MST", "Mon 2006-01-02 15:04:05 -0700", "2006-01-02 15:04:05 MST"} {
		if t, err := time.Parse(layout, v); err == nil {
			return t
		}
	}
	return time.Time{}
}

// Verb is something systemctl does to a unit.
type Verb string

const (
	Start   Verb = "start"
	Stop    Verb = "stop"
	Restart Verb = "restart"
	Enable  Verb = "enable"
	Disable Verb = "disable"
	Reset   Verb = "reset-failed"
)

// Do runs a verb on a unit. System units need root; the invoking user's own user units don't.
func (c *Client) Do(ctx context.Context, u Unit, v Verb) error {
	timeout := 3 * time.Minute
	if v == Enable || v == Disable || v == Reset {
		timeout = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	args := []string{string(v)}
	if v == Enable || v == Disable {
		args = append(args, "--now")
	}
	name, args, priv := c.userArgs(u, append(args, "--", u.Name)...)
	_, err := c.R.Run(ctx, priv || !u.User, name, args...)
	if err != nil {
		return fmt.Errorf("systemctl %s %s: %w", v, u.Name, err)
	}
	return nil
}

// Line is the command for v on u, as a person would type it.
func (c *Client) Line(u Unit, v Verb) string {
	name, args, priv := c.userArgs(u, string(v), u.Name)
	argv := append([]string{name}, args...)
	if priv || !u.User {
		argv = append([]string{"sudo"}, argv...)
	}
	return run.Quote(argv)
}

// JournalArgs are journalctl's arguments for a unit's log (or a syslog identifier's, when ident is set).
func JournalArgs(u Unit, ident string, lines int, follow bool) (args []string, priv bool) {
	args = []string{"--no-pager", "-o", "short-iso", "-n", strconv.Itoa(lines)}
	if follow {
		args = append(args, "-f")
	}
	switch {
	case ident != "":
		args = append(args, "-t", ident)
		priv = true
	case u.User && u.Owner == "":
		args = append(args, "--user", "-u", u.Name)
	case u.User:
		args = append(args, "_SYSTEMD_USER_UNIT="+u.Name)
		priv = true
	default:
		args = append(args, "-u", u.Name)
		priv = true
	}
	return args, priv
}
