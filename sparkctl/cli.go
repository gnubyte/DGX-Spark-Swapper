package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/charmbracelet/x/term"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/config"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/docker"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/host"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/sparkia"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/swapper"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/systemd"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/ui"
)

func runCLI(cmd string, args []string) int {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	switch cmd {
	case "help":
		usage()
		return 0
	case "version":
		fmt.Println("sparkctl", version)
		return 0
	case "default-config":
		// the packaged /etc/sparkctl/config.json is made from this, so the file and the built-in defaults agree
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		enc.SetEscapeHTML(false)
		if err := enc.Encode(config.Defaults()); err != nil {
			return fail(err)
		}
		return 0
	case "completion":
		fmt.Print(bashCompletion)
		return 0
	}
	d, cfgErr := deps()
	if cfgErr != nil {
		fmt.Fprintln(os.Stderr, "warning: config:", cfgErr, "(using the defaults)")
	}
	var err error
	switch cmd {
	case "status":
		err = cliStatus(ctx, d, len(args) > 0 && args[0] == "--json")
	case "models":
		err = cliModels(ctx, d)
	case "swap":
		if len(args) != 1 {
			return fail(errors.New("usage: sparkctl swap MODEL"))
		}
		err = cliSwap(ctx, d, args[0])
	case "login":
		user := ""
		if len(args) > 0 {
			user = args[0]
		}
		err = cliLogin(ctx, d, user)
	case "logout":
		if d.Swap != nil {
			err = d.Swap.Logout(ctx)
		}
		if err == nil {
			fmt.Println("Signed out.")
		}
	case "logs":
		if len(args) != 1 {
			return fail(errors.New("usage: sparkctl logs swapper | sparkia[:SERVICE] | updater | docker:CONTAINER | unit:NAME"))
		}
		err = cliLogs(d, args[0])
	case "doctor":
		return cliDoctor(ctx, d)
	default:
		usage()
		return fail(fmt.Errorf("unknown command %q", cmd))
	}
	if err != nil {
		return fail(err)
	}
	return 0
}

func fail(err error) int {
	msg := err.Error()
	if errors.Is(err, swapper.ErrSignIn) {
		msg += " first: sparkctl login"
	}
	fmt.Fprintln(os.Stderr, "sparkctl:", msg)
	return 1
}

var (
	green = "\033[32m"
	red   = "\033[31m"
	amber = "\033[33m"
	dim   = "\033[2m"
	reset = "\033[0m"
)

func init() {
	if !term.IsTerminal(os.Stdout.Fd()) || os.Getenv("NO_COLOR") != "" {
		green, red, amber, dim, reset = "", "", "", "", ""
	}
}

func ok(b bool) string {
	if b {
		return green + "✓" + reset
	}
	return red + "✗" + reset
}

