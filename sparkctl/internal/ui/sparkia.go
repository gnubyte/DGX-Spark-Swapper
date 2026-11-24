package ui

import (
	"context"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/host"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/sparkia"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/systemd"
)

type sparkiaTab struct {
	tbl     Table
	inst    bool
	svcs    []sparkia.Service
	err     error
	env     map[string]string
	release string
	vision  bool
	visAt   string
	timer   systemd.Unit
	updater systemd.Unit
	legacy  systemd.Unit
	loaded  bool
}

type sparkiaMsg struct {
	inst    bool
	svcs    []sparkia.Service
	err     error
	env     map[string]string
	release string
	vision  bool
	visAt   string
	timer   systemd.Unit
	updater systemd.Unit
	legacy  systemd.Unit
}

func newSparkiaTab() *sparkiaTab {
	return &sparkiaTab{tbl: Table{Cols: []Column{
		{Title: "", Width: 2},
		{Title: "SERVICE", Width: 10},
		{Title: "CONTAINER", Flex: 2, Min: 14},
		{Title: "IMAGE", Flex: 3, Min: 16},
		{Title: "STATE", Width: 10},
		{Title: "STATUS", Flex: 2, Min: 12},
	}, Empty: "No Sparkia containers: press U to start the stack."}}
}

func (t *sparkiaTab) title() string { return "Sparkia" }
func (t *sparkiaTab) interval(a *App) time.Duration {
	return time.Duration(a.D.Cfg.RefreshSecs) * time.Second
}
func (t *sparkiaTab) setFilter(f string) { t.tbl.SetFilter(f) }
func (t *sparkiaTab) filterText() string { return t.tbl.Filter }

func (t *sparkiaTab) help() [][2]string {
	return [][2]string{{"enter", "logs"}, {"A", "all logs"}, {"r", "restart"}, {"s", "stop/start"}, {"R", "recreate"},
		{"U", "up -d"}, {"P", "pull"}, {"u", "update now"}, {"t", "pin release"}, {"f", "firewall status"},
		{"F", "firewall live"}, {"e", "shell"}, {"D", "down"}, {"H", "updater log"}}
}

func (t *sparkiaTab) refresh(a *App) tea.Cmd {
	st := a.D.Stack
	if st == nil {
		return nil
	}
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(a.ctx, 20*time.Second)
		defer cancel()
		m := sparkiaMsg{inst: st.Installed()}
		if a.D.Sys != nil {
			m.timer, _ = a.D.Sys.Show(ctx, systemd.Unit{Name: "sparkia-update.timer"})
			m.updater, _ = a.D.Sys.Show(ctx, systemd.Unit{Name: "sparkia-update.service"})
			m.legacy, _ = a.D.Sys.Show(ctx, ownerUnit(systemd.Unit{Name: "vision-server.service", User: true}))
		}
		m.vision, m.visAt = st.VisionUp(ctx)
		if !m.inst {
			return m
		}
		m.svcs, m.err = st.Services(ctx)
		m.env, _ = st.Env()
		m.release = st.Release()
		return m
	}
}

func (t *sparkiaTab) update(a *App, msg tea.Msg) tea.Cmd {
	m, ok := msg.(sparkiaMsg)
	if !ok {
		return nil
	}
	t.loaded = true
	t.inst, t.svcs, t.err, t.env, t.release = m.inst, m.svcs, m.err, m.env, m.release
	t.vision, t.visAt, t.timer, t.updater, t.legacy = m.vision, m.visAt, m.timer, m.updater, m.legacy
	var rows []Row
	for _, s := range m.svcs {
		status := s.Status
		if s.Health != "" {
			status += " · " + s.Health
		}
		rows = append(rows, Row{Key: s.Service, Cells: []string{dot(s.State == "running" && s.Health != "unhealthy",
			s.State == "restarting" || s.Health == "starting"), s.Service, s.Name, s.Image, state(s.State), status}})
	}
	t.tbl.SetRows(rows)
	return nil
}

func (t *sparkiaTab) sel() (sparkia.Service, bool) {
	k := t.tbl.SelectedKey()
	for _, s := range t.svcs {
		if s.Service == k {
			return s, true
		}
	}
	return sparkia.Service{}, false
}

