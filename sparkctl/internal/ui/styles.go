package ui

import "github.com/charmbracelet/lipgloss"

var (
	cAccent  = lipgloss.AdaptiveColor{Light: "#2D5BD7", Dark: "#7AA2F7"}
	cGood    = lipgloss.AdaptiveColor{Light: "#1A7F37", Dark: "#73D98A"}
	cWarn    = lipgloss.AdaptiveColor{Light: "#9A6700", Dark: "#E0AF68"}
	cBad     = lipgloss.AdaptiveColor{Light: "#CF222E", Dark: "#F7768E"}
	cMuted   = lipgloss.AdaptiveColor{Light: "#6E7781", Dark: "#7C8396"}
	cText    = lipgloss.AdaptiveColor{Light: "#1F2328", Dark: "#C0CAF5"}
	cSelBg   = lipgloss.AdaptiveColor{Light: "#DDE6FA", Dark: "#2B3352"}
	cBarBg   = lipgloss.AdaptiveColor{Light: "#E8EAEE", Dark: "#1F2335"}
	cBorder  = lipgloss.AdaptiveColor{Light: "#C9CED6", Dark: "#3B4261"}
	cMatchBg = lipgloss.AdaptiveColor{Light: "#FFF1A8", Dark: "#5A4A12"}

	sTitle  = lipgloss.NewStyle().Bold(true).Foreground(cAccent)
	sBold   = lipgloss.NewStyle().Bold(true)
	sMuted  = lipgloss.NewStyle().Foreground(cMuted)
	sGood   = lipgloss.NewStyle().Foreground(cGood)
	sWarn   = lipgloss.NewStyle().Foreground(cWarn)
	sBad    = lipgloss.NewStyle().Foreground(cBad)
	sAccent = lipgloss.NewStyle().Foreground(cAccent)
	sSel    = lipgloss.NewStyle().Background(cSelBg).Bold(true)
	sHeader = lipgloss.NewStyle().Foreground(cMuted).Bold(true)
	sKey    = lipgloss.NewStyle().Foreground(cAccent).Bold(true)
	sMatch  = lipgloss.NewStyle().Background(cMatchBg)
	sTabOn  = lipgloss.NewStyle().Bold(true).Foreground(cText).Background(cSelBg).Padding(0, 1)
	sTabOff = lipgloss.NewStyle().Foreground(cMuted).Padding(0, 1)
	sBar    = lipgloss.NewStyle().Background(cBarBg).Foreground(cText)
	sPanel  = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(cBorder).Padding(0, 1)
	sDialog = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(cAccent).Padding(1, 2)
	sDanger = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(cBad).Padding(1, 2)
	sCode   = lipgloss.NewStyle().Foreground(cWarn)
)

// state colours a state word: running/active/healthy green, failed/exited red, the rest amber or grey.
func state(s string) string {
	switch s {
	case "running", "active", "healthy", "up", "serving", "enabled", "ok", "done", "succeeded", "listening":
		return sGood.Render(s)
	case "failed", "exited", "dead", "unhealthy", "error", "down", "missing", "not-found", "masked":
		return sBad.Render(s)
	case "inactive", "disabled", "created", "stopped", "static", "—", "", "not installed":
		return sMuted.Render(orDash(s))
	default:
		return sWarn.Render(s)
	}
}

func orDash(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

// dot is a status light.
func dot(ok bool, warn bool) string {
	switch {
	case ok:
		return sGood.Render("●")
	case warn:
		return sWarn.Render("◐")
	default:
		return sBad.Render("●")
	}
}
