// sparkctl: one terminal app for a DGX Spark running Spark Swapper and Sparkia — the models, the Sparkia stack, Docker,
// the services and their logs. Run it with no arguments for the full-screen interface; `sparkctl help` lists the
// commands for scripts.
package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/config"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/docker"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/sparkia"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/swapper"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/systemd"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/ui"
)

// version is set by the build (-ldflags "-X main.version=1.0.0").
var version = "dev"

func main() {
	ui.Version = version
	args := os.Args[1:]
	cmd := ""
	if len(args) > 0 {
		cmd, args = args[0], args[1:]
	}
	if cmd == "-h" || cmd == "--help" {
		cmd = "help"
	}
	if cmd == "-v" || cmd == "--version" {
		cmd = "version"
	}
	if cmd == "" || cmd == "tui" {
		os.Exit(runTUI())
	}
	os.Exit(runCLI(cmd, args))
}

// deps builds what the interface and the commands work with.
func deps() (ui.Deps, error) {
	cfg, cfgErr := config.Load()
	state := config.LoadState()
	h := run.NewHost()
	d := ui.Deps{Cfg: cfg, State: &state, R: h, Host: h}
	if cfgErr != nil {
		d.ConfigNote = "Config: " + cfgErr.Error() + " (using the defaults)"
	}
	var mu sync.Mutex
	d.SaveState = func() {
		mu.Lock()
		defer mu.Unlock()
		_ = state.Save()
	}
	if h.Has("docker") {
		d.Docker = &docker.Client{R: h, Priv: dockerNeedsRoot(h)}
	}
	if h.Has("systemctl") {
		d.Sys = &systemd.Client{R: h}
	}
	if cfg.SwapperURL != "" {
		c := swapper.New(cfg.SwapperURL, state.SwapperSession, state.SwapperPin)
		c.OnSession = func(v string) {
			mu.Lock()
			state.SwapperSession = v
			mu.Unlock()
			d.SaveState()
		}
		c.OnPin = func(fp string) {
			mu.Lock()
			state.SwapperPin = fp
			mu.Unlock()
			d.SaveState()
		}
		d.Swap = c
	}
	priv := d.Docker != nil && d.Docker.Priv
	d.Stack = &sparkia.Stack{R: h, Dir: cfg.SparkiaDir, Priv: priv, VisionURL: cfg.VisionURL}
	return d, cfgErr
}

// dockerNeedsRoot: the docker socket refuses this user (not root, not in the docker group).
func dockerNeedsRoot(h *run.Host) bool {
	if os.Geteuid() == 0 {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	_, err := h.Run(ctx, false, "docker", "version", "--format", "{{.Server.Version}}")
	return err != nil && strings.Contains(strings.ToLower(err.Error()), "permission denied")
}

func runTUI() int {
	d, _ := deps()
	app := ui.New(d)
	p := tea.NewProgram(app, tea.WithAltScreen())
	_, err := p.Run()
	app.Shutdown()
	if err != nil && !errors.Is(err, tea.ErrProgramKilled) && !errors.Is(err, context.Canceled) {
		fmt.Fprintln(os.Stderr, "sparkctl:", err)
		return 1
	}
	return 0
}

func usage() {
	fmt.Print(`sparkctl — manage Spark Swapper, Sparkia, Docker and the services on a DGX Spark.

  sparkctl                  the full-screen interface (? in it lists the keys)
  sparkctl status [--json]  what's serving, Sparkia, Docker and the services, in one look
  sparkctl models           the Swapper's models and their state
  sparkctl swap MODEL       swap the Swapper to MODEL (its key or title) and follow it
  sparkctl login [USER]     sign in to Spark Swapper (kept in ~/.config/sparkctl, readable only by you)
  sparkctl logout           sign out
  sparkctl logs TARGET      follow a log: swapper, sparkia[:SERVICE], updater, docker:CONTAINER, unit:NAME
  sparkctl doctor           check everything sparkctl relies on, and say what's wrong
  sparkctl version

Config: /etc/sparkctl/config.json (machine), ~/.config/sparkctl/config.json (yours). See /usr/share/doc/sparkctl.
`)
}