func (t *sparkiaTab) key(a *App, k tea.KeyMsg) (bool, tea.Cmd) {
	st := a.D.Stack
	if st == nil {
		return false, nil
	}
	if handled := tableKeys(&t.tbl, k); handled {
		return true, nil
	}
	do := func(act sparkia.Action, danger bool, body string) tea.Cmd {
		run1 := func() tea.Cmd {
			return a.act(act.Title, st.Line(act), st.Priv, func(ctx context.Context) (string, error) { return st.Do(ctx, act) }, nil)
		}
		if body == "" && !danger {
			return run1()
		}
		return a.confirm(newConfirm(act.Title+"?", body, st.Line(act), danger, run1))
	}
	s, has := t.sel()
	switch k.String() {
	case "enter", "L":
		if !has {
			return true, nil
		}
		return true, a.logs("Sparkia · "+s.Service, st.Priv, "docker", st.LogsArgs(s.Service, a.D.Cfg.LogLines, true)...)
	case "A":
		return true, a.logs("Sparkia · every service", st.Priv, "docker", st.LogsArgs("", a.D.Cfg.LogLines/2, true)...)
	case "r":
		if has {
			return true, do(sparkia.Restart(s.Service), false, "")
		}
	case "s":
		if has {
			if s.State == "running" {
				return true, do(sparkia.Stop(s.Service), true, stopNote(s.Service))
			}
			return true, do(sparkia.Start(s.Service), false, "")
		}
	case "R":
		if has {
			return true, do(sparkia.Recreate(s.Service), false, "Makes a fresh container from the image (its settings re-read from compose.yaml and .env).")
		}
	case "U":
		return true, do(sparkia.Up(), false, "")
	case "P":
		return true, do(sparkia.Pull(), false, "Downloads newer images for the release in .env. U (up -d) then switches the containers over.")
	case "D":
		return true, do(sparkia.Down(), true, "Stops Vision Server, the firewall's traffic log and the guard, and removes their containers. "+
			"Your data stays. U brings them back.")
	case "u":
		u := systemd.Unit{Name: "sparkia-update.service"}
		return true, a.confirm(newConfirm("Update Sparkia now?",
			"Runs the updater: the newest release's deploy files, docker compose pull, then up -d (recreates what changed). "+
				"Its log opens while it runs.", a.D.Sys.Line(u, systemd.Start)+" --no-block", false, func() tea.Cmd {
				return a.act("Start the Sparkia updater", a.D.Sys.Line(u, systemd.Start), true, func(ctx context.Context) (string, error) {
					ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
					defer cancel()
					_, err := a.D.R.Run(ctx, true, "systemctl", "start", "--no-block", "sparkia-update.service")
					return "", err
				}, func() tea.Cmd {
					args, priv := systemd.JournalArgs(u, "", 100, true)
					return a.logs("Sparkia updater", priv && journalNeedsRoot(a), "journalctl", args...)
				})
			}))
	case "H":
		args, priv := systemd.JournalArgs(systemd.Unit{Name: "sparkia-update.service"}, "", a.D.Cfg.LogLines, true)
		return true, a.logs("Sparkia updater's log", priv && journalNeedsRoot(a), "journalctl", args...)
	case "t":
		cur := t.env["SPARKIA_TAG"]
		if cur == "" {
			cur = "latest"
		}
		a.dlg = newInput("Pin the Sparkia release", "SPARKIA_TAG in "+st.Dir+"/.env. A version (0.9.142) stays put; latest follows main again. "+
			"The updater applies it on its next run (u runs it now).", cur, "latest", func(v string) tea.Cmd {
			return a.act("Pin Sparkia to "+v, "set SPARKIA_TAG="+v+" in "+st.Dir+"/.env", true, func(ctx context.Context) (string, error) {
				return "", st.SetTag(ctx, v)
			}, nil)
		})
		a.dlg.(*inputDialog).validate = func(v string) error {
			if !sparkia.ValidTag(v) {
				return errBad("a release tag is letters, digits, dots and dashes (latest, 0.9.142)")
			}
			return nil
		}
		return true, nil
	case "f":
		return true, a.report("Firewall status", "sudo spark-firewall status", true, func(ctx context.Context) (string, error) {
			return st.FirewallStatus(ctx)
		})
	case "F":
		return true, a.handoff("Firewall: live traffic", rootArgv(a, sparkia.FirewallArgv("now")), true)
	case "e":
		if has {
			return true, a.handoff("Shell in "+s.Name, rootArgvIf(a, st.Priv, shellIn(s.Name)), false)
		}
	}
	return false, nil
}

