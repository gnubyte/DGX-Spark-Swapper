package ui

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/host"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/swapper"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/systemd"
)

type swapperTab struct {
	st      *swapper.Status
	sess    *swapper.Session
	err     error
	tbl     Table
	jobView *viewer
	jobID   string
	pending *swapper.Job // a job just started, until the status shows it
	at      time.Time
}

type swapperMsg struct {
	st   *swapper.Status
	sess *swapper.Session
	err  error
}

func newSwapperTab() *swapperTab {
	return &swapperTab{tbl: Table{Cols: []Column{
		{Title: "", Width: 2},
		{Title: "MODEL", Flex: 3, Min: 18},
		{Title: "WINDOW", Width: 7},
		{Title: "ENGINE", Flex: 3, Min: 10},
		{Title: "TOK/S", Width: 6, Right: true},
		{Title: "STATE", Width: 14},
	}, Empty: "No models."}}
}

func (t *swapperTab) title() string { return "Swapper" }

// watching: a job is running, so the tab polls every second (even in the background).
func (t *swapperTab) watching() bool {
	return t.st != nil && ((t.st.Job != nil && t.st.Job.Running()) || (t.st.InstallJob != nil && t.st.InstallJob.Running()))
}

func (t *swapperTab) interval(a *App) time.Duration {
	if t.watching() || (t.jobView != nil && a.view == t.jobView) {
		return time.Second
	}
	return time.Duration(a.D.Cfg.RefreshSecs) * time.Second
}

func (t *swapperTab) setFilter(f string) { t.tbl.SetFilter(f) }
func (t *swapperTab) filterText() string { return t.tbl.Filter }

func (t *swapperTab) help() [][2]string {
	if t.st == nil {
		return [][2]string{{"enter", "sign in"}, {"L", "Swapper's log"}, {"T", "trust its new certificate"}}
	}
	return [][2]string{{"enter", "swap to"}, {"o", "options"}, {"i", "install"}, {"j", "job log"}, {"c", "cancel job"},
		{"m", "speed check"}, {"p", "pictures"}, {"d", "delete"}, {"a", "activity"}, {"L", "service log"},
		{"R", "restart service"}, {"u", "updates"}, {"w", "connect info"}, {"O", "sign out"}}
}

func (t *swapperTab) refresh(a *App) tea.Cmd {
	c := a.D.Swap
	if c == nil {
		return nil
	}
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(a.ctx, 15*time.Second)
		defer cancel()
		st, err := c.Status(ctx)
		if err == nil {
			return swapperMsg{st: st}
		}
		m := swapperMsg{err: err}
		if errors.Is(err, swapper.ErrSignIn) {
			if s, serr := c.Session(ctx); serr == nil {
				m.sess = &s
			}
		}
		return m
	}
}

func (t *swapperTab) update(a *App, msg tea.Msg) tea.Cmd {
	m, ok := msg.(swapperMsg)
	if !ok {
		return nil
	}
	t.err, t.sess, t.at = m.err, m.sess, time.Now()
	prevJob := ""
	if t.st != nil && t.st.Job != nil && t.st.Job.Running() {
		prevJob = t.st.Job.ID
	}
	if m.err != nil {
		if errors.Is(m.err, swapper.ErrSignIn) {
			t.st = nil
		}
		return nil
	}
	t.st = m.st
	var rows []Row
	for _, md := range m.st.Sorted() {
		rows = append(rows, Row{Key: md.Key, Cells: []string{modelDot(m.st, md), md.Title, ctxLabel(md.Context), md.Engine,
			perfCell(md), modelState(m.st, md)}})
	}
	t.tbl.SetRows(rows)
	// a job that just ended: say how
	if prevJob != "" && m.st.Job != nil && m.st.Job.ID == prevJob && !m.st.Job.Running() {
		if m.st.Job.State == "failed" {
			a.say(lvErr, "%s failed: %s", jobTitle(m.st, m.st.Job), firstLine(m.st.Job.Error))
		} else {
			a.say(lvOK, "✓ %s finished", jobTitle(m.st, m.st.Job))
		}
	}
	// keep the open job log current
	if t.jobView != nil {
		if j := t.followedJob(); j != nil {
			t.jobView.lines = jobLines(m.st, j)
			if !j.Running() {
				t.jobView.ended = "job " + j.State
			}
		}
	}
	return nil
}

