package ui

import (
	"context"
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/docker"
)

type dockerView int

const (
	dvContainers dockerView = iota
	dvImages
	dvVolumes
	dvDisk
)

type dockerTab struct {
	mode    dockerView
	cs      []docker.Container
	images  []docker.Image
	vols    []docker.Volume
	usage   []docker.Usage
	stats   map[string]docker.Stats
	statsAt time.Time
	version string
	err     error
	ctbl    Table
	itbl    Table
	vtbl    Table
	utbl    Table
	loaded  bool
}

type dockerMsg struct {
	mode    dockerView
	cs      []docker.Container
	images  []docker.Image
	vols    []docker.Volume
	usage   []docker.Usage
	stats   map[string]docker.Stats
	version string
	err     error
}

func newDockerTab() *dockerTab {
	return &dockerTab{
		ctbl: Table{Cols: []Column{
			{Title: "", Width: 2},
			{Title: "NAME", Flex: 3, Min: 14},
			{Title: "IMAGE", Flex: 4, Min: 14},
			{Title: "STATE", Width: 10},
			{Title: "STATUS", Flex: 2, Min: 12},
			{Title: "CPU", Width: 7, Right: true},
			{Title: "MEMORY", Width: 18, Right: true},
			{Title: "PROJECT", Width: 10},
		}, Empty: "No containers."},
		itbl: Table{Cols: []Column{
			{Title: "", Width: 2},
			{Title: "IMAGE", Flex: 5, Min: 20},
			{Title: "ID", Width: 12},
			{Title: "SIZE", Width: 9, Right: true},
			{Title: "CREATED", Width: 16},
			{Title: "USED BY", Flex: 2, Min: 10},
		}, Empty: "No images."},
		vtbl: Table{Cols: []Column{
			{Title: "NAME", Flex: 3, Min: 20},
			{Title: "DRIVER", Width: 8},
			{Title: "MOUNTPOINT", Flex: 4, Min: 20},
		}, Empty: "No volumes."},
		utbl: Table{Cols: []Column{
			{Title: "TYPE", Width: 16},
			{Title: "TOTAL", Width: 7, Right: true},
			{Title: "ACTIVE", Width: 7, Right: true},
			{Title: "SIZE", Width: 10, Right: true},
			{Title: "RECLAIMABLE", Flex: 1, Min: 14, Right: true},
		}},
	}
}

func (t *dockerTab) title() string { return "Docker" }

func (t *dockerTab) interval(a *App) time.Duration {
	if t.mode == dvDisk {
		return time.Minute
	}
	return time.Duration(a.D.Cfg.RefreshSecs) * time.Second
}

func (t *dockerTab) table() *Table {
	switch t.mode {
	case dvImages:
		return &t.itbl
	case dvVolumes:
		return &t.vtbl
	case dvDisk:
		return &t.utbl
	}
	return &t.ctbl
}

func (t *dockerTab) setFilter(f string) { t.table().SetFilter(f) }
func (t *dockerTab) filterText() string { return t.table().Filter }

func (t *dockerTab) help() [][2]string {
	views := [2]string{"c i v d", "containers/images/volumes/disk"}
	switch t.mode {
	case dvImages:
		return [][2]string{views, {"p", "pull again"}, {"n", "pull new"}, {"x", "remove"}, {"P", "prune dangling"},
			{"A", "prune unused"}, {"I", "inspect"}, {"enter", "containers using it"}}
	case dvVolumes:
		return [][2]string{views, {"x", "remove"}, {"I", "inspect"}}
	case dvDisk:
		return [][2]string{views, {"B", "clear build cache"}, {"C", "remove stopped containers"}, {"P", "prune dangling images"}}
	}
	return [][2]string{views, {"enter", "logs"}, {"s", "start/stop"}, {"r", "restart"}, {"e", "shell"}, {"x", "remove"},
		{"K", "kill"}, {"I", "inspect"}, {"a", "all/running"}}
}