func stopNote(svc string) string {
	switch svc {
	case "firewall":
		return "The rules stay loaded in the kernel, but the traffic log and its API stop until it starts again."
	case "guard":
		return "Endpoint protection stops watching until it starts again."
	case "vision":
		return "Vision Server goes offline for everyone until it starts again."
	}
	return ""
}

func (t *sparkiaTab) view(a *App, w, h int) string {
	st := a.D.Stack
	if st == nil {
		return "\n  " + sMuted.Render("No Sparkia folder is configured (sparkia_dir).")
	}
	if !t.loaded {
		return "\n  " + a.spin.View() + " Reading the Sparkia stack…"
	}
	if !t.inst {
		msg := "\n  " + sWarn.Render("Sparkia isn't installed here") + sMuted.Render(" (no "+st.Dir+"/compose.yaml).") + "\n\n" +
			sMuted.Render("  Install it from a release:  sudo bash deploy/install.sh   (see the Sparkia repo's deploy/README.md)")
		if t.legacy.Exists() {
			msg += "\n\n  " + "The host install's " + sBold.Render("vision-server") + " user service is " + state(t.legacy.Active) + "."
		}
		return msg
	}
	tag := t.env["SPARKIA_TAG"]
	if tag == "" {
		tag = "latest"
	}
	next := "—"
	if !t.timer.NextRun.IsZero() {
		next = "next check " + host.Ago(t.timer.NextRun)
	}
	last := ""
	if t.updater.Active == "activating" {
		last = sWarn.Render("  updating now…")
	} else if t.updater.Result != "" && t.updater.Result != "success" {
		last = sBad.Render("  last update: " + t.updater.Result + " (H for its log)")
	}
	user := t.env["SPARK_USER"]
	head := kv([][2]string{
		{"Folder", st.Dir + sMuted.Render("  runs as "+orDash(user)+" · Vision on :"+orDash(t.env["VISION_PORT"]))},
		{"Release", sBold.Render(orDash(t.release)) + sMuted.Render("  following "+tag)},
		{"Updates", state(t.timer.Active) + sMuted.Render("  "+next) + last},
		{"Vision", dot(t.vision, false) + " " + t.visAt + sMuted.Render("  "+st.VisionURL)},
	})
	if t.err != nil {
		head += "\n" + sBad.Render(firstLine(t.err.Error()))
	}
	hh := lipgloss.Height(head) + 1
	return head + "\n\n" + t.tbl.View(w, max(3, h-hh-1), true)
}

// ── shared helpers for tabs ──

// tableKeys moves a table's cursor; true when the key was one of those.
func tableKeys(t *Table, k tea.KeyMsg) bool {
	switch k.String() {
	case "up", "k":
		t.Move(-1)
	case "down", "j":
		t.Move(1)
	case "pgup", "ctrl+u":
		t.Move(-10)
	case "pgdown", "ctrl+d":
		t.Move(10)
	case "home", "g":
		t.Home()
	case "end", "G":
		t.End()
	default:
		return false
	}
	return true
}

type errBad string

func (e errBad) Error() string { return string(e) }

// rootArgv puts sudo in front of argv unless we're root.
func rootArgv(a *App, argv []string) []string { return rootArgvIf(a, true, argv) }

func rootArgvIf(a *App, priv bool, argv []string) []string {
	if !priv || a.D.Host == nil || a.D.Host.Cached() == run.Root {
		return argv
	}
	return append([]string{"sudo"}, argv...)
}

func shellIn(name string) []string {
	return []string{"docker", "exec", "-it", name, "sh", "-c", "command -v bash >/dev/null && exec bash || exec sh"}
}
