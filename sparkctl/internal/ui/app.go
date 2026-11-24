// Package ui is sparkctl's terminal interface.
package ui

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/spinner"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/config"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/docker"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/sparkia"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/swapper"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/systemd"
)

// Version is set at build time.
var Version = "dev"

// Deps is what the tabs work with.
type Deps struct {
	Cfg    config.Config
	State  *config.State
	R      run.Runner
	Host   *run.Host // nil in tests: root is then taken as given
	Docker *docker.Client
	Sys    *systemd.Client
	Swap   *swapper.Client
	Stack  *sparkia.Stack
	// SaveState persists State (nil: don't).
	SaveState func()
	// ConfigNote is a problem reading the config, shown once at start.
	ConfigNote string
}

// tab is one screen.
type tab interface {
	title() string
	// refresh fetches the tab's data (nil when nothing to do); interval is how often it wants it.
	refresh(a *App) tea.Cmd
	interval(a *App) time.Duration
	// update takes the tab's own messages (always, even in the background).
	update(a *App, msg tea.Msg) tea.Cmd
	// key handles a key while the tab shows.
	key(a *App, k tea.KeyMsg) (bool, tea.Cmd)
	view(a *App, w, h int) string
	help() [][2]string
}

// filterable tabs narrow their list with /.
type filterable interface {
	setFilter(string)
	filterText() string
}

type level int

const (
	lvInfo level = iota
	lvOK
	lvErr
)

type activity struct {
	At    time.Time
	Title string
	Line  string
	Out   string
	Err   error
	Took  time.Duration
}

// App is the root model.
type App struct {
	D       Deps
	ctx     context.Context
	cancel  context.CancelFunc
	tabs    []tab
	active  int
	w, h    int
	dlg     dialog
	view    *viewer
	views   []*viewer // viewers under the one showing (a log opened from a report)
	spin    spinner.Model
	status  string
	level   level
	statAt  time.Time
	running map[int]string
	seq     int
	history []activity
	last    map[int]time.Time
	busy    map[int]bool
	filter  textinput.Model
	typing  bool
	quit    bool
}

type tickMsg time.Time

type actionDoneMsg struct {
	id    int
	title string
	line  string
	out   string
	err   error
	took  time.Duration
	after func() tea.Cmd
	show  bool
}

type sudoDoneMsg struct {
	err  error
	next func() tea.Cmd
}

type execDoneMsg struct {
	title string
	err   error
}

type statusMsg struct {
	text string
	lv   level
}

// refreshedMsg ends a tab's refresh (so the next can start).
type refreshedMsg struct{ tab int }

// New builds the app.
func New(d Deps) *App {
	ctx, cancel := context.WithCancel(context.Background())
	sp := spinner.New()
	sp.Spinner = spinner.MiniDot
	sp.Style = sAccent
	fi := textinput.New()
	fi.Prompt = "/"
	fi.CharLimit = 120
	a := &App{D: d, ctx: ctx, cancel: cancel, spin: sp, running: map[int]string{}, last: map[int]time.Time{},
		busy: map[int]bool{}, filter: fi}
	a.tabs = []tab{newOverview(), newSwapperTab(), newSparkiaTab(), newDockerTab(), newServicesTab(), newRunTab()}
	if d.State != nil && d.State.LastTab >= 0 && d.State.LastTab < len(a.tabs) {
		a.active = d.State.LastTab
	}
	if d.ConfigNote != "" {
		a.status, a.level, a.statAt = d.ConfigNote, lvErr, time.Now()
	}
	return a
}

// Context is cancelled when the app is shut down (every command it started stops).
func (a *App) Context() context.Context { return a.ctx }

// Shutdown stops everything the app started; call it once the program has returned.
func (a *App) Shutdown() { a.cancel() }

func (a *App) Init() tea.Cmd {
	return tea.Batch(a.spin.Tick, a.refreshTab(a.active, true), tick())
}

func tick() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg { return tickMsg(t) })
}