func (t *dockerTab) refresh(a *App) tea.Cmd {
	d := a.D.Docker
	if d == nil {
		return nil
	}
	mode := t.mode
	wantStats := mode == dvContainers && time.Since(t.statsAt) > 8*time.Second
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(a.ctx, 60*time.Second)
		defer cancel()
		m := dockerMsg{mode: mode}
		if t.version == "" {
			m.version, _ = d.Version(ctx)
		}
		switch mode {
		case dvContainers:
			m.cs, m.err = d.Containers(ctx)
			if m.err == nil && wantStats {
				m.stats, _ = d.Stats(ctx)
			}
		case dvImages:
			m.images, m.err = d.Images(ctx)
			if m.err == nil {
				m.cs, _ = d.Containers(ctx)
			}
		case dvVolumes:
			m.vols, m.err = d.Volumes(ctx)
		case dvDisk:
			m.usage, m.err = d.DiskUsage(ctx)
		}
		return m
	}
}

func (t *dockerTab) update(a *App, msg tea.Msg) tea.Cmd {
	m, ok := msg.(dockerMsg)
	if !ok {
		return nil
	}
	t.loaded = true
	t.err = m.err
	if m.version != "" {
		t.version = m.version
	}
	if m.err != nil {
		return nil
	}
	if m.stats != nil {
		t.stats, t.statsAt = m.stats, time.Now()
	}
	switch m.mode {
	case dvContainers:
		t.cs = m.cs
		t.fillContainers()
	case dvImages:
		t.images = m.images
		if m.cs != nil {
			t.cs = m.cs
		}
		used := map[string][]string{}
		for _, c := range t.cs {
			used[c.Image] = append(used[c.Image], c.Names)
		}
		var rows []Row
		for _, im := range m.images {
			users := append(used[im.Ref()], used[im.ID]...)
			if im.Tag == "latest" {
				users = append(users, used[im.Repository]...)
			}
			mark := sMuted.Render("○")
			if len(users) > 0 {
				mark = sGood.Render("●")
			} else if im.Dangling() {
				mark = sWarn.Render("·")
			}
			created := im.CreatedSince
			name := im.Repository + ":" + im.Tag
			if im.Dangling() {
				name = sMuted.Render("<none> (dangling)")
			}
			rows = append(rows, Row{Key: im.ID + "|" + im.Ref(), Cells: []string{mark, name, shortID(im.ID), im.Size, created,
				strings.Join(users, ", ")}})
		}
		t.itbl.SetRows(rows)
	case dvVolumes:
		t.vols = m.vols
		var rows []Row
		for _, v := range m.vols {
			rows = append(rows, Row{Key: v.Name, Cells: []string{v.Name, v.Driver, v.Mountpoint}})
		}
		t.vtbl.SetRows(rows)
	case dvDisk:
		t.usage = m.usage
		var rows []Row
		for _, u := range m.usage {
			rows = append(rows, Row{Key: u.Type, Cells: []string{u.Type, u.TotalCount, u.Active, u.Size, u.Reclaimable}})
		}
		t.utbl.SetRows(rows)
	}
	return nil
}

func (t *dockerTab) fillContainers() {
	var rows []Row
	for _, c := range t.cs {
		cpu, mem := sMuted.Render("—"), sMuted.Render("—")
		if s, ok := t.stats[c.Names]; ok && c.Running() {
			cpu, mem = s.CPUPerc, strings.Split(s.MemUsage, " / ")[0]
		}
		rows = append(rows, Row{Key: c.Names, Cells: []string{dot(c.Running(), c.State == "restarting" || c.State == "paused"),
			c.Names, c.Image, state(c.State), c.Status, cpu, mem, orDash(c.Project())}})
	}
	t.ctbl.SetRows(rows)
}

func shortID(id string) string {
	id = strings.TrimPrefix(id, "sha256:")
	if len(id) > 12 {
		return id[:12]
	}
	return id
}

func (t *dockerTab) container() (docker.Container, bool) {
	k := t.ctbl.SelectedKey()
	for _, c := range t.cs {
		if c.Names == k {
			return c, true
		}
	}
	return docker.Container{}, false
}

func (t *dockerTab) image() (docker.Image, bool) {
	k := t.itbl.SelectedKey()
	for _, im := range t.images {
		if im.ID+"|"+im.Ref() == k {
			return im, true
		}
	}
	return docker.Image{}, false
}