func cliStatus(ctx context.Context, d ui.Deps, asJSON bool) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	out := map[string]any{"host": host.Hostname()}
	mem, _ := host.ReadMemory()
	out["memory"] = map[string]uint64{"total": mem.Total, "available": mem.Available}
	var st *swapper.Status
	var swErr error
	if d.Swap != nil {
		st, swErr = d.Swap.Status(ctx)
	}
	if st != nil {
		active := map[string]any{"key": st.Active}
		if m, ok := st.Models[st.Active]; ok {
			active["title"], active["served_id"], active["context"] = m.Title, m.ServedID, m.ServedContext
		}
		out["swapper"] = map[string]any{"version": st.Version, "active": active, "busy": st.Busy, "job": st.Job}
	} else if swErr != nil {
		out["swapper"] = map[string]any{"error": swErr.Error()}
	}
	var svcs any
	if d.Stack != nil && d.Stack.Installed() {
		s, err := d.Stack.Services(ctx)
		if err != nil {
			svcs = map[string]string{"error": err.Error()}
		} else {
			svcs = s
		}
		out["sparkia"] = map[string]any{"release": d.Stack.Release(), "services": svcs}
	}
	var cs []docker.Container
	if d.Docker != nil {
		var err error
		cs, err = d.Docker.Containers(ctx)
		if err != nil {
			out["docker"] = map[string]string{"error": err.Error()}
		} else {
			out["docker"] = cs
		}
	}
	var units []systemd.Unit
	if d.Sys != nil {
		for _, c := range d.Cfg.Units {
			u, err := d.Sys.Show(ctx, systemd.Unit{Name: c.Name, User: c.User, Owner: c.Owner})
			if err == nil && u.Exists() {
				units = append(units, u)
			}
		}
		out["units"] = units
	}
	if asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(out)
	}
	fmt.Printf("%s  memory %s free of %s\n", host.Hostname(), host.Bytes(mem.Available), host.Bytes(mem.Total))
	switch {
	case st != nil && st.Active != "":
		m := st.Models[st.Active]
		fmt.Printf("Swapper  %s serving %s (%s, %s)\n", ok(true), m.Title, m.ServedID, ctxShort(m.ServedContext))
	case st != nil:
		fmt.Printf("Swapper  %s nothing serving\n", amber+"◐"+reset)
	case swErr != nil:
		fmt.Printf("Swapper  %s %v\n", ok(false), swErr)
	}
	if st != nil && st.Job != nil && st.Job.Running() {
		fmt.Printf("         job: %s %s\n", st.Job.Kind, st.Job.Target)
	}
	if svcs != nil {
		if ss, isList := svcs.([]sparkia.Service); !isList {
			fmt.Printf("Sparkia  %s %v\n", ok(false), svcs)
		} else {
			var parts []string
			for _, s := range ss {
				parts = append(parts, ok(s.State == "running")+" "+s.Service)
			}
			fmt.Printf("Sparkia  %s  %s\n", strings.Join(parts, "  "), dim+d.Stack.Release()+reset)
		}
	}
	if cs != nil {
		up := 0
		for _, c := range cs {
			if c.Running() {
				up++
			}
		}
		fmt.Printf("Docker   %d running, %d stopped\n", up, len(cs)-up)
	}
	for _, u := range units {
		fmt.Printf("  %s %-28s %s %s\n", ok(u.Active == "active"), u.Name, u.Active, dim+u.Sub+reset)
	}
	return nil
}

func ctxShort(n int) string {
	switch {
	case n >= 1048576 && n%1048576 == 0:
		return fmt.Sprintf("%dM", n/1048576)
	case n >= 1024 && n%1024 == 0:
		return fmt.Sprintf("%dK", n/1024)
	}
	return fmt.Sprint(n)
}

func needSwapper(d ui.Deps) error {
	if d.Swap == nil {
		return errors.New("no Swapper configured (swapper_url)")
	}
	return nil
}

func cliModels(ctx context.Context, d ui.Deps) error {
	if err := needSwapper(d); err != nil {
		return err
	}
	st, err := d.Swap.Status(ctx)
	if err != nil {
		return err
	}
	for _, m := range st.Sorted() {
		mark, word := " ", dim+"not installed"+reset
		switch {
		case m.Key == st.Active:
			mark, word = green+"●"+reset, green+"serving"+reset
		case m.Running:
			mark, word = amber+"◐"+reset, amber+"loading"+reset
		case m.Installed:
			mark, word = "○", "installed"
		}
		fmt.Printf("%s %-12s %-40s %6s  %s\n", mark, m.Key, m.Title, ctxShort(m.Context), word)
	}
	return nil
}

// findModel matches a model by key, then by title (any case), then by a unique prefix.
func findModel(st *swapper.Status, q string) (swapper.Model, error) {
	if m, ok := st.Models[q]; ok {
		return m, nil
	}
	lq := strings.ToLower(q)
	var hits []swapper.Model
	for _, m := range st.Sorted() {
		if strings.ToLower(m.Title) == lq {
			return m, nil
		}
		if strings.HasPrefix(strings.ToLower(m.Key), lq) || strings.Contains(strings.ToLower(m.Title), lq) {
			hits = append(hits, m)
		}
	}
	switch len(hits) {
	case 1:
		return hits[0], nil
	case 0:
		return swapper.Model{}, fmt.Errorf("no model matches %q (sparkctl models lists them)", q)
	}
	var names []string
	for _, h := range hits {
		names = append(names, h.Key)
	}
	return swapper.Model{}, fmt.Errorf("%q matches %s: say which", q, strings.Join(names, ", "))
}

