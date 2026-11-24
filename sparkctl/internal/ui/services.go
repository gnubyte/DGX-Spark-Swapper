package ui

import (
	"context"
	"os"
	"os/user"
	"time"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/host"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/systemd"
)

type servicesTab struct {
	tbl    Table
	units  []systemd.Unit
	labels map[string]string
	err    error
	loaded bool
}

type servicesMsg struct {
	units []systemd.Unit
	err   error
}

func newServicesTab() *servicesTab {
	return &servicesTab{labels: map[string]string{}, tbl: Table{Cols: []Column{
		{Title: "", Width: 2},
		{Title: "SERVICE", Flex: 2, Min: 18},
		{Title: "UNIT", Flex: 2, Min: 18},
		{Title: "STATE", Width: 18},
		{Title: "AT BOOT", Width: 9},
		{Title: "SINCE", Width: 10},
		{Title: "MEMORY", Width: 8, Right: true},
		{Title: "RESTARTS", Width: 8, Right: true},
	}, Empty: "None of the configured services are on this machine."}}
}

func (t *servicesTab) title() string { return "Services" }
func (t *servicesTab) interval(a *App) time.Duration {
	return time.Duration(a.D.Cfg.RefreshSecs) * time.Second
}
func (t *servicesTab) setFilter(f string) { t.tbl.SetFilter(f) }
func (t *servicesTab) filterText() string { return t.tbl.Filter }

func (t *servicesTab) help() [][2]string {
	return [][2]string{{"enter", "log"}, {"s", "start"}, {"t", "stop"}, {"r", "restart"}, {"e", "enable/disable at boot"},
		{"x", "clear failed"}, {"i", "status"}, {"n", "show any unit"}}
}

// ownerUnit fills in whose user manager runs a user unit when sparkctl runs as root through sudo.
func ownerUnit(u systemd.Unit) systemd.Unit {
	if u.User && u.Owner == "" && os.Geteuid() == 0 {
		if su := os.Getenv("SUDO_USER"); su != "" {
			u.Owner = su
		}
	}
	return u
}

// unitsOf is the units the config names (user units owned by the right person).
func unitsOf(d Deps) []systemd.Unit {
	var us []systemd.Unit
	for _, c := range d.Cfg.Units {
		us = append(us, ownerUnit(systemd.Unit{Name: c.Name, User: c.User, Owner: c.Owner}))
	}
	return us
}

func (t *servicesTab) refresh(a *App) tea.Cmd {
	if a.D.Sys == nil {
		return nil
	}
	for _, c := range a.D.Cfg.Units {
		t.labels[c.Name] = c.Label
	}
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(a.ctx, 20*time.Second)
		defer cancel()
		var m servicesMsg
		for _, u := range unitsOf(a.D) {
			got, err := a.D.Sys.Show(ctx, u)
			if err != nil {
				// a user unit can't be read without that user's session (root logged in directly): skip it quietly
				if !u.User {
					m.err = err
				}
				continue
			}
			if got.Exists() {
				m.units = append(m.units, got)
			}
		}
		return m
	}
}

func (t *servicesTab) update(a *App, msg tea.Msg) tea.Cmd {
	m, ok := msg.(servicesMsg)
	if !ok {
		return nil
	}
	t.loaded, t.err = true, m.err
	t.units = m.units
	var rows []Row
	for _, u := range m.units {
		st := u.Active
		if u.Sub != "" && u.Sub != u.Active {
			st += " (" + u.Sub + ")"
		}
		since := "—"
		if !u.Since.IsZero() && u.Active == "active" {
			since = host.Duration(time.Since(u.Since))
		}
		if !u.NextRun.IsZero() {
			since = "next " + host.Ago(u.NextRun)
		}
		mem := sMuted.Render("—")
		if u.MemoryBytes > 0 {
			mem = host.Bytes(u.MemoryBytes)
		}
		label := t.labels[u.Name]
		if u.User {
			label += sMuted.Render(" (user)")
		}
		restarts := sMuted.Render("0")
		if u.Restarts > 0 {
			restarts = sWarn.Render(itoa(u.Restarts))
		}
		rows = append(rows, Row{Key: u.Name, Cells: []string{dot(u.Active == "active", u.Active == "activating" || u.Active == "reloading"),
			label, u.Name, stateCell(u), state(u.Enabled), since, mem, restarts}})
	}
	t.tbl.SetRows(rows)
	return nil
}

func stateCell(u systemd.Unit) string {
	s := u.Active
	if u.Sub != "" && u.Sub != u.Active {
		s += " · " + u.Sub
	}
	switch u.Active {
	case "active":
		return sGood.Render(s)
	case "failed":
		return sBad.Render(s)
	case "inactive":
		return sMuted.Render(s)
	}
	return sWarn.Render(s)
}