// refreshTab starts tab i's refresh unless one is running or it isn't due.
func (a *App) refreshTab(i int, force bool) tea.Cmd {
	t := a.tabs[i]
	if a.busy[i] {
		return nil
	}
	if !force && time.Since(a.last[i]) < t.interval(a) {
		return nil
	}
	cmd := t.refresh(a)
	if cmd == nil {
		return nil
	}
	a.busy[i] = true
	a.last[i] = time.Now()
	return tea.Sequence(cmd, func() tea.Msg { return refreshedMsg{tab: i} })
}

// say shows a message in the status line.
func (a *App) say(lv level, format string, args ...any) {
	a.status, a.level, a.statAt = fmt.Sprintf(format, args...), lv, time.Now()
}

// fail shows an error in the status line (sign-in and sudo problems get a hint).
func (a *App) fail(err error) {
	if err == nil {
		return
	}
	msg := err.Error()
	if errors.Is(err, run.ErrNotFound) {
		msg = strings.Replace(msg, "not installed", "isn't installed on this machine", 1)
	}
	a.say(lvErr, "%s", msg)
}

// needsSudo reports whether a root action must first ask for the sudo password.
func (a *App) needsSudo() bool {
	return a.D.Host != nil && a.D.Host.Privilege() == run.SudoAsks
}

// sudoThen hands the terminal to `sudo -v` (the person types their password to sudo itself), then runs next.
func (a *App) sudoThen(next func() tea.Cmd) tea.Cmd {
	c := exec.Command("sudo", "-v", "-p", "[sparkctl] sudo password for %u: ")
	return tea.ExecProcess(c, func(err error) tea.Msg { return sudoDoneMsg{err: err, next: next} })
}

// act runs fn in the background as one named action; priv says it needs root. after (optional) runs when it ends.
func (a *App) act(title, line string, priv bool, fn func(ctx context.Context) (string, error), after func() tea.Cmd) tea.Cmd {
	return a.actWith(false, title, line, priv, fn, after)
}

// report is act whose output opens in a viewer when it's done (a status report, an inspect).
func (a *App) report(title, line string, priv bool, fn func(ctx context.Context) (string, error)) tea.Cmd {
	return a.actWith(true, title, line, priv, fn, nil)
}

func (a *App) actWith(show bool, title, line string, priv bool, fn func(ctx context.Context) (string, error), after func() tea.Cmd) tea.Cmd {
	if priv && a.needsSudo() {
		return a.sudoThen(func() tea.Cmd { return a.actWith(show, title, line, false, fn, after) })
	}
	a.seq++
	id := a.seq
	a.running[id] = title
	ctx := a.ctx
	return func() tea.Msg {
		start := time.Now()
		out, err := fn(ctx)
		return actionDoneMsg{id: id, title: title, line: line, out: out, err: err, took: time.Since(start), after: after, show: show}
	}
}

// handoff gives the terminal to an interactive command (a shell, docker exec -it, top); sparkctl comes back after.
func (a *App) handoff(title string, argv []string, pause bool) tea.Cmd {
	if len(argv) == 0 {
		return nil
	}
	if pause {
		line := run.Quote(argv)
		argv = []string{"sh", "-c", line + `; s=$?; printf '\n\033[2m[exit %s] Press Enter to go back to sparkctl\033[0m' "$s"; read _`}
	}
	c := exec.Command(argv[0], argv[1:]...)
	return tea.ExecProcess(c, func(err error) tea.Msg { return execDoneMsg{title: title, err: err} })
}

// openViewer shows a viewer (over any other).
func (a *App) openViewer(v *viewer, cmd tea.Cmd) tea.Cmd {
	if a.view != nil {
		a.views = append(a.views, a.view)
	}
	a.view = v
	return cmd
}

// logs opens a live log.
func (a *App) logs(title string, priv bool, name string, args ...string) tea.Cmd {
	if priv && a.needsSudo() {
		return a.sudoThen(func() tea.Cmd { return a.logs(title, priv, name, args...) })
	}
	v, cmd := streamViewer(a.ctx, a.D.R, title, priv, name, args...)
	return a.openViewer(v, cmd)
}