// followedJob is the job the log view shows.
func (t *swapperTab) followedJob() *swapper.Job {
	if t.st == nil {
		return nil
	}
	for _, j := range []*swapper.Job{t.st.Job, t.st.InstallJob} {
		if j != nil && j.ID == t.jobID {
			return j
		}
	}
	if t.pending != nil && t.pending.ID == t.jobID {
		return t.pending
	}
	return nil
}

func (t *swapperTab) selected() (swapper.Model, bool) {
	if t.st == nil {
		return swapper.Model{}, false
	}
	k := t.tbl.SelectedKey()
	m, ok := t.st.Models[k]
	return m, ok
}

func (t *swapperTab) key(a *App, k tea.KeyMsg) (bool, tea.Cmd) {
	c := a.D.Swap
	switch k.String() {
	case "up", "k":
		t.tbl.Move(-1)
		return true, nil
	case "down", "j":
		if k.String() == "j" && t.st != nil {
			return true, t.openJob(a)
		}
		t.tbl.Move(1)
		return true, nil
	case "pgup":
		t.tbl.Move(-10)
		return true, nil
	case "pgdown":
		t.tbl.Move(10)
		return true, nil
	case "home", "g":
		t.tbl.Home()
		return true, nil
	case "end", "G":
		t.tbl.End()
		return true, nil
	case "L":
		args, priv := systemd.JournalArgs(systemd.Unit{Name: "spark-swapper.service"}, "", a.D.Cfg.LogLines, true)
		return true, a.logs("Spark Swapper's log", priv && journalNeedsRoot(a), "journalctl", args...)
	case "R":
		u := systemd.Unit{Name: "spark-swapper.service"}
		return true, a.confirm(newConfirm("Restart Spark Swapper?",
			"The web page and this tab reconnect in a few seconds. The serving model keeps running.",
			a.D.Sys.Line(u, systemd.Restart), false, func() tea.Cmd {
				return a.act("Restart Spark Swapper", a.D.Sys.Line(u, systemd.Restart), true,
					func(ctx context.Context) (string, error) { return "", a.D.Sys.Do(ctx, u, systemd.Restart) }, nil)
			}))
	case "T":
		pin := c.TrustSeen()
		if a.D.State != nil {
			a.D.State.SwapperPin = pin
			a.saveState()
		}
		a.say(lvOK, "Trusting the Swapper's certificate %s…", pin[:min(16, len(pin))])
		return true, a.refreshTab(a.active, true)
	}
	if t.st == nil {
		if k.String() == "enter" {
			return true, t.signIn(a)
		}
		return false, nil
	}
	md, has := t.selected()
	switch k.String() {
	case "enter", "s":
		if !has {
			return true, nil
		}
		return true, t.swap(a, md)
	case "o":
		if has {
			return true, t.options(a, md)
		}
	case "i":
		if has {
			return true, t.install(a, md, "")
		}
	case "c":
		if j := t.st.Job; j != nil && j.Running() {
			return true, a.confirm(newConfirm("Cancel "+jobTitle(t.st, j)+"?",
				"The Swapper stops at the next safe point and puts the previous model back if it can.", "", true,
				func() tea.Cmd {
					return a.act("Cancel the job", "POST /api/swap/cancel", false, func(ctx context.Context) (string, error) {
						return "", c.Cancel(ctx)
					}, nil)
				}))
		}
		a.say(lvInfo, "No swap or settings job is running.")
		return true, nil
	case "m":
		if t.st.Active == "" {
			a.say(lvInfo, "Nothing is serving to measure.")
			return true, nil
		}
		return true, a.act("Speed check on "+t.st.Models[t.st.Active].Title, "POST /api/measure", false,
			func(ctx context.Context) (string, error) { return "", c.Measure(ctx) }, nil)
	case "p":
		if t.st.Pictures == nil || !t.st.Pictures.Installed {
			a.say(lvInfo, "Qwen-Image isn't installed on this Spark.")
			return true, nil
		}
		on := !t.st.Pictures.Enabled
		word := map[bool]string{true: "on", false: "off"}[on]
		return true, a.act("Pictures "+word, "POST /api/pictures", false,
			func(ctx context.Context) (string, error) { return "", c.Pictures(ctx, on) }, nil)
	case "d":
		if !has {
			return true, nil
		}
		if !md.Installed {
			a.say(lvInfo, "%s isn't installed.", md.Title)
			return true, nil
		}
		return true, a.confirm(newConfirm("Delete "+md.Title+"?",
			"Removes its weights, recipe and engine image (what no other model shares). It can be installed again later.",
			"POST /api/models/delete", true, func() tea.Cmd {
				return t.jobAct(a, "Delete "+md.Title, "POST /api/models/delete", true, func(ctx context.Context) (*swapper.Job, error) {
					return c.Delete(ctx, md.Key)
				})
			}).requireTyping(md.Key))
	case "a":
		return true, a.report("Swapper activity", "GET /api/activity", false, func(ctx context.Context) (string, error) {
			lines, err := c.Activity(ctx)
			if err != nil {
				return "", err
			}
			return strings.Join(lines, "\n"), nil
		})
	case "w":
		return true, t.connectInfo(a)
	case "u":
		return true, t.updates(a)
	case "O":
		return true, a.act("Sign out of Spark Swapper", "POST /api/logout", false, func(ctx context.Context) (string, error) {
			return "", c.Logout(ctx)
		}, nil)
	}
	return false, nil
}

