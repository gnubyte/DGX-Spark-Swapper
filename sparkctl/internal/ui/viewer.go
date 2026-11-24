package ui

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
)

// viewer is a full-screen log or text view: it follows a live stream (docker logs -f, journalctl -f), keeps the last
// maxLines, and searches, filters, wraps and saves.
type viewer struct {
	id      int
	title   string
	source  string // the command it streams, shown in the header
	lines   []string
	follow  bool // stick to the bottom as lines arrive
	wrap    bool
	top     int    // first visible line (of the rendered list)
	search  string // highlighted
	filter  bool   // show only matching lines
	input   textinput.Model
	typing  bool
	status  string
	ended   string
	cancel  context.CancelFunc
	live    bool
	matchAt []int
}

const maxLines = 20000

type streamMsg struct {
	id    int
	lines []string
	done  bool
	err   error
	next  <-chan streamMsg
}

var viewerSeq int

func newViewer(title string) *viewer {
	viewerSeq++
	in := textinput.New()
	in.Prompt = "/"
	in.CharLimit = 200
	return &viewer{id: viewerSeq, title: title, follow: true, input: in}
}

// textViewer shows fixed text.
func textViewer(title, text string) *viewer {
	v := newViewer(title)
	v.lines = strings.Split(strings.TrimRight(text, "\n"), "\n")
	v.follow = false
	return v
}

// streamViewer follows a command's output.
func streamViewer(parent context.Context, r run.Runner, title string, priv bool, name string, args ...string) (*viewer, tea.Cmd) {
	v := newViewer(title)
	v.source = run.Quote(r.Argv(priv, name, args...))
	v.live = true
	ctx, cancel := context.WithCancel(parent)
	v.cancel = cancel
	lines := make(chan string, 1024)
	batches := make(chan streamMsg, 16)
	go func() {
		err := r.Stream(ctx, priv, lines, name, args...)
		batches <- streamMsg{id: v.id, done: true, err: err}
		close(batches)
	}()
	// group lines into batches so a busy log doesn't redraw for every line
	out := make(chan streamMsg, 16)
	go func() {
		defer close(out)
		var buf []string
		tick := time.NewTicker(120 * time.Millisecond)
		defer tick.Stop()
		flush := func() {
			if len(buf) > 0 {
				out <- streamMsg{id: v.id, lines: buf}
				buf = nil
			}
		}
		for {
			select {
			case l, ok := <-lines:
				if !ok {
					lines = nil
					continue
				}
				buf = append(buf, l)
				if len(buf) >= 2000 {
					flush()
				}
			case <-tick.C:
				flush()
			case m, ok := <-batches:
				if !ok {
					return
				}
				// drain what's left before saying it ended
				for lines != nil {
					l, ok := <-lines
					if !ok {
						break
					}
					buf = append(buf, l)
				}
				flush()
				out <- m
				return
			}
		}
	}()
	return v, waitStream(out)
}

func waitStream(ch <-chan streamMsg) tea.Cmd {
	return func() tea.Msg {
		m, ok := <-ch
		if !ok {
			return nil
		}
		m.next = ch
		return m
	}
}

func (v *viewer) close() {
	if v.cancel != nil {
		v.cancel()
	}
}

func (v *viewer) add(lines []string) {
	for _, l := range lines {
		v.lines = append(v.lines, sanitize(l))
	}
	if over := len(v.lines) - maxLines; over > 0 {
		v.lines = append([]string(nil), v.lines[over:]...)
		v.top = max(0, v.top-over)
	}
}

// sanitize keeps a log line's colours but drops what would move the cursor or clear the screen.
var ctlRe = regexp.MustCompile(`\x1b\[[0-9;?]*[ABCDEFGHJKSTfhlnsu]|\x1b\][^\x07]*\x07|\r`)

func sanitize(s string) string {
	s = ctlRe.ReplaceAllString(s, "")
	return strings.ReplaceAll(s, "\t", "    ")
}

// visible is the lines after the filter.
func (v *viewer) visible() []string {
	if !v.filter || v.search == "" {
		return v.lines
	}
	q := strings.ToLower(v.search)
	var out []string
	for _, l := range v.lines {
		if strings.Contains(strings.ToLower(ansi.Strip(l)), q) {
			out = append(out, l)
		}
	}
	return out
}

func (v *viewer) rendered(w int) []string {
	src := v.visible()
	if !v.wrap {
		return src
	}
	var out []string
	for _, l := range src {
		wrapped := ansi.Hardwrap(l, w, true)
		out = append(out, strings.Split(wrapped, "\n")...)
	}
	return out
}

// Update handles keys; done closes the viewer.
func (v *viewer) Update(msg tea.KeyMsg, w, h int) (done bool, cmd tea.Cmd) {
	body := max(1, h-2)
	if v.typing {
		switch msg.String() {
		case "esc":
			v.typing = false
			v.input.Blur()
			return false, nil
		case "enter":
			v.typing = false
			v.input.Blur()
			v.search = strings.TrimSpace(v.input.Value())
			v.follow = false
			v.jump(w, body, 1, true)
			return false, nil
		}
		v.input, cmd = v.input.Update(msg)
		return false, cmd
	}
	lines := v.rendered(w)
	last := max(0, len(lines)-body)
	switch msg.String() {
	case "esc", "q":
		if v.search != "" && msg.String() == "esc" {
			v.search, v.filter = "", false
			return false, nil
		}
		v.close()
		return true, nil
	case "up", "k":
		v.follow = false
		v.top = max(0, v.top-1)
	case "down", "j":
		v.top = min(last, v.top+1)
		v.follow = v.top >= last
	case "pgup", "b", "ctrl+u":
		v.follow = false
		v.top = max(0, v.top-body)
	case "pgdown", " ", "ctrl+d":
		v.top = min(last, v.top+body)
		v.follow = v.top >= last
	case "g", "home":
		v.follow = false
		v.top = 0
	case "G", "end":
		v.follow = true
		v.top = last
	case "f":
		v.follow = !v.follow
		if v.follow {
			v.top = last
		}
	case "w":
		v.wrap = !v.wrap
	case "/":
		v.typing = true
		v.input.SetValue(v.search)
		v.input.CursorEnd()
		return false, v.input.Focus()
	case "n":
		v.jump(w, body, 1, false)
	case "N":
		v.jump(w, body, -1, false)
	case "F":
		if v.search != "" {
			v.filter = !v.filter
			v.top = 0
		}
	case "s":
		v.status = v.save()
	}
	return false, nil
}