// confirm asks before an action.
func (a *App) confirm(d dialog) tea.Cmd {
	a.dlg = d
	return nil
}

func (a *App) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch m := msg.(type) {
	case tea.WindowSizeMsg:
		a.w, a.h = m.Width, m.Height
		return a, nil
	case spinner.TickMsg:
		var cmd tea.Cmd
		a.spin, cmd = a.spin.Update(m)
		return a, cmd
	case tickMsg:
		cmds := []tea.Cmd{tick()}
		if h := a.D.Host; h != nil && time.Time(m).Second()%15 == 0 {
			// re-check sudo in the background, so the badge stays true without stalling a frame
			cmds = append(cmds, func() tea.Msg { h.Privilege(); return nil })
		}
		if c := a.refreshTab(a.active, false); c != nil {
			cmds = append(cmds, c)
		}
		// the Swapper tab keeps watching a job even in the background
		for i, t := range a.tabs {
			if st, ok := t.(*swapperTab); ok && i != a.active && st.watching() {
				if c := a.refreshTab(i, false); c != nil {
					cmds = append(cmds, c)
				}
			}
		}
		if a.status != "" && a.level != lvErr && time.Since(a.statAt) > 10*time.Second {
			a.status = ""
		}
		if a.status != "" && a.level == lvErr && time.Since(a.statAt) > 30*time.Second {
			a.status = ""
		}
		return a, tea.Batch(cmds...)
	case refreshedMsg:
		a.busy[m.tab] = false
		return a, nil
	case actionDoneMsg:
		delete(a.running, m.id)
		a.history = append(a.history, activity{At: time.Now(), Title: m.title, Line: m.line, Out: m.out, Err: m.err, Took: m.took})
		if len(a.history) > 200 {
			a.history = a.history[len(a.history)-200:]
		}
		if m.err != nil {
			a.fail(fmt.Errorf("%s: %w", m.title, m.err))
		} else {
			a.say(lvOK, "✓ %s (%s)", m.title, m.took.Round(100*time.Millisecond))
		}
		cmds := []tea.Cmd{a.refreshTab(a.active, true)}
		if m.show && m.err == nil {
			out := m.out
			if strings.TrimSpace(out) == "" {
				out = "(nothing)"
			}
			v := textViewer(m.title, out)
			v.source = m.line
			cmds = append(cmds, a.openViewer(v, nil))
		}
		if m.after != nil {
			cmds = append(cmds, m.after())
		}
		return a, tea.Batch(cmds...)
	case sudoDoneMsg:
		if a.D.Host != nil {
			a.D.Host.Forget()
		}
		if m.err != nil {
			a.say(lvErr, "sudo sign-in didn't work: %v", m.err)
			return a, nil
		}
		if a.D.Host != nil && a.D.Host.Privilege() == run.SudoAsks {
			a.say(lvErr, "sudo still asks for a password (is this user allowed to use sudo?)")
			return a, nil
		}
		a.say(lvOK, "Signed in to sudo.")
		if m.next != nil {
			return a, m.next()
		}
		return a, a.refreshTab(a.active, true)
	case execDoneMsg:
		if m.err != nil {
			a.say(lvErr, "%s: %v", m.title, m.err)
		}
		return a, a.refreshTab(a.active, true)
	case statusMsg:
		a.say(m.lv, "%s", m.text)
		return a, nil
	case streamMsg:
		return a, a.onStream(m)
	case tea.KeyMsg:
		return a, a.onKey(m)
	case tea.MouseMsg:
		return a, nil
	}
	// a tab's own message
	var cmds []tea.Cmd
	for _, t := range a.tabs {
		if c := t.update(a, msg); c != nil {
			cmds = append(cmds, c)
		}
	}
	return a, tea.Batch(cmds...)
}

func (a *App) onStream(m streamMsg) tea.Cmd {
	all := append([]*viewer{a.view}, a.views...)
	for _, v := range all {
		if v == nil || v.id != m.id {
			continue
		}
		v.add(m.lines)
		if m.done {
			switch {
			case m.err != nil:
				v.ended = "ended: " + m.err.Error()
			default:
				v.ended = "ended"
			}
			return nil
		}
		return waitStream(m.next)
	}
	// its viewer is gone: stop reading (the context was cancelled when it closed)
	if !m.done {
		return waitStream(m.next)
	}
	return nil
}