func (a *App) saveState() {
	if a.D.SaveState != nil {
		a.D.SaveState()
	}
}

func journalNeedsRoot(a *App) bool {
	// members of adm or systemd-journal read the system journal without root; asking sudo only when needed
	return !inGroup("adm", "systemd-journal")
}

// jobAct runs a call that starts a Swapper job; when it returns one, the job's log opens (open) and is followed.
// The job comes back through the action's end (on the UI's goroutine), never written from the background.
func (t *swapperTab) jobAct(a *App, title, line string, open bool, call func(ctx context.Context) (*swapper.Job, error)) tea.Cmd {
	var job *swapper.Job
	return a.act(title, line, false, func(ctx context.Context) (string, error) {
		j, err := call(ctx)
		job = j
		return "", err
	}, func() tea.Cmd {
		if job == nil {
			return nil
		}
		t.jobID, t.pending = job.ID, job
		if open {
			return t.openJob(a)
		}
		return nil
	})
}

func (t *swapperTab) openJob(a *App) tea.Cmd {
	var j *swapper.Job
	cands := []*swapper.Job{t.st.Job, t.st.InstallJob}
	if t.pending != nil && (t.st.Job == nil || t.st.Job.ID != t.pending.ID) && (t.st.InstallJob == nil || t.st.InstallJob.ID != t.pending.ID) {
		// just started: the next status will have it
		cands = append(cands, t.pending)
	}
	for _, x := range cands {
		if x == nil {
			continue
		}
		if x.ID == t.jobID {
			j = x
			break
		}
		if j == nil || x.Running() || x.Started > j.Started {
			j = x
		}
	}
	if j == nil {
		a.say(lvInfo, "No job has run since the Swapper started.")
		return nil
	}
	t.jobID = j.ID
	v := newViewer(jobTitle(t.st, j))
	v.live = true
	v.lines = jobLines(t.st, j)
	if !j.Running() {
		v.ended = "job " + j.State
	}
	t.jobView = v
	return a.openViewer(v, nil)
}