func (t *servicesTab) sel() (systemd.Unit, bool) {
	k := t.tbl.SelectedKey()
	for _, u := range t.units {
		if u.Name == k {
			return u, true
		}
	}
	return systemd.Unit{}, false
}

func (t *servicesTab) key(a *App, k tea.KeyMsg) (bool, tea.Cmd) {
	sys := a.D.Sys
	if sys == nil {
		return false, nil
	}
	if tableKeys(&t.tbl, k) {
		return true, nil
	}
	if k.String() == "n" {
		a.dlg = newInput("Show a unit", "Any systemd unit (add @user for a user unit of yours).", "", "ollama.service",
			func(v string) tea.Cmd {
				if v == "" {
					return nil
				}
				u := systemd.Unit{Name: v}
				if len(v) > 5 && v[len(v)-5:] == "@user" {
					u = ownerUnit(systemd.Unit{Name: v[:len(v)-5], User: true})
				}
				return a.report("systemctl status "+u.Name, "systemctl status "+u.Name, false, func(ctx context.Context) (string, error) {
					return unitStatus(ctx, a, u)
				})
			})
		return true, nil
	}
	u, ok := t.sel()
	if !ok {
		return false, nil
	}
	verb := func(v systemd.Verb, danger bool, body string) tea.Cmd {
		line := sys.Line(u, v)
		go1 := func() tea.Cmd {
			return a.act(string(v)+" "+u.Name, line, !u.User || u.Owner != "", func(ctx context.Context) (string, error) {
				return "", sys.Do(ctx, u, v)
			}, nil)
		}
		if !danger && body == "" {
			return go1()
		}
		return a.confirm(newConfirm(string(v)+" "+u.Name+"?", body, line, danger, go1))
	}
	switch k.String() {
	case "enter", "L":
		args, priv := systemd.JournalArgs(u, "", a.D.Cfg.LogLines, true)
		return true, a.logs(t.labels[u.Name]+" · "+u.Name, priv && journalNeedsRoot(a), "journalctl", args...)
	case "s":
		return true, verb(systemd.Start, false, "")
	case "t":
		return true, verb(systemd.Stop, true, unitNote(u.Name))
	case "r":
		return true, verb(systemd.Restart, unitNote(u.Name) != "", unitNote(u.Name))
	case "e":
		if u.Enabled == "enabled" {
			return true, verb(systemd.Disable, true, "It stops now and won't start at boot.")
		}
		return true, verb(systemd.Enable, false, "It starts now and at every boot.")
	case "x":
		return true, verb(systemd.Reset, false, "")
	case "i":
		return true, a.report("systemctl status "+u.Name, sys.Line(u, "status"), false, func(ctx context.Context) (string, error) {
			return unitStatus(ctx, a, u)
		})
	}
	return false, nil
}

func unitStatus(ctx context.Context, a *App, u systemd.Unit) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	args := []string{"status", "--no-pager", "-l", "-n", "30", "--", u.Name}
	priv := false
	if u.User {
		if u.Owner != "" {
			args = append([]string{"--user", "-M", u.Owner + "@"}, args...)
			priv = true
		} else {
			args = append([]string{"--user"}, args...)
		}
	}
	res, err := a.D.R.Run(ctx, priv, "systemctl", args...)
	// systemctl status exits 3 for a stopped unit: that's still an answer
	if res.Stdout != "" {
		return res.Stdout, nil
	}
	return res.Stdout, err
}

func unitNote(name string) string {
	switch name {
	case "docker.service":
		return "Every container stops: the serving model, Sparkia (Vision Server, firewall log, guard) and anything else."
	case "spark-swapper.service":
		return "The Swapper's page and API go away until it's back; the serving model keeps running."
	case "nginx.service":
		return "Apps reaching the model over HTTPS (:11443) lose it until nginx is back."
	case "spark-firewall.service":
		return "This unit loads the firewall rules at boot; stopping it can flush them."
	}
	return ""
}

// inGroup reports whether this process is in one of the named groups.
func inGroup(names ...string) bool {
	if os.Geteuid() == 0 {
		return true
	}
	gids, err := os.Getgroups()
	if err != nil {
		return false
	}
	for _, n := range names {
		g, err := user.LookupGroup(n)
		if err != nil {
			continue
		}
		for _, id := range gids {
			if itoa(id) == g.Gid {
				return true
			}
		}
	}
	return false
}

func (t *servicesTab) view(a *App, w, h int) string {
	if a.D.Sys == nil {
		return "\n  " + sMuted.Render("systemd isn't available here.")
	}
	if !t.loaded {
		return "\n  " + a.spin.View() + " Asking systemd…"
	}
	head := sMuted.Render("The services Spark Swapper and Sparkia run on. Change the list in /etc/sparkctl/config.json (\"units\").")
	if t.err != nil {
		head = sBad.Render(firstLine(t.err.Error()))
	}
	return head + "\n\n" + t.tbl.View(w, max(3, h-3), true)
}