func (a *App) onKey(k tea.KeyMsg) tea.Cmd {
	if k.String() == "ctrl+c" && a.dlg == nil && a.view == nil && !a.typing {
		return a.exit()
	}
	if a.dlg != nil {
		cur := a.dlg
		done, cmd := cur.Update(k)
		// a dialog may open the next one (user name, then password): only close the one that finished
		if done && a.dlg == cur {
			a.dlg = nil
		}
		return cmd
	}
	if a.view != nil {
		done, cmd := a.view.Update(k, a.w, a.bodyHeight()+2)
		if done {
			a.view = nil
			if n := len(a.views); n > 0 {
				a.view, a.views = a.views[n-1], a.views[:n-1]
			}
		}
		return cmd
	}
	if a.typing {
		f, _ := a.tabs[a.active].(filterable)
		switch k.String() {
		case "esc":
			a.typing = false
			a.filter.Blur()
			if f != nil {
				f.setFilter("")
			}
			return nil
		case "enter":
			a.typing = false
			a.filter.Blur()
			return nil
		}
		var cmd tea.Cmd
		a.filter, cmd = a.filter.Update(k)
		if f != nil {
			f.setFilter(a.filter.Value())
		}
		return cmd
	}
	t := a.tabs[a.active]
	if handled, cmd := t.key(a, k); handled {
		return cmd
	}
	switch k.String() {
	case "q":
		return a.exit()
	case "tab", "right", "l":
		return a.switchTab((a.active + 1) % len(a.tabs))
	case "shift+tab", "left", "h":
		return a.switchTab((a.active + len(a.tabs) - 1) % len(a.tabs))
	case "1", "2", "3", "4", "5", "6", "7", "8", "9":
		n, _ := strconv.Atoi(k.String())
		if n-1 < len(a.tabs) {
			return a.switchTab(n - 1)
		}
	case "r", "f5", "ctrl+r":
		a.say(lvInfo, "Refreshing…")
		return a.refreshTab(a.active, true)
	case "?":
		a.dlg = &textDialog{title: "Keys", text: a.helpText()}
	case "/":
		if f, ok := t.(filterable); ok {
			a.typing = true
			a.filter.SetValue(f.filterText())
			a.filter.CursorEnd()
			return a.filter.Focus()
		}
	case "esc":
		if f, ok := t.(filterable); ok && f.filterText() != "" {
			f.setFilter("")
		}
	case "S":
		if a.D.Host == nil || a.D.Host.Privilege() == run.Root {
			a.say(lvInfo, "Already root.")
			return nil
		}
		return a.sudoThen(nil)
	case "!":
		sh := os.Getenv("SHELL")
		if sh == "" {
			sh = "/bin/bash"
		}
		return a.handoff("Shell", []string{sh}, false)
	case ":":
		a.dlg = newInput("Run a command", "Runs in your terminal; sparkctl comes back when it ends. Prefix with sudo for root.",
			"", "e.g. docker compose -f /opt/sparkia/compose.yaml ps", func(v string) tea.Cmd {
				v = strings.TrimSpace(v)
				if v == "" {
					return nil
				}
				return a.runLine(v, false)
			})
	}
	return nil
}

// runLine runs a shell line interactively.
func (a *App) runLine(line string, root bool) tea.Cmd {
	argv := []string{"sh", "-c", line}
	if root && os.Geteuid() != 0 {
		argv = append([]string{"sudo"}, argv...)
	}
	a.history = append(a.history, activity{At: time.Now(), Title: "Ran in the terminal", Line: line})
	return a.handoff(line, argv, true)
}

func (a *App) switchTab(i int) tea.Cmd {
	a.active = i
	if a.D.State != nil {
		a.D.State.LastTab = i
	}
	return a.refreshTab(i, true)
}

