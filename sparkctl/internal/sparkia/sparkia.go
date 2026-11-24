// Package sparkia manages the Sparkia stack: its Docker Compose project in /opt/sparkia (Vision Server, the firewall and
// the guard), the release it follows, its updater and the firewall's own CLI.
package sparkia

import (
	"bufio"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
)

// Service is one row of `docker compose ps`.
type Service struct {
	Name     string `json:"Name"`
	Service  string `json:"Service"`
	Image    string `json:"Image"`
	State    string `json:"State"`
	Status   string `json:"Status"`
	Health   string `json:"Health"`
	ExitCode int    `json:"ExitCode"`
	// CreatedAt is a number in some Compose versions and text in others (2.40): not read.
}

// Stack is the Sparkia install in Dir.
type Stack struct {
	R    run.Runner
	Dir  string // /opt/sparkia
	Priv bool   // docker needs root here
	// VisionURL is where Vision Server answers (https://127.0.0.1:8443).
	VisionURL string
}

// Installed reports whether the compose project is on this machine.
func (s *Stack) Installed() bool {
	_, err := os.Stat(filepath.Join(s.Dir, "compose.yaml"))
	return err == nil
}

func (s *Stack) compose(args ...string) []string {
	return append([]string{"compose", "--project-directory", s.Dir, "-f", filepath.Join(s.Dir, "compose.yaml")}, args...)
}

// ComposeArgv is a docker compose command line for the stack (for logs and hand-offs).
func (s *Stack) ComposeArgv(args ...string) []string {
	return append([]string{"docker"}, s.compose(args...)...)
}

// Services lists the stack's containers.
func (s *Stack) Services(ctx context.Context) ([]Service, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	res, err := s.R.Run(ctx, s.Priv, "docker", s.compose("ps", "-a", "--format", "json")...)
	if err != nil {
		return nil, err
	}
	return ParseServices(res.Stdout)
}

// ParseServices reads `docker compose ps --format json`: a JSON array (older Compose) or a JSON object a line.
func ParseServices(out string) ([]Service, error) {
	out = strings.TrimSpace(out)
	var svcs []Service
	if out == "" {
		return nil, nil
	}
	if strings.HasPrefix(out, "[") {
		if err := json.Unmarshal([]byte(out), &svcs); err != nil {
			return nil, fmt.Errorf("unexpected docker compose output: %w", err)
		}
	} else {
		sc := bufio.NewScanner(strings.NewReader(out))
		sc.Buffer(make([]byte, 64*1024), 4<<20)
		for sc.Scan() {
			l := strings.TrimSpace(sc.Text())
			if l == "" {
				continue
			}
			var sv Service
			if err := json.Unmarshal([]byte(l), &sv); err != nil {
				return svcs, fmt.Errorf("unexpected docker compose output: %w", err)
			}
			svcs = append(svcs, sv)
		}
	}
	sort.SliceStable(svcs, func(i, j int) bool { return svcs[i].Service < svcs[j].Service })
	return svcs, nil
}

// Env reads /opt/sparkia/.env (KEY=value lines; comments and blanks kept apart).
func (s *Stack) Env() (map[string]string, error) {
	b, err := os.ReadFile(filepath.Join(s.Dir, ".env"))
	if err != nil {
		return nil, err
	}
	return ParseEnv(string(b)), nil
}

// ParseEnv reads KEY=value lines.
func ParseEnv(text string) map[string]string {
	env := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		env[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), `"'`)
	}
	return env
}

var keyRe = regexp.MustCompile(`^[A-Z_][A-Z0-9_]*$`)
var tagRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// SetEnvText returns text with KEY set to value (replaced where it is, appended otherwise).
func SetEnvText(text, key, value string) (string, error) {
	if !keyRe.MatchString(key) {
		return "", fmt.Errorf("%q isn't a setting name", key)
	}
	if strings.ContainsAny(value, "\n\r") {
		return "", fmt.Errorf("a value is one line")
	}
	lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
	done := false
	for i, l := range lines {
		k, _, ok := strings.Cut(strings.TrimSpace(l), "=")
		if ok && strings.TrimSpace(k) == key && !strings.HasPrefix(strings.TrimSpace(l), "#") {
			lines[i] = key + "=" + value
			done = true
		}
	}
	if !done {
		lines = append(lines, key+"="+value)
	}
	return strings.Join(lines, "\n") + "\n", nil
}

// ValidTag reports whether tag is a release tag Sparkia can pin ("latest", "0.9.142", a commit).
func ValidTag(tag string) bool { return tagRe.MatchString(tag) }