func jobLines(st *swapper.Status, j *swapper.Job) []string {
	var out []string
	for _, s := range j.Steps {
		mark := map[string]string{"done": sGood.Render("✓"), "running": sWarn.Render("▸"), "failed": sBad.Render("✗"),
			"skipped": sMuted.Render("–")}[s.State]
		if mark == "" {
			mark = sMuted.Render("·")
		}
		line := mark + " " + s.Label
		if s.Detail != "" {
			line += sMuted.Render("  " + firstLine(s.Detail))
		}
		out = append(out, line)
	}
	if len(j.Steps) > 0 {
		out = append(out, "")
	}
	for _, l := range j.Log {
		out = append(out, sMuted.Render(l.At().Format("15:04:05"))+" "+sanitize(l.M))
	}
	if j.Error != "" {
		out = append(out, "", sBad.Render("✗ "+j.Error))
	}
	return out
}

func (t *swapperTab) signIn(a *App) tea.Cmd {
	c := a.D.Swap
	if t.sess != nil && t.sess.SetupNeeded {
		a.say(lvErr, "The Swapper has no admin yet: open %s in a browser once to create it.", c.Base)
		return nil
	}
	user := a.D.Cfg.SwapperUser
	if a.D.State != nil && a.D.State.SwapperUser != "" {
		user = a.D.State.SwapperUser
	}
	a.dlg = newInput("Sign in to Spark Swapper", "Your Swapper login ("+c.Base+").", user, "username", func(u string) tea.Cmd {
		u = strings.TrimSpace(u)
		if u == "" {
			return nil
		}
		a.dlg = newPassword("Password for "+u, "", func(p string) tea.Cmd {
			ok := false
			return a.act("Sign in as "+u, "POST /api/login", false, func(ctx context.Context) (string, error) {
				err := c.Login(ctx, u, p)
				ok = err == nil
				return "", err
			}, func() tea.Cmd {
				if ok && a.D.State != nil {
					a.D.State.SwapperUser = u
					a.saveState()
				}
				return nil
			})
		})
		return nil
	})
	return nil
}

func (t *swapperTab) swap(a *App, md swapper.Model) tea.Cmd {
	c := a.D.Swap
	if md.Key == t.st.Active {
		a.say(lvInfo, "%s is already serving.", md.Title)
		return nil
	}
	if !md.Installed {
		return t.install(a, md, "")
	}
	from := "Nothing is serving now."
	if t.st.Active != "" {
		from = t.st.Models[t.st.Active].Title + " stops first; apps see the new model under its own ID."
	}
	body := from
	if md.ETA != "" {
		body += "\nLoading takes " + md.ETA + "."
	}
	return a.confirm(newConfirm("Swap to "+md.Title+"?", body, "POST /api/swap {target: "+md.Key+"}", false, func() tea.Cmd {
		return t.jobAct(a, "Swap to "+md.Title, "POST /api/swap", true, func(ctx context.Context) (*swapper.Job, error) {
			return c.Swap(ctx, md.Key)
		})
	}))
}