func cliSwap(ctx context.Context, d ui.Deps, q string) error {
	if err := needSwapper(d); err != nil {
		return err
	}
	st, err := d.Swap.Status(ctx)
	if err != nil {
		return err
	}
	m, err := findModel(st, q)
	if err != nil {
		return err
	}
	if st.Active == m.Key {
		fmt.Printf("%s is already serving.\n", m.Title)
		return nil
	}
	job, err := d.Swap.Swap(ctx, m.Key)
	if err != nil {
		return err
	}
	fmt.Printf("Swapping to %s (job %s). Ctrl+C stops watching; the swap goes on.\n", m.Title, job.ID)
	seen := 0
	for {
		st, err := d.Swap.Status(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			fmt.Fprintln(os.Stderr, dim+"(", err, ")"+reset)
		} else if j := st.Job; j != nil && j.ID == job.ID {
			start := j.LogTotal - len(j.Log)
			for i, l := range j.Log {
				if start+i >= seen {
					fmt.Printf("%s %s\n", dim+l.At().Format("15:04:05")+reset, l.M)
				}
			}
			seen = j.LogTotal
			if !j.Running() {
				if j.State == "failed" {
					return fmt.Errorf("the swap failed: %s", j.Error)
				}
				fmt.Printf("%s %s is serving.\n", ok(true), m.Title)
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(time.Second):
		}
	}
}

func cliLogin(ctx context.Context, d ui.Deps, user string) error {
	if err := needSwapper(d); err != nil {
		return err
	}
	s, err := d.Swap.Session(ctx)
	if err != nil {
		return err
	}
	if s.SetupNeeded {
		return fmt.Errorf("this Swapper has no admin yet: open %s in a browser once to create it", d.Swap.Base)
	}
	in := bufio.NewReader(os.Stdin)
	if user == "" {
		user = d.State.SwapperUser
		if user == "" {
			user = d.Cfg.SwapperUser
		}
		fmt.Printf("Spark Swapper at %s (%s)\nUsername", d.Swap.Base, s.Hostname)
		if user != "" {
			fmt.Printf(" [%s]", user)
		}
		fmt.Print(": ")
		line, _ := in.ReadString('\n')
		if v := strings.TrimSpace(line); v != "" {
			user = v
		}
	}
	if user == "" {
		return errors.New("no username")
	}
	fmt.Printf("Password for %s: ", user)
	var pw []byte
	if term.IsTerminal(os.Stdin.Fd()) {
		pw, err = term.ReadPassword(os.Stdin.Fd())
		fmt.Println()
	} else {
		var line string
		line, err = in.ReadString('\n')
		pw = []byte(strings.TrimRight(line, "\r\n"))
	}
	if err != nil {
		return err
	}
	if err := d.Swap.Login(ctx, user, string(pw)); err != nil {
		return err
	}
	d.State.SwapperUser = user
	d.SaveState()
	fmt.Printf("%s Signed in as %s. Fingerprint of the Swapper's certificate: %s\n", ok(true), user, d.Swap.Pin())
	return nil
}

// cliLogs replaces sparkctl with the log command (Ctrl+C ends it like any log).
func cliLogs(d ui.Deps, target string) error {
	kind, name, _ := strings.Cut(target, ":")
	var argv []string
	priv := false
	switch kind {
	case "swapper":
		a, p := systemd.JournalArgs(systemd.Unit{Name: "spark-swapper.service"}, "", 300, true)
		argv, priv = append([]string{"journalctl"}, a...), p
	case "updater":
		a, p := systemd.JournalArgs(systemd.Unit{Name: "sparkia-update.service"}, "", 300, true)
		argv, priv = append([]string{"journalctl"}, a...), p
	case "unit":
		if name == "" {
			return errors.New("unit:NAME")
		}
		a, p := systemd.JournalArgs(systemd.Unit{Name: name}, "", 300, true)
		argv, priv = append([]string{"journalctl"}, a...), p
	case "sparkia":
		argv = append([]string{"docker"}, d.Stack.LogsArgs(name, 300, true)...)
		priv = d.Stack.Priv
	case "docker":
		if name == "" {
			return errors.New("docker:CONTAINER")
		}
		argv = append([]string{"docker"}, docker.LogsArgs(name, 300, true)...)
		priv = d.Docker != nil && d.Docker.Priv
	default:
		return fmt.Errorf("unknown log %q: swapper, sparkia[:SERVICE], updater, docker:CONTAINER, unit:NAME", target)
	}
	if priv && os.Geteuid() != 0 && !(kind == "swapper" || kind == "updater" || kind == "unit") {
		argv = append([]string{"sudo"}, argv...)
	}
	path, err := exec.LookPath(argv[0])
	if err != nil {
		return fmt.Errorf("%s: %w", argv[0], run.ErrNotFound)
	}
	return syscall.Exec(path, argv, os.Environ())
}

func cliDoctor(ctx context.Context, d ui.Deps) int {
	bad := 0
	check := func(name string, good bool, detail string) {
		if !good {
			bad++
		}
		fmt.Printf("%s %-26s %s\n", ok(good), name, detail)
	}
	cfgPath := config.SystemPath
	if _, err := os.Stat(cfgPath); err != nil {
		cfgPath = "defaults (no " + config.SystemPath + ")"
	}
	check("config", d.ConfigNote == "", cfgPath+" "+d.ConfigNote)
	if h, ok2 := d.R.(*run.Host); ok2 {
		p := h.Privilege()
		check("root for actions", p != run.NoRoot, p.String())
	}
	if d.Docker == nil {
		check("docker", false, "not installed")
	} else {
		v, err := d.Docker.Version(ctx)
		detail := "daemon " + v
		if d.Docker.Priv {
			detail += " (through sudo: add yourself to the docker group to skip it)"
		}
		if err != nil {
			detail = err.Error()
		}
		check("docker", err == nil, detail)
	}
	if d.Sys == nil {
		check("systemd", false, "systemctl not found")
	} else {
		u, err := d.Sys.Show(ctx, systemd.Unit{Name: "spark-swapper.service"})
		detail := u.Active + " " + u.Sub
		if err != nil {
			detail = err.Error()
		} else if !u.Exists() {
			detail = "not installed"
		}
		check("spark-swapper.service", err == nil && u.Active == "active", detail)
	}
	if d.Swap != nil {
		s, err := d.Swap.Session(ctx)
		if err != nil {
			check("Swapper API", false, err.Error())
		} else {
			check("Swapper API", true, fmt.Sprintf("%s v%s at %s", s.Hostname, s.Version, d.Swap.Base))
			_, err := d.Swap.Status(ctx)
			detail := "signed in as " + d.State.SwapperUser
			if err != nil {
				detail = err.Error()
				if errors.Is(err, swapper.ErrSignIn) {
					detail = "not signed in: sparkctl login"
				}
			}
			check("Swapper sign-in", err == nil, detail)
		}
	}
	if d.Stack != nil {
		if !d.Stack.Installed() {
			check("Sparkia", true, "not installed here ("+d.Stack.Dir+")")
		} else {
			ss, err := d.Stack.Services(ctx)
			if err != nil {
				check("Sparkia", false, err.Error())
			} else {
				for _, s := range ss {
					check("Sparkia "+s.Service, s.State == "running", s.Status)
				}
			}
			up, how := d.Stack.VisionUp(ctx)
			check("Vision Server", up, how)
		}
	}
	if gs, err := host.ReadGPUs(ctx, d.R); err == nil && len(gs) > 0 {
		check("GPU", true, gs[0].Name+" driver "+gs[0].Driver)
	} else if err != nil {
		check("GPU", false, err.Error())
	}
	if bad > 0 {
		fmt.Printf("\n%d problem(s).\n", bad)
		return 1
	}
	fmt.Println("\nAll good.")
	return 0
}