func (t *dockerTab) key(a *App, k tea.KeyMsg) (bool, tea.Cmd) {
	d := a.D.Docker
	if d == nil {
		return false, nil
	}
	if tableKeys(t.table(), k) {
		return true, nil
	}
	setMode := func(m dockerView) (bool, tea.Cmd) {
		t.mode = m
		return true, a.refreshTab(a.active, true)
	}
	switch k.String() {
	case "c":
		return setMode(dvContainers)
	case "i":
		return setMode(dvImages)
	case "v":
		return setMode(dvVolumes)
	case "d":
		return setMode(dvDisk)
	}
	do := func(act docker.Action, danger bool, body string) tea.Cmd {
		go1 := func() tea.Cmd {
			return a.act(act.Title, act.Line(), d.Priv, func(ctx context.Context) (string, error) { return d.Do(ctx, act) }, nil)
		}
		if !danger && body == "" {
			return go1()
		}
		return a.confirm(newConfirm(act.Title+"?", body, act.Line(), danger, go1))
	}
	inspect := func(ref string) tea.Cmd {
		return a.report("docker inspect "+ref, "docker inspect "+ref, d.Priv, func(ctx context.Context) (string, error) {
			return d.Inspect(ctx, ref)
		})
	}
	switch t.mode {
	case dvContainers:
		c, has := t.container()
		if !has {
			return false, nil
		}
		switch k.String() {
		case "enter", "L":
			return true, a.logs(c.Names, d.Priv, "docker", docker.LogsArgs(c.Names, a.D.Cfg.LogLines, true)...)
		case "s":
			if c.Running() {
				return true, do(docker.Stop(c.Names), true, protectedNote(c))
			}
			return true, do(docker.Start(c.Names), false, "")
		case "r":
			return true, do(docker.Restart(c.Names), protectedNote(c) != "", protectedNote(c))
		case "K":
			return true, do(docker.Kill(c.Names), true, "Stops it at once, without a clean shutdown.")
		case "x":
			body := "Removes the container (its image stays)."
			if c.Running() {
				body = "It's running: it's stopped first. " + body
			}
			if note := protectedNote(c); note != "" {
				body = note + "\n" + body
			}
			return true, do(docker.RemoveContainer(c.Names, c.Running()), true, body)
		case "e":
			if !c.Running() {
				a.say(lvInfo, "%s isn't running.", c.Names)
				return true, nil
			}
			return true, a.handoff("Shell in "+c.Names, rootArgvIf(a, d.Priv, docker.ShellArgv(c.Names)), false)
		case "I":
			return true, inspect(c.Names)
		}
	case dvImages:
		im, has := t.image()
		switch k.String() {
		case "n":
			a.dlg = newInput("Pull an image", "Any name docker pull takes (repo:tag or repo@sha256:…).", "", "ghcr.io/gnubyte/sparkia-server:latest",
				func(v string) tea.Cmd {
					v = strings.TrimSpace(v)
					if v == "" {
						return nil
					}
					return do(docker.Pull(v), false, "")
				})
			return true, nil
		case "P":
			return true, do(docker.PruneImages(false), false, "Removes images with no name that no container uses (left by rebuilds and pulls).")
		case "A":
			return true, a.confirm(newConfirm("Remove every image no container uses?",
				"Includes model engines you aren't running right now: a model whose image is removed has to download or build it "+
					"again (an hour or more for some). Prefer x on single images, or P for the dangling ones.",
				docker.PruneImages(true).Line(), true, func() tea.Cmd {
					act := docker.PruneImages(true)
					return a.act(act.Title, act.Line(), d.Priv, func(ctx context.Context) (string, error) { return d.Do(ctx, act) }, nil)
				}).requireTyping("prune"))
		}
		if !has {
			return false, nil
		}
		switch k.String() {
		case "p":
			if im.Dangling() {
				a.say(lvInfo, "A dangling image has no name to pull.")
				return true, nil
			}
			return true, do(docker.Pull(im.Ref()), false, "")
		case "x":
			ref := im.Ref()
			if im.Dangling() {
				ref = shortID(im.ID)
			}
			var users []string
			for _, c := range t.cs {
				if c.Image == im.Ref() || c.Image == im.ID {
					users = append(users, c.Names)
				}
			}
			body := "Frees " + im.Size + "."
			if len(users) > 0 {
				body = "Used by " + strings.Join(users, ", ") + ": docker refuses unless those containers are removed first. " + body
			}
			return true, do(docker.RemoveImage(ref, false), true, body)
		case "I":
			return true, inspect(im.ID)
		case "enter":
			t.mode = dvContainers
			t.ctbl.SetFilter(im.Repository)
			return true, a.refreshTab(a.active, true)
		}
	case dvVolumes:
		k2 := t.vtbl.SelectedKey()
		switch k.String() {
		case "x":
			if k2 != "" {
				return true, do(docker.RemoveVolume(k2), true, "Deletes everything stored in the volume. Docker refuses while a container uses it.")
			}
		case "I":
			if k2 != "" {
				return true, a.report("docker volume inspect "+k2, "docker volume inspect "+k2, d.Priv, func(ctx context.Context) (string, error) {
					res, err := a.D.R.Run(ctx, d.Priv, "docker", "volume", "inspect", k2)
					return res.Stdout, err
				})
			}
		}
	case dvDisk:
		switch k.String() {
		case "B":
			return true, do(docker.PruneBuildCache(), true, "Engine images that are built locally (TensorFold recipes) rebuild slower next time.")
		case "C":
			return true, do(docker.PruneContainers(), true, "Removes every stopped container. Model containers the Swapper keeps stopped are made again on their next start (slower).")
		case "P":
			return true, do(docker.PruneImages(false), false, "Removes images with no name that no container uses.")
		}
	}
	switch k.String() {
	case "a":
		if t.mode == dvContainers {
			if t.ctbl.Filter == "running" {
				t.ctbl.SetFilter("")
			} else {
				t.ctbl.SetFilter("running")
			}
			return true, nil
		}
	}
	return false, nil
}