func (t *swapperTab) install(a *App, md swapper.Model, variant string) tea.Cmd {
	c := a.D.Swap
	if md.Installed && variant == "" {
		// a card whose other build isn't installed yet: offer it
		var items []choice
		var variants []string
		seen := map[string]bool{}
		for _, cx := range md.Contexts {
			if cx.Variant != "" && !cx.Installed && !seen[cx.Variant] {
				seen[cx.Variant] = true
				size := ""
				if cx.Install != nil {
					size = fmt.Sprintf(" (~%.0f GB, %s)", cx.Install.SizeGB, cx.Install.Time)
				}
				items = append(items, choice{Label: "Install " + orDash(cx.VariantLabel), Detail: "for the " + cx.Label + " window" + size})
				variants = append(variants, cx.Variant)
			}
		}
		if len(items) == 0 {
			a.say(lvInfo, "%s is installed. (Its Details on the web page have Update recipe…)", md.Title)
			return nil
		}
		a.dlg = newChoice(md.Title+": other builds", "", items, func(i int) tea.Cmd { return t.install(a, md, variants[i]) })
		return nil
	}
	body := ""
	if md.Install != nil {
		body = fmt.Sprintf("About %.0f GB; %s.", md.Install.SizeGB, md.Install.Time)
		if md.Install.Note != "" {
			body += "\n" + md.Install.Note
		}
	}
	what := md.Title
	if variant != "" {
		what += " (" + variant + " build)"
	}
	return a.confirm(newConfirm("Install "+what+"?", body+"\nThe model serving now keeps serving while it installs.",
		"POST /api/models/install", false, func() tea.Cmd {
			return t.jobAct(a, "Start installing "+what, "POST /api/models/install", true, func(ctx context.Context) (*swapper.Job, error) {
				return c.Install(ctx, md.Key, variant)
			})
		}))
}

func (t *swapperTab) options(a *App, md swapper.Model) tea.Cmd {
	c := a.D.Swap
	if len(md.Contexts) == 0 && len(md.Switches) == 0 {
		a.say(lvInfo, "%s has no options.", md.Title)
		return nil
	}
	type pick struct {
		tokens int
		sw     string
		on     bool
	}
	var items []choice
	var picks []pick
	for _, cx := range md.Contexts {
		it := choice{Label: "Window " + cx.Label, Detail: cx.Detail, Mark: cx.Tokens == md.Context}
		if cx.Variant != "" && !cx.Installed {
			it.Disabled = "Needs " + orDash(cx.VariantLabel) + " installed first (press i)."
		}
		items = append(items, it)
		picks = append(picks, pick{tokens: cx.Tokens})
	}
	for _, s := range md.Switches {
		label := s.Label
		state := "off"
		if s.On {
			state = "on"
		}
		it := choice{Label: fmt.Sprintf("%s: %s → turn %s", label, state, map[bool]string{true: "off", false: "on"}[s.On]), Detail: s.Detail}
		if s.Available != nil && !*s.Available && !s.On {
			it.Disabled = s.Why
		}
		items = append(items, it)
		picks = append(picks, pick{sw: s.Name, on: !s.On})
	}
	restart := "It's saved for the next start."
	if md.Key == t.st.Active {
		restart = "It's serving: it restarts with the change (and comes back as it was if it won't start)."
	}
	a.dlg = newChoice(md.Title+": options", restart, items, func(i int) tea.Cmd {
		p := picks[i]
		apply := func() tea.Cmd {
			title := md.Title + ": "
			var sw map[string]bool
			if p.sw != "" {
				sw = map[string]bool{p.sw: p.on}
				title += p.sw + " " + map[bool]string{true: "on", false: "off"}[p.on]
			} else {
				title += "window " + ctxLabel(p.tokens)
			}
			// a serving model restarts (a job, shown); one that isn't just keeps the choice
			return t.jobAct(a, title, "POST /api/settings", true, func(ctx context.Context) (*swapper.Job, error) {
				return c.Settings(ctx, md.Key, p.tokens, sw)
			})
		}
		if p.sw != "" && p.on {
			for _, s := range md.Switches {
				if s.Name == p.sw && s.Warn != "" {
					return a.confirm(newConfirm("Turn on "+s.Label+"?", s.Warn, "", true, apply))
				}
			}
		}
		return apply()
	})
	return nil
}

func (t *swapperTab) connectInfo(a *App) tea.Cmd {
	c := a.D.Swap
	return a.report("Connection info", "GET /api/connect", false, func(ctx context.Context) (string, error) {
		m, err := c.Connect(ctx)
		if err != nil {
			return "", err
		}
		b, _ := json.MarshalIndent(m, "", "  ")
		return string(b), nil
	})
}

