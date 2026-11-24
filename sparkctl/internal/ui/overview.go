package ui

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/user"
	"path/filepath"
	"strings"
	"sync"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/docker"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/host"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/sparkia"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/swapper"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/systemd"
)

type overview struct {
	d  *overviewMsg
	at time.Time
}

type overviewMsg struct {
	mem      host.Memory
	memErr   error
	load     host.Load
	uptime   time.Duration
	disks    []host.Disk
	gpus     []host.GPU
	gpuErr   error
	sw       *swapper.Status
	swErr    error
	sparkia  []sparkia.Service
	spErr    error
	spInst   bool
	vision   bool
	visionAt string
	release  string
	tag      string
	cs       []docker.Container
	images   int
	dockErr  error
	units    []systemd.Unit
}

func newOverview() *overview { return &overview{} }

func (o *overview) title() string { return "Overview" }
func (o *overview) interval(a *App) time.Duration {
	return time.Duration(a.D.Cfg.RefreshSecs) * time.Second
}
func (o *overview) help() [][2]string {
	return [][2]string{{"2-6", "Swapper · Sparkia · Docker · Services · Run"}, {"r", "refresh"}, {":", "run a command"}}
}

// homeDir is the Spark user's home (the one who ran sudo, when sparkctl runs as root).
func homeDir() string {
	if su := os.Getenv("SUDO_USER"); su != "" && os.Geteuid() == 0 {
		if u, err := user.Lookup(su); err == nil {
			return u.HomeDir
		}
	}
	h, _ := os.UserHomeDir()
	return h
}

func (o *overview) refresh(a *App) tea.Cmd {
	d := a.D
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(a.ctx, 20*time.Second)
		defer cancel()
		m := &overviewMsg{}
		var wg sync.WaitGroup
		go1 := func(f func()) { wg.Add(1); go func() { defer wg.Done(); f() }() }
		go1(func() {
			m.mem, m.memErr = host.ReadMemory()
			m.load, _ = host.ReadLoad()
			m.uptime, _ = host.ReadUptime()
			paths := []string{"/", filepath.Join(homeDir(), ".cache", "huggingface"), "/var/lib/docker"}
			paths = append(paths, d.Cfg.Paths...)
			seen := map[string]bool{}
			for _, p := range paths {
				dk, err := host.ReadDisk(p)
				if err != nil || seen[dk.Mount+dk.Device] {
					continue
				}
				seen[dk.Mount+dk.Device] = true
				m.disks = append(m.disks, dk)
			}
		})
		go1(func() { m.gpus, m.gpuErr = host.ReadGPUs(ctx, d.R) })
		go1(func() {
			if d.Swap != nil {
				m.sw, m.swErr = d.Swap.Status(ctx)
			}
		})
		go1(func() {
			if d.Stack == nil {
				return
			}
			m.spInst = d.Stack.Installed()
			if !m.spInst {
				return
			}
			m.sparkia, m.spErr = d.Stack.Services(ctx)
			m.vision, m.visionAt = d.Stack.VisionUp(ctx)
			m.release = d.Stack.Release()
			if env, err := d.Stack.Env(); err == nil {
				m.tag = env["SPARKIA_TAG"]
			}
		})
		go1(func() {
			if d.Docker == nil {
				return
			}
			m.cs, m.dockErr = d.Docker.Containers(ctx)
			if m.dockErr == nil {
				if is, err := d.Docker.Images(ctx); err == nil {
					m.images = len(is)
				}
			}
		})
		go1(func() {
			if d.Sys == nil {
				return
			}
			for _, u := range unitsOf(d) {
				got, err := d.Sys.Show(ctx, u)
				if err == nil && got.Exists() {
					m.units = append(m.units, got)
				}
			}
		})
		wg.Wait()
		return m
	}
}

func (o *overview) update(a *App, msg tea.Msg) tea.Cmd {
	if m, ok := msg.(*overviewMsg); ok {
		o.d, o.at = m, time.Now()
	}
	return nil
}

func (o *overview) key(a *App, k tea.KeyMsg) (bool, tea.Cmd) { return false, nil }

