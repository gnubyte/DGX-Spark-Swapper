package ui

import (
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// runTab is the escape hatch: saved commands, any command line, and what sparkctl has done this session.
type runTab struct {
	tbl  Table
	htbl Table
	hist bool
}

func newRunTab() *runTab {
	return &runTab{
		tbl: Table{Cols: []Column{
			{Title: "", Width: 2},
			{Title: "COMMAND", Flex: 2, Min: 18},
			{Title: "RUNS", Flex: 4, Min: 20},
		}},
		htbl: Table{Cols: []Column{
			{Title: "", Width: 2},
			{Title: "WHEN", Width: 8},
			{Title: "WHAT", Flex: 2, Min: 16},
			{Title: "COMMAND", Flex: 3, Min: 16},
			{Title: "TOOK", Width: 7, Right: true},
		}, Empty: "Nothing yet: actions you take in sparkctl show here, with what they printed."},
	}
}

func (t *runTab) title() string                 { return "Run" }
func (t *runTab) interval(a *App) time.Duration { return time.Hour }
func (t *runTab) refresh(a *App) tea.Cmd        { t.fill(a); return nil }
func (t *runTab) update(a *App, msg tea.Msg) tea.Cmd {
	if _, ok := msg.(actionDoneMsg); ok {
		t.fill(a)
	}
	return nil
}

func (t *runTab) table() *Table {
	if t.hist {
		return &t.htbl
	}
	return &t.tbl
}
func (t *runTab) setFilter(f string) { t.table().SetFilter(f) }
func (t *runTab) filterText() string { return t.table().Filter }

func (t *runTab) help() [][2]string {
	if t.hist {
		return [][2]string{{"enter", "what it printed"}, {"H", "saved commands"}, {":", "run a command"}}
	}
	return [][2]string{{"enter", "run"}, {"E", "edit, then run"}, {":", "any command"}, {"!", "shell"}, {"H", "history"}}
}

func (t *runTab) fill(a *App) {
	var rows []Row
	for i, c := range a.D.Cfg.Commands {
		mark := ""
		if c.Root {
			mark = sWarn.Render("#")
		}
		rows = append(rows, Row{Key: itoa(i), Cells: []string{mark, c.Name, sCode.Render(c.Cmd)}})
	}
	t.tbl.SetRows(rows)
	var hrows []Row
	for i := len(a.history) - 1; i >= 0; i-- {
		h := a.history[i]
		mark := sGood.Render("✓")
		if h.Err != nil {
			mark = sBad.Render("✗")
		}
		took := ""
		if h.Took > 0 {
			took = h.Took.Round(100 * time.Millisecond).String()
		}
		hrows = append(hrows, Row{Key: itoa(i), Cells: []string{mark, h.At.Format("15:04:05"), h.Title, sMuted.Render(h.Line), took}})
	}
	t.htbl.SetRows(hrows)
}

func (t *runTab) key(a *App, k tea.KeyMsg) (bool, tea.Cmd) {
	if tableKeys(t.table(), k) {
		return true, nil
	}
	switch k.String() {
	case "H":
		t.hist = !t.hist
		t.fill(a)
		return true, nil
	}
	if t.hist {
		if k.String() == "enter" {
			r, ok := t.htbl.Selected()
			if !ok {
				return true, nil
			}
			var i int
			fmt.Sscan(r.Key, &i)
			if i < 0 || i >= len(a.history) {
				return true, nil
			}
			h := a.history[i]
			text := "$ " + h.Line + "\n\n" + h.Out
			if h.Err != nil {
				text += "\n\n✗ " + h.Err.Error()
			}
			return true, a.openViewer(textViewer(h.Title, text), nil)
		}
		return false, nil
	}
	r, ok := t.tbl.Selected()
	if !ok {
		return false, nil
	}
	var i int
	fmt.Sscan(r.Key, &i)
	if i < 0 || i >= len(a.D.Cfg.Commands) {
		return false, nil
	}
	c := a.D.Cfg.Commands[i]
	switch k.String() {
	case "enter":
		return true, a.runLine(c.Cmd, c.Root)
	case "E":
		a.dlg = newInput("Run", "Edit the line, then enter. It runs in your terminal.", c.Cmd, "", func(v string) tea.Cmd {
			v = strings.TrimSpace(v)
			if v == "" {
				return nil
			}
			return a.runLine(v, c.Root)
		})
		return true, nil
	}
	return false, nil
}

func (t *runTab) view(a *App, w, h int) string {
	t.fill(a)
	head := sMuted.Render("Anything you'd type at the prompt. It runs in this terminal; sparkctl comes back when it ends. ") +
		sWarn.Render("#") + sMuted.Render(" runs as root.")
	if t.hist {
		head = sMuted.Render("What sparkctl did this session — enter shows what it printed.")
	}
	tabs := sTabOff.Render("saved commands")
	htab := sTabOff.Render("history (H)")
	if t.hist {
		htab = sTabOn.Render("history")
	} else {
		tabs = sTabOn.Render("saved commands")
	}
	extra := "\n" + sMuted.Render("Add your own to \"commands\" in /etc/sparkctl/config.json or ~/.config/sparkctl/config.json.")
	body := t.table().View(w, max(3, h-5), true)
	return lipgloss.JoinVertical(lipgloss.Left, tabs+" "+htab, head, "", body, extra)
}