func (t *swapperTab) updates(a *App) tea.Cmd {
	c := a.D.Swap
	var info *swapper.Updates
	return a.act("Check for a Swapper update", "POST /api/updates/check", false, func(ctx context.Context) (string, error) {
		u, err := c.CheckUpdates(ctx)
		info = u
		return "", err
	}, func() tea.Cmd {
		if info == nil {
			return nil
		}
		if !info.Available {
			a.say(lvOK, "Spark Swapper %s is the newest.", info.Current)
			return nil
		}
		notes := strings.TrimSpace(info.Notes)
		if len(notes) > 1200 {
			notes = notes[:1200] + "…"
		}
		return a.confirm(newConfirm("Install Spark Swapper "+info.Latest+"?", "Now "+info.Current+". The Swapper restarts; the "+
			"serving model keeps running.\n\n"+notes, "POST /api/updates/install", false, func() tea.Cmd {
			return a.act("Install Swapper "+info.Latest, "POST /api/updates/install", false, func(ctx context.Context) (string, error) {
				return "", c.InstallUpdate(ctx)
			}, nil)
		}))
	})
}

// ── drawing ──

func (t *swapperTab) view(a *App, w, h int) string {
	c := a.D.Swap
	if c == nil {
		return "\n  " + sMuted.Render("No Swapper is configured (swapper_url).")
	}
	if t.st == nil {
		return t.signedOut(a, w)
	}
	var top string
	if j := t.liveJob(); j != nil {
		top = jobPanel(t.st, j, w)
	}
	topH := lipgloss.Height(top)
	if top == "" {
		topH = 0
	}
	sum := t.summary(w)
	listH := h - topH - lipgloss.Height(sum) - 1
	detailW := 0
	if w >= 120 {
		detailW = w * 2 / 5
	}
	tbl := t.tbl.View(w-detailW-1, max(3, listH), true)
	body := tbl
	if detailW > 0 {
		body = lipgloss.JoinHorizontal(lipgloss.Top, tbl, " ", t.details(detailW, listH))
	}
	parts := []string{sum}
	if top != "" {
		parts = append(parts, top)
	}
	parts = append(parts, body)
	return lipgloss.JoinVertical(lipgloss.Left, parts...)
}

// liveJob is the running job, or one that ended in the last minute.
func (t *swapperTab) liveJob() *swapper.Job {
	for _, j := range []*swapper.Job{t.st.Job, t.st.InstallJob} {
		if j == nil {
			continue
		}
		if j.Running() || (!j.FinishedAt().IsZero() && time.Since(j.FinishedAt()) < time.Minute) {
			return j
		}
	}
	return nil
}

func (t *swapperTab) summary(w int) string {
	s := t.st
	serving := sMuted.Render("nothing serving")
	if s.Active != "" {
		md := s.Models[s.Active]
		serving = sGood.Render("● ") + sBold.Render(md.Title) + sMuted.Render(" serving as "+md.ServedID+" at "+ctxLabel(md.ServedContext))
	} else if s.Loading != "" {
		serving = sWarn.Render("◐ loading " + s.Models[s.Loading].Title)
	}
	mem := fmt.Sprintf("%.0f of %.0f GiB free", s.Memory.Available, s.Memory.Total)
	disk := ""
	if s.Disk != nil && s.Disk.Total > 0 {
		disk = "  ·  disk " + host.BytesF(s.Disk.Free) + " free"
	}
	upd := ""
	if s.Update != nil && s.Update.Available {
		upd = sWarn.Render("  ·  update " + s.Update.Latest + " (u)")
	}
	return serving + sMuted.Render("   ·  "+mem+disk+"  ·  v"+s.Version) + upd
}