// SetTag pins the release the stack runs (SPARKIA_TAG in .env); "latest" follows main again. It writes .env as root
// (through a file in the user's own temp folder, installed in place).
func (s *Stack) SetTag(ctx context.Context, tag string) error {
	if !ValidTag(tag) {
		return fmt.Errorf("%q isn't a release tag (latest, 0.9.142, …)", tag)
	}
	path := filepath.Join(s.Dir, ".env")
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	text, err := SetEnvText(string(b), "SPARKIA_TAG", tag)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp("", "sparkia-env-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.WriteString(text); err != nil {
		tmp.Close()
		return err
	}
	tmp.Close()
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	// validate before replacing: compose must still read it
	if _, err := s.R.Run(ctx, s.Priv, "docker", "compose", "--project-directory", s.Dir, "-f", filepath.Join(s.Dir, "compose.yaml"),
		"--env-file", tmp.Name(), "config", "-q"); err != nil {
		return fmt.Errorf("the new .env doesn't check out: %w", err)
	}
	_, err = s.R.Run(ctx, true, "install", "-m", "0644", tmp.Name(), path)
	return err
}

// Release is the release tag the updater last installed (/opt/sparkia/.release).
func (s *Stack) Release() string {
	b, err := os.ReadFile(filepath.Join(s.Dir, ".release"))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// Action is one compose command.
type Action struct {
	Title   string
	Args    []string
	Timeout time.Duration
}

func (s *Stack) Line(a Action) string {
	return run.Quote(append([]string{"sudo", "docker"}, s.compose(a.Args...)...))
}

func Up() Action {
	return Action{"Start the stack (up -d)", []string{"up", "-d", "--remove-orphans"}, 20 * time.Minute}
}
func Pull() Action { return Action{"Pull the stack's images", []string{"pull"}, time.Hour} }
func Down() Action {
	return Action{"Stop and remove the stack's containers", []string{"down"}, 5 * time.Minute}
}
func RestartAll() Action {
	return Action{"Restart every service", []string{"restart"}, 10 * time.Minute}
}
func Restart(svc string) Action {
	return Action{"Restart " + svc, []string{"restart", svc}, 5 * time.Minute}
}
func Stop(svc string) Action  { return Action{"Stop " + svc, []string{"stop", svc}, 5 * time.Minute} }
func Start(svc string) Action { return Action{"Start " + svc, []string{"start", svc}, 5 * time.Minute} }
func Recreate(svc string) Action {
	return Action{"Recreate " + svc, []string{"up", "-d", "--force-recreate", svc}, 10 * time.Minute}
}

// Do runs a compose action.
func (s *Stack) Do(ctx context.Context, a Action) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, a.Timeout)
	defer cancel()
	res, err := s.R.Run(ctx, s.Priv, "docker", s.compose(a.Args...)...)
	return strings.TrimSpace(res.Stdout + "\n" + res.Stderr), err
}

// LogsArgs follow one service's log (all of them when svc is "").
func (s *Stack) LogsArgs(svc string, tail int, follow bool) []string {
	args := []string{"logs", "--timestamps", "--tail", fmt.Sprint(tail)}
	if follow {
		args = append(args, "--follow")
	}
	if svc != "" {
		args = append(args, svc)
	}
	return s.compose(args...)
}

// FirewallStatus runs `spark-firewall status` (the CLI runs inside the firewall container).
func (s *Stack) FirewallStatus(ctx context.Context) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if s.R.Has("spark-firewall") {
		res, err := s.R.Run(ctx, true, "spark-firewall", "status")
		return strings.TrimSpace(res.Stdout), err
	}
	res, err := s.R.Run(ctx, s.Priv, "docker", "exec", "-i", "sparkia-firewall", "python3", "-I", "/usr/local/bin/spark-firewall", "status")
	return strings.TrimSpace(res.Stdout), err
}

// FirewallArgv opens the firewall CLI interactively with args.
func FirewallArgv(args ...string) []string {
	return append([]string{"spark-firewall"}, args...)
}

// VisionUp checks Vision Server answers over HTTPS (any HTTP status means it's up).
func (s *Stack) VisionUp(ctx context.Context) (bool, string) {
	if s.VisionURL == "" {
		return false, "no address"
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	cl := &http.Client{Transport: &http.Transport{Proxy: nil, TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.VisionURL, nil)
	if err != nil {
		return false, err.Error()
	}
	resp, err := cl.Do(req)
	if err != nil {
		return false, err.Error()
	}
	resp.Body.Close()
	return resp.StatusCode < 500, resp.Status
}