func (o *overview) view(a *App, w, h int) string {
	if o.d == nil {
		return "\n  " + a.spin.View() + " Looking at this machine…"
	}
	m := o.d
	cols := 1
	if w >= 110 {
		cols = 2
	}
	pw := w / cols
	left := []string{o.machine(m, pw), o.gpu(m, pw)}
	right := []string{o.swapper(a, m, pw), o.sparkia(m, pw), o.dockerP(m, pw), o.services(m, pw)}
	if cols == 1 {
		return lipgloss.JoinVertical(lipgloss.Left, append(left, right...)...)
	}
	return lipgloss.JoinHorizontal(lipgloss.Top,
		lipgloss.JoinVertical(lipgloss.Left, left...),
		lipgloss.JoinVertical(lipgloss.Left, right...))
}

func (o *overview) machine(m *overviewMsg, w int) string {
	bw := max(10, w-36)
	rows := [][2]string{
		{"Host", host.Hostname() + sMuted.Render("  up "+host.Duration(m.uptime))},
		{"Load", fmt.Sprintf("%.2f %.2f %.2f", m.load[0], m.load[1], m.load[2])},
	}
	if m.memErr == nil && m.mem.Total > 0 {
		rows = append(rows, [2]string{"Memory", bar(m.mem.UsedPct(), bw) + fmt.Sprintf(" %s free of %s",
			host.Bytes(m.mem.Available), host.Bytes(m.mem.Total))})
		if m.mem.SwapTotal > 0 {
			used := m.mem.SwapTotal - m.mem.SwapFree
			rows = append(rows, [2]string{"Swap", fmt.Sprintf("%s of %s used", host.Bytes(used), host.Bytes(m.mem.SwapTotal))})
		}
	}
	for _, d := range m.disks {
		label := d.Mount
		if label == "" {
			label = d.Path
		}
		rows = append(rows, [2]string{"Disk " + label, bar(d.UsedPct(), bw) + fmt.Sprintf(" %s free of %s",
			host.Bytes(d.Free), host.Bytes(d.Total))})
	}
	return panel("This machine", kv(rows), w)
}

func (o *overview) gpu(m *overviewMsg, w int) string {
	if m.gpuErr != nil {
		return panel("GPU", sBad.Render(m.gpuErr.Error()), w)
	}
	if len(m.gpus) == 0 {
		return panel("GPU", sMuted.Render("No NVIDIA GPU (nvidia-smi isn't here)."), w)
	}
	var b strings.Builder
	for i, g := range m.gpus {
		if i > 0 {
			b.WriteString("\n")
		}
		mem := g.MemUsed + " MiB"
		if strings.Contains(g.MemUsed, "N/A") {
			mem = "unified with the host memory"
		} else if g.MemTotal != "" && !strings.Contains(g.MemTotal, "N/A") {
			mem += " of " + g.MemTotal + " MiB"
		}
		b.WriteString(kv([][2]string{
			{"GPU", sBold.Render(g.Name) + sMuted.Render("  driver "+g.Driver)},
			{"Busy", g.Util + "%" + sMuted.Render("   "+g.Temp+"°C   "+g.Power+" W")},
			{"Memory", mem},
		}))
		if len(g.Processes) > 0 {
			b.WriteString("\n" + sMuted.Render("Using it:"))
			for _, p := range g.Processes {
				use := ""
				if !strings.Contains(p.Mem, "N/A") {
					use = "  " + p.Mem + " MiB"
				}
				b.WriteString(fmt.Sprintf("\n  %s %s%s", sMuted.Render(p.PID), filepath.Base(p.Name), use))
			}
		}
	}
	return panel("GPU", b.String(), w)
}

