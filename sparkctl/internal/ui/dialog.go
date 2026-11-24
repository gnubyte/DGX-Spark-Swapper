package ui

import (
	"strings"

	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// dialog is a box over the screen that takes the keys until it's done.
type dialog interface {
	// Update handles a key; done closes the dialog.
	Update(msg tea.KeyMsg) (done bool, cmd tea.Cmd)
	View(width int) string
}

// ── confirm ──

type confirmDialog struct {
	title   string
	body    string
	line    string // the command that will run, shown so nothing is hidden
	danger  bool
	typed   string // when set, the person types this to confirm
	input   textinput.Model
	onYes   func() tea.Cmd
	yesWord string
}

func newConfirm(title, body, line string, danger bool, onYes func() tea.Cmd) *confirmDialog {
	return &confirmDialog{title: title, body: body, line: line, danger: danger, onYes: onYes, yesWord: "Yes"}
}

// requireTyping makes the person type word before the action runs (for what can't be undone).
func (d *confirmDialog) requireTyping(word string) *confirmDialog {
	d.typed = word
	d.input = textinput.New()
	d.input.Placeholder = word
	d.input.CharLimit = 200
	d.input.Focus()
	return d
}

func (d *confirmDialog) Update(msg tea.KeyMsg) (bool, tea.Cmd) {
	switch msg.String() {
	case "esc", "ctrl+c":
		return true, nil
	case "n", "N":
		if d.typed == "" {
			return true, nil
		}
	case "y", "Y":
		if d.typed == "" {
			return true, d.onYes()
		}
	case "enter":
		if d.typed == "" || strings.TrimSpace(d.input.Value()) == d.typed {
			return true, d.onYes()
		}
		return false, nil
	}
	if d.typed != "" {
		var cmd tea.Cmd
		d.input, cmd = d.input.Update(msg)
		return false, cmd
	}
	return false, nil
}

func (d *confirmDialog) View(width int) string {
	w := min(max(width-8, 30), 78)
	var b strings.Builder
	title := sTitle
	box := sDialog
	if d.danger {
		title = sBad.Bold(true)
		box = sDanger
	}
	b.WriteString(title.Render(d.title))
	if d.body != "" {
		b.WriteString("\n\n" + lipgloss.NewStyle().Width(w).Render(d.body))
	}
	if d.line != "" {
		b.WriteString("\n\n" + sMuted.Render("Runs: ") + sCode.Render(d.line))
	}
	if d.typed != "" {
		b.WriteString("\n\n" + "Type " + sBold.Render(d.typed) + " to confirm:\n" + d.input.View())
		b.WriteString("\n\n" + sKey.Render("enter") + sMuted.Render(" confirm  ") + sKey.Render("esc") + sMuted.Render(" cancel"))
	} else {
		b.WriteString("\n\n" + sKey.Render("y") + sMuted.Render(" "+strings.ToLower(d.yesWord)+"  ") + sKey.Render("n/esc") + sMuted.Render(" cancel"))
	}
	return box.Width(w + 4).Render(b.String())
}

// ── text input ──

type inputDialog struct {
	title    string
	body     string
	input    textinput.Model
	validate func(string) error
	onSubmit func(string) tea.Cmd
	err      string
}

func newInput(title, body, value, placeholder string, onSubmit func(string) tea.Cmd) *inputDialog {
	in := textinput.New()
	in.SetValue(value)
	in.Placeholder = placeholder
	in.CharLimit = 4096
	in.Focus()
	in.CursorEnd()
	return &inputDialog{title: title, body: body, input: in, onSubmit: onSubmit}
}

func newPassword(title, body string, onSubmit func(string) tea.Cmd) *inputDialog {
	d := newInput(title, body, "", "password", onSubmit)
	d.input.EchoMode = textinput.EchoPassword
	d.input.EchoCharacter = '•'
	return d
}

func (d *inputDialog) Update(msg tea.KeyMsg) (bool, tea.Cmd) {
	switch msg.String() {
	case "esc", "ctrl+c":
		return true, nil
	case "enter":
		v := d.input.Value()
		if d.validate != nil {
			if err := d.validate(v); err != nil {
				d.err = err.Error()
				return false, nil
			}
		}
		return true, d.onSubmit(v)
	}
	d.err = ""
	var cmd tea.Cmd
	d.input, cmd = d.input.Update(msg)
	return false, cmd
}

func (d *inputDialog) View(width int) string {
	w := min(max(width-8, 30), 78)
	d.input.Width = w - 2
	var b strings.Builder
	b.WriteString(sTitle.Render(d.title))
	if d.body != "" {
		b.WriteString("\n\n" + lipgloss.NewStyle().Width(w).Render(d.body))
	}
	b.WriteString("\n\n" + d.input.View())
	if d.err != "" {
		b.WriteString("\n" + sBad.Render(d.err))
	}
	b.WriteString("\n\n" + sKey.Render("enter") + sMuted.Render(" ok  ") + sKey.Render("esc") + sMuted.Render(" cancel"))
	return sDialog.Width(w + 4).Render(b.String())
}

// ── choose one ──

type choice struct {
	Label    string
	Detail   string
	Disabled string // why it can't be picked
	Mark     bool   // shown as the current choice
}

type choiceDialog struct {
	title  string
	body   string
	items  []choice
	cursor int
	onPick func(i int) tea.Cmd
}

func newChoice(title, body string, items []choice, onPick func(i int) tea.Cmd) *choiceDialog {
	d := &choiceDialog{title: title, body: body, items: items, onPick: onPick}
	for i, it := range items {
		if it.Mark {
			d.cursor = i
		}
	}
	return d
}

func (d *choiceDialog) Update(msg tea.KeyMsg) (bool, tea.Cmd) {
	switch msg.String() {
	case "esc", "ctrl+c", "q":
		return true, nil
	case "up", "k":
		if d.cursor > 0 {
			d.cursor--
		}
	case "down", "j":
		if d.cursor < len(d.items)-1 {
			d.cursor++
		}
	case "enter", " ":
		if d.cursor < len(d.items) && d.items[d.cursor].Disabled == "" {
			return true, d.onPick(d.cursor)
		}
	}
	return false, nil
}

func (d *choiceDialog) View(width int) string {
	w := min(max(width-8, 30), 84)
	var b strings.Builder
	b.WriteString(sTitle.Render(d.title))
	if d.body != "" {
		b.WriteString("\n\n" + lipgloss.NewStyle().Width(w).Render(d.body))
	}
	b.WriteString("\n")
	for i, it := range d.items {
		mark := "  "
		if it.Mark {
			mark = sGood.Render("✓ ")
		}
		label := it.Label
		if it.Disabled != "" {
			label = sMuted.Render(label)
		}
		line := mark + label
		if i == d.cursor {
			line = sSel.Render("› ") + line
		} else {
			line = "  " + line
		}
		b.WriteString("\n" + line)
		detail := it.Detail
		if it.Disabled != "" {
			detail = it.Disabled
		}
		if detail != "" && i == d.cursor {
			b.WriteString("\n" + lipgloss.NewStyle().Width(w-4).PaddingLeft(4).Foreground(cMuted).Render(detail))
		}
	}
	b.WriteString("\n\n" + sKey.Render("↑↓") + sMuted.Render(" move  ") + sKey.Render("enter") + sMuted.Render(" choose  ") +
		sKey.Render("esc") + sMuted.Render(" cancel"))
	return sDialog.Width(w + 4).Render(b.String())
}

// ── a block of text (help, a report) ──

type textDialog struct {
	title string
	text  string
	top   int
}

func (d *textDialog) Update(msg tea.KeyMsg) (bool, tea.Cmd) {
	switch msg.String() {
	case "esc", "q", "enter", "ctrl+c", "?":
		return true, nil
	case "down", "j":
		d.top++
	case "up", "k":
		if d.top > 0 {
			d.top--
		}
	}
	return false, nil
}

func (d *textDialog) View(width int) string {
	w := min(max(width-8, 30), 96)
	lines := strings.Split(lipgloss.NewStyle().Width(w).Render(d.text), "\n")
	if d.top > len(lines)-1 {
		d.top = max(0, len(lines)-1)
	}
	lines = lines[d.top:]
	if len(lines) > 34 {
		lines = append(lines[:34], sMuted.Render("… ↓ for more"))
	}
	return sDialog.Width(w + 4).Render(sTitle.Render(d.title) + "\n\n" + strings.Join(lines, "\n") +
		"\n\n" + sKey.Render("esc") + sMuted.Render(" close"))
}