func (a *App) exit() tea.Cmd {
	a.quit = true
	if a.view != nil {
		a.view.close()
	}
	for _, v := range a.views {
		v.close()
	}
	// a.cancel() runs after the program has stopped (Shutdown): cancelling under a running program stalls its exit
	if a.D.SaveState != nil {
		a.D.SaveState()
	}
	return tea.Quit
}

func (a *App) helpText() string {
	var b strings.Builder
	section := func(title string, keys [][2]string) {
		b.WriteString(sBold.Render(title) + "\n")
		for _, k := range keys {
			b.WriteString(fmt.Sprintf("  %s  %s\n", sKey.Render(fmt.Sprintf("%-10s", k[0])), k[1]))
		}
		b.WriteString("\n")
	}
	section(a.tabs[a.active].title(), a.tabs[a.active].help())
	section("Everywhere", [][2]string{
		{"1-6 tab", "switch screens (←→ too)"},
		{"↑↓ pgup/dn", "move"},
		{"/", "filter the list (esc clears)"},
		{"r", "refresh now"},
		{":", "run any command in the terminal"},
		{"!", "open a shell (exit to come back)"},
		{"S", "sign in to sudo (root actions)"},
		{"?", "these keys"},
		{"q", "quit"},
	})
	section("Log views", [][2]string{{"f", "follow / pause"}, {"/", "search"}, {"n N", "next / previous match"},
		{"F", "show only matching lines"}, {"w", "wrap long lines"}, {"g G", "top / end"}, {"s", "save to ~/sparkctl-logs"},
		{"esc", "back"}})
	return b.String()
}

func (a *App) bodyHeight() int { return max(1, a.h-3) }

func (a *App) View() string {
	if a.quit {
		return ""
	}
	if a.w == 0 {
		return "Starting sparkctl…"
	}
	if a.w < 50 || a.h < 12 {
		msg := "sparkctl needs a terminal of at least 50×12 (this one is " + itoa(a.w) + "×" + itoa(a.h) + ")."
		return fit(lipgloss.NewStyle().Width(max(a.w, 1)).Render(msg), max(a.w, 1), max(a.h, 1))
	}
	header := a.header()
	var body string
	if a.view != nil {
		body = a.view.View(a.w, a.bodyHeight()+1)
		return lipgloss.JoinVertical(lipgloss.Left, header, body)
	}
	body = a.tabs[a.active].view(a, a.w, a.bodyHeight())
	body = fit(body, a.w, a.bodyHeight())
	if a.dlg != nil {
		body = overlay(body, a.dlg.View(a.w), a.w, a.bodyHeight())
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, body, a.footer())
}

func (a *App) header() string {
	var tabs []string
	for i, t := range a.tabs {
		label := itoa(i+1) + " " + t.title()
		if i == a.active {
			tabs = append(tabs, sTabOn.Render(label))
		} else {
			tabs = append(tabs, sTabOff.Render(label))
		}
	}
	left := sTitle.Render(" sparkctl ") + strings.Join(tabs, "")
	var badges []string
	if a.D.Host != nil {
		switch p := a.D.Host.Cached(); p {
		case run.Root:
			badges = append(badges, sWarn.Render("root"))
		case run.SudoReady:
			badges = append(badges, sGood.Render("sudo ✓"))
		case run.SudoAsks:
			badges = append(badges, sMuted.Render("sudo: S to sign in"))
		default:
			badges = append(badges, sBad.Render("no root"))
		}
	}
	host, _ := os.Hostname()
	badges = append(badges, sMuted.Render(host))
	right := strings.Join(badges, sMuted.Render(" · ")) + " "
	gap := a.w - lipgloss.Width(left) - lipgloss.Width(right)
	if gap < 1 {
		return ansi.Truncate(left, a.w, "…")
	}
	return left + strings.Repeat(" ", gap) + right
}