// jump moves to the next (dir 1) or previous (-1) line matching the search.
func (v *viewer) jump(w, body, dir int, fromTop bool) {
	if v.search == "" {
		return
	}
	lines := v.rendered(w)
	q := strings.ToLower(v.search)
	start := v.top + dir
	if fromTop {
		start = 0
	}
	for i := 0; i < len(lines); i++ {
		j := (start + dir*i + len(lines)*2) % max(len(lines), 1)
		if strings.Contains(strings.ToLower(ansi.Strip(lines[j])), q) {
			v.top = min(j, max(0, len(lines)-body))
			v.follow = false
			v.status = ""
			return
		}
	}
	v.status = "No line has “" + v.search + "”."
}

func (v *viewer) save() string {
	dir := filepath.Join(os.Getenv("HOME"), "sparkctl-logs")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "Couldn't save: " + err.Error()
	}
	name := regexp.MustCompile(`[^A-Za-z0-9._-]+`).ReplaceAllString(strings.ToLower(v.title), "-")
	path := filepath.Join(dir, fmt.Sprintf("%s-%s.log", strings.Trim(name, "-"), time.Now().Format("20060102-150405")))
	var b strings.Builder
	for _, l := range v.lines {
		b.WriteString(ansi.Strip(l) + "\n")
	}
	if err := os.WriteFile(path, []byte(b.String()), 0o600); err != nil {
		return "Couldn't save: " + err.Error()
	}
	return "Saved " + itoa(len(v.lines)) + " lines to " + path
}

func (v *viewer) View(w, h int) string {
	body := max(1, h-2)
	lines := v.rendered(w)
	last := max(0, len(lines)-body)
	if v.follow {
		v.top = last
	}
	v.top = min(max(0, v.top), last)
	// header
	mode := []string{}
	if v.live && v.ended == "" {
		if v.follow {
			mode = append(mode, sGood.Render("● live"))
		} else {
			mode = append(mode, sWarn.Render("❚❚ paused (f follows)"))
		}
	}
	if v.ended != "" {
		mode = append(mode, sMuted.Render(v.ended))
	}
	if v.wrap {
		mode = append(mode, sMuted.Render("wrap"))
	}
	if v.search != "" {
		s := "search “" + v.search + "”"
		if v.filter {
			s += " (only matches)"
		}
		mode = append(mode, sAccent.Render(s))
	}
	head := sTitle.Render(v.title)
	if v.source != "" {
		head += sMuted.Render("  $ " + v.source)
	}
	right := strings.Join(mode, sMuted.Render(" · "))
	head = ansi.Truncate(head, max(1, w-lipgloss.Width(right)-1), "…")
	head += strings.Repeat(" ", max(1, w-lipgloss.Width(head)-lipgloss.Width(right))) + right

	var b strings.Builder
	b.WriteString(head + "\n")
	q := strings.ToLower(v.search)
	for i := v.top; i < len(lines) && i < v.top+body; i++ {
		l := lines[i]
		if !v.wrap {
			l = ansi.Truncate(l, w, "…")
		}
		if q != "" && strings.Contains(strings.ToLower(ansi.Strip(l)), q) {
			l = highlight(ansi.Strip(l), v.search)
		}
		b.WriteString(l + "\n")
	}
	for i := len(lines) - v.top; i < body; i++ {
		if len(lines) == 0 && i == 0 {
			if v.live && v.ended == "" {
				b.WriteString(sMuted.Render("Waiting for output…") + "\n")
			} else {
				b.WriteString(sMuted.Render("(nothing)") + "\n")
			}
			continue
		}
		b.WriteString("\n")
	}
	// footer
	pos := fmt.Sprintf("%d–%d of %d", min(v.top+1, len(lines)), min(v.top+body, len(lines)), len(lines))
	var foot string
	switch {
	case v.typing:
		foot = v.input.View()
	case v.status != "":
		foot = sAccent.Render(v.status)
	default:
		foot = keyHelp([][2]string{{"esc", "back"}, {"f", "follow"}, {"/", "search"}, {"n/N", "next/prev"}, {"F", "only matches"},
			{"w", "wrap"}, {"g/G", "top/end"}, {"s", "save"}})
	}
	foot = ansi.Truncate(foot, max(1, w-len(pos)-1), "…")
	b.WriteString(foot + strings.Repeat(" ", max(1, w-lipgloss.Width(foot)-len(pos))) + sMuted.Render(pos))
	return b.String()
}

// highlight marks every occurrence of q (any case) in plain text s.
func highlight(s, q string) string {
	if q == "" {
		return s
	}
	lower, lq := strings.ToLower(s), strings.ToLower(q)
	var b strings.Builder
	i := 0
	for {
		j := strings.Index(lower[i:], lq)
		if j < 0 {
			b.WriteString(s[i:])
			break
		}
		b.WriteString(s[i : i+j])
		b.WriteString(sMatch.Render(s[i+j : i+j+len(q)]))
		i += j + len(q)
	}
	return b.String()
}
