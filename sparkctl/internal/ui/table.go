package ui

import (
	"strings"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"
)

// Column is one table column: a fixed width, or a share of what's left (Flex) with a minimum.
type Column struct {
	Title string
	Width int
	Flex  int
	Min   int
	Right bool
}

// Table is a scrolling, filterable list of rows with a cursor. Cells may carry ANSI styling; the filter matches
// their plain text. Key identifies a row across refreshes, so the cursor stays on the same thing.
type Table struct {
	Cols   []Column
	rows   []Row
	view   []int // indices into rows after the filter
	cursor int
	offset int
	Filter string
	Empty  string
}

// Row is a table row.
type Row struct {
	Key   string
	Cells []string
}

// SetRows replaces the rows, keeping the cursor on the row with the same key.
func (t *Table) SetRows(rows []Row) {
	sel := t.SelectedKey()
	t.rows = rows
	t.apply()
	if sel != "" {
		for i, idx := range t.view {
			if t.rows[idx].Key == sel {
				t.cursor = i
				return
			}
		}
	}
	t.clamp()
}

// SetFilter narrows the rows to those whose text contains f (any case).
func (t *Table) SetFilter(f string) {
	sel := t.SelectedKey()
	t.Filter = f
	t.apply()
	for i, idx := range t.view {
		if t.rows[idx].Key == sel {
			t.cursor = i
			return
		}
	}
	t.cursor = 0
	t.clamp()
}

func (t *Table) apply() {
	t.view = t.view[:0]
	f := strings.ToLower(strings.TrimSpace(t.Filter))
	for i, r := range t.rows {
		if f == "" || strings.Contains(strings.ToLower(ansi.Strip(strings.Join(r.Cells, " "))), f) {
			t.view = append(t.view, i)
		}
	}
}

func (t *Table) clamp() {
	if t.cursor >= len(t.view) {
		t.cursor = len(t.view) - 1
	}
	if t.cursor < 0 {
		t.cursor = 0
	}
}

// Len is how many rows show.
func (t *Table) Len() int { return len(t.view) }

// Selected is the row under the cursor (ok false when there's none).
func (t *Table) Selected() (Row, bool) {
	if t.cursor < 0 || t.cursor >= len(t.view) {
		return Row{}, false
	}
	return t.rows[t.view[t.cursor]], true
}

// SelectedKey is the key of the row under the cursor.
func (t *Table) SelectedKey() string {
	r, ok := t.Selected()
	if !ok {
		return ""
	}
	return r.Key
}

// Select puts the cursor on the row with key.
func (t *Table) Select(key string) {
	for i, idx := range t.view {
		if t.rows[idx].Key == key {
			t.cursor = i
			return
		}
	}
}

// Move moves the cursor by n rows (clamped).
func (t *Table) Move(n int) {
	t.cursor += n
	t.clamp()
}

// Home and End jump to the first and last rows.
func (t *Table) Home() { t.cursor = 0 }
func (t *Table) End()  { t.cursor = len(t.view) - 1; t.clamp() }

// widths lays the columns out in width w (one space between them).
func (t *Table) widths(w int) []int {
	ws := make([]int, len(t.Cols))
	fixed, flex := 0, 0
	for i, c := range t.Cols {
		if c.Flex == 0 {
			ws[i] = c.Width
			fixed += c.Width
		} else {
			flex += c.Flex
		}
	}
	gaps := len(t.Cols) - 1
	left := w - fixed - gaps
	for i, c := range t.Cols {
		if c.Flex > 0 {
			ws[i] = max(c.Min, left*c.Flex/max(flex, 1))
		}
	}
	// too narrow: shrink from the right
	total := gaps
	for _, x := range ws {
		total += x
	}
	for i := len(ws) - 1; total > w && i >= 0; i-- {
		cut := min(total-w, max(ws[i]-3, 0))
		ws[i] -= cut
		total -= cut
	}
	return ws
}

func cell(s string, w int, right bool) string {
	if w <= 0 {
		return ""
	}
	s = strings.ReplaceAll(s, "\n", " ")
	if lipgloss.Width(s) > w {
		s = ansi.Truncate(s, w, "…")
	}
	pad := w - lipgloss.Width(s)
	if right {
		return strings.Repeat(" ", pad) + s
	}
	return s + strings.Repeat(" ", pad)
}

// View renders the table into w×h (header included).
func (t *Table) View(w, h int, focused bool) string {
	ws := t.widths(w)
	var b strings.Builder
	head := make([]string, len(t.Cols))
	for i, c := range t.Cols {
		head[i] = cell(c.Title, ws[i], c.Right)
	}
	b.WriteString(sHeader.Render(strings.Join(head, " ")))
	body := h - 1
	if body < 1 {
		return b.String()
	}
	if len(t.view) == 0 {
		msg := t.Empty
		if t.Filter != "" {
			msg = "Nothing matches “" + t.Filter + "”."
		}
		if msg == "" {
			msg = "Nothing here."
		}
		b.WriteString("\n" + sMuted.Render(msg))
		return b.String()
	}
	if t.cursor < t.offset {
		t.offset = t.cursor
	}
	if t.cursor >= t.offset+body {
		t.offset = t.cursor - body + 1
	}
	if t.offset > max(0, len(t.view)-body) {
		t.offset = max(0, len(t.view)-body)
	}
	for i := t.offset; i < len(t.view) && i < t.offset+body; i++ {
		r := t.rows[t.view[i]]
		parts := make([]string, len(t.Cols))
		for j := range t.Cols {
			v := ""
			if j < len(r.Cells) {
				v = r.Cells[j]
			}
			parts[j] = cell(v, ws[j], t.Cols[j].Right)
		}
		line := strings.Join(parts, " ")
		if i == t.cursor && focused {
			line = sSel.Render(cell(ansi.Strip(line), w, false))
		}
		b.WriteString("\n" + line)
	}
	if len(t.view) > body {
		// a scroll hint on the header's right
		return b.String()
	}
	return b.String()
}

// Position says "12/40" for a footer.
func (t *Table) Position() string {
	if len(t.view) == 0 {
		return "0/0"
	}
	return itoa(t.cursor+1) + "/" + itoa(len(t.view))
}