func (a *App) footer() string {
	var left string
	if a.typing {
		left = a.filter.View()
	} else if len(a.running) > 0 {
		ids := make([]int, 0, len(a.running))
		for id := range a.running {
			ids = append(ids, id)
		}
		sort.Ints(ids)
		names := make([]string, 0, len(ids))
		for _, id := range ids {
			names = append(names, a.running[id])
		}
		left = a.spin.View() + " " + strings.Join(names, ", ") + "…"
	} else if a.status != "" {
		switch a.level {
		case lvErr:
			left = sBad.Render("✗ " + firstLine(a.status))
		case lvOK:
			left = sGood.Render(a.status)
		default:
			left = sAccent.Render(a.status)
		}
	} else {
		left = keyHelp(a.tabs[a.active].help())
	}
	right := sMuted.Render(" ? keys  q quit")
	left = ansi.Truncate(left, max(1, a.w-lipgloss.Width(right)-1), "…")
	gap := max(1, a.w-lipgloss.Width(left)-lipgloss.Width(right))
	return sBar.Width(a.w).Render(left + strings.Repeat(" ", gap) + right)
}

// ── helpers ──

func keyHelp(keys [][2]string) string {
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, sKey.Render(k[0])+" "+sMuted.Render(k[1]))
	}
	return strings.Join(parts, "  ")
}

func itoa(n int) string { return strconv.Itoa(n) }

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		rest := strings.TrimSpace(s[i+1:])
		if rest != "" {
			return s[:i] + " " + sMuted.Render("(+more: see Run ▸ history)")
		}
		return s[:i]
	}
	return s
}

// fit pads or cuts s to exactly w×h.
func fit(s string, w, h int) string {
	lines := strings.Split(s, "\n")
	if len(lines) > h {
		lines = lines[:h]
	}
	for i, l := range lines {
		if lipgloss.Width(l) > w {
			lines[i] = ansi.Truncate(l, w, "…")
		}
	}
	for len(lines) < h {
		lines = append(lines, "")
	}
	return strings.Join(lines, "\n")
}

// overlay draws box centred over bg.
func overlay(bg, box string, w, h int) string {
	bl := strings.Split(bg, "\n")
	ol := strings.Split(box, "\n")
	bw := 0
	for _, l := range ol {
		bw = max(bw, lipgloss.Width(l))
	}
	top := max(0, (h-len(ol))/2)
	left := max(0, (w-bw)/2)
	for i, l := range ol {
		y := top + i
		if y >= len(bl) {
			break
		}
		row := bl[y]
		pre := ansi.Truncate(row, left, "")
		pre += strings.Repeat(" ", max(0, left-lipgloss.Width(pre)))
		post := ansi.TruncateLeft(row, left+lipgloss.Width(l), "")
		bl[y] = pre + l + post
	}
	return strings.Join(bl, "\n")
}

// bar draws a usage bar: [■■■■■□□□□□] 52%.
func bar(pct float64, w int) string {
	if w < 4 {
		w = 4
	}
	n := int(pct / 100 * float64(w))
	n = min(max(n, 0), w)
	st := sGood
	if pct >= 90 {
		st = sBad
	} else if pct >= 75 {
		st = sWarn
	}
	return st.Render(strings.Repeat("█", n)) + sMuted.Render(strings.Repeat("░", w-n))
}

// panel draws a titled box of width w.
func panel(title, body string, w int) string {
	inner := max(10, w-4)
	content := sTitle.Render(title) + "\n" + body
	lines := strings.Split(content, "\n")
	for i, l := range lines {
		if lipgloss.Width(l) > inner {
			lines[i] = ansi.Truncate(l, inner, "…")
		}
	}
	return sPanel.Width(w - 2).Render(strings.Join(lines, "\n"))
}

// kv renders "label  value" rows with the labels aligned.
func kv(rows [][2]string) string {
	wl := 0
	for _, r := range rows {
		wl = max(wl, lipgloss.Width(r[0]))
	}
	var b strings.Builder
	for i, r := range rows {
		if i > 0 {
			b.WriteString("\n")
		}
		b.WriteString(sMuted.Render(r[0]) + strings.Repeat(" ", wl-lipgloss.Width(r[0])+2) + r[1])
	}
	return b.String()
}