func (t *swapperTab) signedOut(a *App, w int) string {
	c := a.D.Swap
	var b strings.Builder
	b.WriteString("\n")
	var changed *swapper.ErrCertChanged
	switch {
	case errors.As(t.err, &changed):
		b.WriteString(sBad.Render("  "+changed.Error()) + "\n")
	case errors.Is(t.err, swapper.ErrSignIn):
		if t.sess != nil && t.sess.SetupNeeded {
			b.WriteString("  " + sWarn.Render("This Swapper has no admin yet.") + " Open " + c.Base + " in a browser once to create it.\n")
		} else {
			b.WriteString("  " + sBold.Render("Sign in to Spark Swapper") + sMuted.Render(" — press ") + sKey.Render("enter") + "\n")
		}
		if t.sess != nil {
			b.WriteString("\n" + indent(kv([][2]string{{"Address", c.Base}, {"Version", t.sess.Version},
				{"Machine", t.sess.Hostname + " · " + t.sess.PlatformLabel}}), 2) + "\n")
		}
	case t.err != nil:
		b.WriteString("  " + sBad.Render(firstLine(t.err.Error())) + "\n\n")
		b.WriteString("  " + sMuted.Render("Is it running? ") + sKey.Render("L") + sMuted.Render(" shows its log; tab 5 Services can start it.") + "\n")
	default:
		b.WriteString("  " + a.spin.View() + " Connecting to " + c.Base + "…\n")
	}
	return b.String()
}

func (t *swapperTab) details(w, h int) string {
	md, ok := t.selected()
	if !ok {
		return ""
	}
	rows := [][2]string{{"Engine", md.Engine}, {"Window", ctxLabel(md.Context)}}
	if md.Running {
		rows = append(rows, [2]string{"Serves as", md.ServedID})
	}
	if p := md.Measured; p != nil {
		rows = append(rows, [2]string{"Measured", fmt.Sprintf("%.1f tok/s alone, %.1f across %d", p.Decode, p.DecodeConcurrent, p.Concurrency)})
	} else if p := md.Perf; p != nil {
		rows = append(rows, [2]string{"Speed", fmt.Sprintf("%.1f tok/s alone, %.1f across %d", p.Decode, p.DecodeConcurrent, p.Concurrency)})
	}
	if len(md.Inputs) > 0 {
		rows = append(rows, [2]string{"Reads", strings.Join(md.Inputs, ", ")})
	}
	if !md.Installed {
		if md.Install != nil {
			rows = append(rows, [2]string{"Install", fmt.Sprintf("~%.0f GB · %s", md.Install.SizeGB, md.Install.Time)})
		}
		if md.InstallDetail != "" {
			rows = append(rows, [2]string{"Missing", md.InstallDetail})
		}
	}
	var on []string
	for _, s := range md.Switches {
		if s.On {
			on = append(on, s.Label)
		}
	}
	if len(on) > 0 {
		rows = append(rows, [2]string{"Options on", strings.Join(on, ", ")})
	}
	if md.ETA != "" {
		rows = append(rows, [2]string{"Loads in", md.ETA})
	}
	body := lipgloss.NewStyle().Width(w-4).Render(sMuted.Render(md.Tagline)) + "\n\n" + kv(rows)
	for _, n := range md.Notes {
		body += "\n\n" + lipgloss.NewStyle().Width(w-4).Foreground(cMuted).Render("• "+n)
	}
	return fit(panel(md.Title, body, w), w, h)
}