// protectedNote warns about containers others depend on.
func protectedNote(c docker.Container) string {
	switch {
	case c.Project() == "sparkia":
		return "This is part of Sparkia: the Sparkia tab (3) manages it with docker compose."
	case strings.HasPrefix(c.Names, "vllm") || strings.Contains(c.Names, "qwen") || strings.Contains(c.Names, "nemotron") ||
		strings.Contains(c.Image, "tensorfold") || strings.Contains(c.Image, "vllm") || strings.Contains(c.Image, "sglang"):
		return "This looks like a model the Swapper serves: swapping from the Swapper tab (2) is the safer way."
	}
	return ""
}

func (t *dockerTab) view(a *App, w, h int) string {
	if a.D.Docker == nil {
		return "\n  " + sMuted.Render("Docker isn't installed on this machine.")
	}
	tabs := []string{"c containers", "i images", "v volumes", "d disk"}
	var parts []string
	for i, s := range tabs {
		if dockerView(i) == t.mode {
			parts = append(parts, sTabOn.Render(s))
		} else {
			parts = append(parts, sTabOff.Render(s))
		}
	}
	head := strings.Join(parts, " ")
	info := ""
	if t.version != "" {
		info = sMuted.Render("Docker " + t.version)
	}
	if a.D.Docker.Priv {
		info += sMuted.Render(" · through sudo (you're not in the docker group)")
	}
	tbl := t.table()
	if tbl.Filter != "" {
		info += sAccent.Render("  filter “" + tbl.Filter + "” (esc clears)")
	}
	head += "  " + info + strings.Repeat(" ", max(0, w-lipgloss.Width(head)-lipgloss.Width(info)-2-len(tbl.Position()))) +
		sMuted.Render(tbl.Position())
	if !t.loaded {
		return head + "\n\n  " + a.spin.View() + " Asking docker…"
	}
	if t.err != nil {
		return head + "\n\n  " + sBad.Render(firstLine(t.err.Error())) + "\n\n  " +
			sMuted.Render("Is the daemon running? Tab 5 Services can start docker.service.")
	}
	body := tbl.View(w, max(3, h-2), true)
	if t.mode == dvDisk {
		body += "\n\n" + sMuted.Render(fmt.Sprintf("Reclaim: %s build cache · %s stopped containers · %s dangling images",
			sKey.Render("B"), sKey.Render("C"), sKey.Render("P")))
	}
	return head + "\n" + body
}