func (o *overview) swapper(a *App, m *overviewMsg, w int) string {
	switch {
	case errors.Is(m.swErr, swapper.ErrSignIn):
		return panel("Spark Swapper", sWarn.Render("Not signed in.")+sMuted.Render(" Go to 2 Swapper and press enter to sign in."), w)
	case m.swErr != nil:
		return panel("Spark Swapper", sBad.Render(m.swErr.Error()), w)
	case m.sw == nil:
		return panel("Spark Swapper", sMuted.Render("Not configured."), w)
	}
	s := m.sw
	serving := sMuted.Render("nothing")
	if s.Active != "" {
		md := s.Models[s.Active]
		serving = sGood.Render("● ") + sBold.Render(md.Title) + sMuted.Render("  "+md.ServedID+" · "+ctxLabel(md.ServedContext))
	} else if s.Loading != "" {
		serving = sWarn.Render("◐ loading " + s.Models[s.Loading].Title)
	}
	rows := [][2]string{{"Serving", serving}}
	if j := s.Job; j != nil && j.Running() {
		rows = append(rows, [2]string{"Job", sWarn.Render(jobTitle(s, j)) + " " + sMuted.Render(currentStep(j))})
	} else if j != nil && j.State == "failed" {
		rows = append(rows, [2]string{"Last job", sBad.Render("failed: " + firstLine(j.Error))})
	}
	if j := s.InstallJob; j != nil && j.Running() {
		rows = append(rows, [2]string{"Installing", sWarn.Render(jobTitle(s, j)) + " " + sMuted.Render(currentStep(j))})
	}
	front := dot(s.Front.Up && s.Front.EnforcesKey, s.Front.Up)
	rows = append(rows, [2]string{"HTTPS front", front + " " + map[bool]string{true: "up", false: "down"}[s.Front.Up]})
	if s.Pictures != nil && s.Pictures.Installed {
		rows = append(rows, [2]string{"Pictures", dot(s.Pictures.Running, s.Pictures.Enabled) + " " + s.Pictures.Summary})
	}
	ver := s.Version
	if s.Update != nil && s.Update.Available {
		ver += sWarn.Render("  → " + s.Update.Latest + " available")
	}
	rows = append(rows, [2]string{"Version", ver})
	return panel("Spark Swapper", kv(rows), w)
}

func (o *overview) sparkia(m *overviewMsg, w int) string {
	if !m.spInst {
		return panel("Sparkia", sMuted.Render("Not installed here (no compose.yaml in the Sparkia folder)."), w)
	}
	if m.spErr != nil {
		return panel("Sparkia", sBad.Render(m.spErr.Error()), w)
	}
	var parts []string
	for _, s := range m.sparkia {
		parts = append(parts, dot(s.State == "running", s.State == "restarting")+" "+s.Service)
	}
	if len(parts) == 0 {
		parts = append(parts, sBad.Render("no containers"))
	}
	tag := m.tag
	if tag == "" {
		tag = "latest"
	}
	rows := [][2]string{
		{"Services", strings.Join(parts, "  ")},
		{"Vision", dot(m.vision, false) + " " + m.visionAt},
		{"Release", orDash(m.release) + sMuted.Render("  following "+tag)},
	}
	return panel("Sparkia", kv(rows), w)
}

func (o *overview) dockerP(m *overviewMsg, w int) string {
	if m.dockErr != nil {
		return panel("Docker", sBad.Render(firstLine(m.dockErr.Error())), w)
	}
	run, stopped := 0, 0
	var bad []string
	for _, c := range m.cs {
		if c.Running() {
			run++
		} else {
			stopped++
			if c.State == "restarting" || (c.State == "exited" && !strings.Contains(c.Status, "(0)")) {
				bad = append(bad, c.Names)
			}
		}
	}
	rows := [][2]string{{"Containers", fmt.Sprintf("%s running, %d stopped", sGood.Render(itoa(run)), stopped)},
		{"Images", itoa(m.images)}}
	if len(bad) > 0 {
		rows = append(rows, [2]string{"Exited badly", sBad.Render(strings.Join(bad, ", "))})
	}
	return panel("Docker", kv(rows), w)
}

func (o *overview) services(m *overviewMsg, w int) string {
	var rows [][2]string
	for _, u := range m.units {
		rows = append(rows, [2]string{u.Name, state(u.Active) + sMuted.Render(" "+u.Sub)})
	}
	if len(rows) == 0 {
		return panel("Services", sMuted.Render("None of the usual services are installed."), w)
	}
	return panel("Services", kv(rows), w)
}

func ctxLabel(tokens int) string {
	switch {
	case tokens <= 0:
		return "—"
	case tokens%1000000 == 0:
		return itoa(tokens/1000000) + "M"
	case tokens%1048576 == 0:
		return itoa(tokens/1048576) + "M"
	case tokens%1024 == 0:
		return itoa(tokens/1024) + "K"
	default:
		return itoa(tokens)
	}
}