func jobPanel(st *swapper.Status, j *swapper.Job, w int) string {
	var b strings.Builder
	for _, s := range j.Steps {
		mark := map[string]string{"done": sGood.Render("✓"), "running": sWarn.Render("▸"), "failed": sBad.Render("✗"),
			"skipped": sMuted.Render("–")}[s.State]
		if mark == "" {
			mark = sMuted.Render("·")
		}
		b.WriteString(mark + " " + s.Label + "   ")
	}
	if len(j.Log) > 0 {
		b.WriteString("\n" + sMuted.Render(firstLine(sanitize(j.Log[len(j.Log)-1].M))))
	}
	if j.Error != "" {
		b.WriteString("\n" + sBad.Render(firstLine(j.Error)))
	}
	state := j.State
	if j.CancelRequested && j.Running() {
		state = "cancelling"
	}
	took := ""
	if !j.StartedAt().IsZero() {
		end := time.Now()
		if !j.FinishedAt().IsZero() {
			end = j.FinishedAt()
		}
		took = " · " + end.Sub(j.StartedAt()).Round(time.Second).String()
	}
	return panel(jobTitle(st, j)+"  "+stateWord(state)+sMuted.Render(took+" · j full log · c cancel"), b.String(), w)
}

func stateWord(s string) string {
	switch s {
	case "running", "pending", "cancelling":
		return sWarn.Render(s)
	case "done", "succeeded":
		return sGood.Render(s)
	case "failed", "cancelled":
		return sBad.Render(s)
	}
	return s
}

// jobTitle names a job: "Swap to Qwen3.8 Flash Next", "Install Nemotron 3.5 Bolt".
func jobTitle(st *swapper.Status, j *swapper.Job) string {
	name := func(k string) string {
		if st != nil {
			if m, ok := st.Models[k]; ok {
				return m.Title
			}
		}
		return k
	}
	switch {
	case j.Kind == "swap":
		return "Swap to " + name(j.Target)
	case j.Kind == "settings" || j.Change != "":
		return name(j.Target) + ": " + orDash(j.Change)
	case strings.HasPrefix(j.Kind, "install:model:"):
		k := strings.TrimPrefix(j.Kind, "install:model:")
		k, v, _ := strings.Cut(k, "@")
		if v != "" {
			return "Install " + name(k) + " (" + v + ")"
		}
		return "Install " + name(k)
	case strings.HasPrefix(j.Kind, "install:"):
		return "Provision " + strings.TrimPrefix(j.Kind, "install:")
	case strings.HasPrefix(j.Kind, "delete"):
		return "Delete " + name(j.Target)
	}
	if j.Target != "" {
		return j.Kind + " " + name(j.Target)
	}
	return j.Kind
}

func currentStep(j *swapper.Job) string {
	for _, s := range j.Steps {
		if s.State == "running" {
			return s.Label
		}
	}
	return ""
}

func modelDot(st *swapper.Status, m swapper.Model) string {
	switch {
	case m.Key == st.Active:
		return sGood.Render("●")
	case m.Running:
		return sWarn.Render("◐")
	case m.Installed:
		return sMuted.Render("○")
	default:
		return sMuted.Render("·")
	}
}

func modelState(st *swapper.Status, m swapper.Model) string {
	switch {
	case m.Key == st.Active:
		return sGood.Render("serving")
	case m.Running && !m.Healthy:
		return sWarn.Render("loading")
	case st.Job != nil && st.Job.Running() && st.Job.Target == m.Key:
		return sWarn.Render("swapping")
	case st.InstallJob != nil && st.InstallJob.Running() && strings.Contains(st.InstallJob.Kind, ":"+m.Key):
		return sWarn.Render("installing")
	case m.Installed:
		return "installed"
	default:
		return sMuted.Render("not installed")
	}
}

func perfCell(m swapper.Model) string {
	if m.Measured != nil && m.Measured.Decode > 0 {
		return fmt.Sprintf("%.0f", m.Measured.Decode)
	}
	if m.Perf != nil && m.Perf.Decode > 0 {
		return sMuted.Render(fmt.Sprintf("%.0f", m.Perf.Decode))
	}
	return sMuted.Render("—")
}

func indent(s string, n int) string {
	pad := strings.Repeat(" ", n)
	lines := strings.Split(s, "\n")
	for i := range lines {
		lines[i] = pad + lines[i]
	}
	return strings.Join(lines, "\n")
}

// sortedKeys is a map's keys in order.
func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
