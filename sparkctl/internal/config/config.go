// Package config keeps sparkctl's settings: /etc/sparkctl/config.json for the machine, ~/.config/sparkctl for the
// person (their Swapper sign-in and the certificate they trusted).
package config

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
)

// Unit is a systemd unit sparkctl shows on the Services tab.
type Unit struct {
	Name  string `json:"name"`
	Label string `json:"label"`
	User  bool   `json:"user,omitempty"`
	// Owner is the user whose manager runs a user unit (empty: whoever runs sparkctl).
	Owner string `json:"owner,omitempty"`
}

// Config is the machine's settings.
type Config struct {
	SwapperURL  string `json:"swapper_url"`
	SwapperUser string `json:"swapper_user"`
	SparkiaDir  string `json:"sparkia_dir"`
	VisionURL   string `json:"vision_url"`
	RefreshSecs int    `json:"refresh_seconds"`
	LogLines    int    `json:"log_lines"`
	Units       []Unit `json:"units"`
	// Extra disks to show on the overview (the HF cache and docker's root are always shown).
	Paths []string `json:"paths"`
	// Commands are named shell commands on the Run tab (anything you'd type at the prompt).
	Commands []Command `json:"commands"`
}

// Command is a saved command line.
type Command struct {
	Name string `json:"name"`
	Cmd  string `json:"cmd"`
	Root bool   `json:"root,omitempty"`
}

// Defaults for a DGX Spark with Spark Swapper and Sparkia.
func Defaults() Config {
	return Config{
		SwapperURL:  "https://127.0.0.1:8999",
		SparkiaDir:  "/opt/sparkia",
		VisionURL:   "https://127.0.0.1:8443/",
		RefreshSecs: 3,
		LogLines:    500,
		Units: []Unit{
			{Name: "spark-swapper.service", Label: "Spark Swapper"},
			{Name: "docker.service", Label: "Docker"},
			{Name: "sparkia-update.timer", Label: "Sparkia updates (every 15 min)"},
			{Name: "sparkia-update.service", Label: "Sparkia updater"},
			{Name: "spark-firewall.service", Label: "Firewall rules at boot"},
			{Name: "nginx.service", Label: "HTTPS front (nginx)"},
			{Name: "qwen-image.service", Label: "Qwen-Image (pictures)", User: true},
			{Name: "openclaw-gateway.service", Label: "OpenClaw gateway", User: true},
			{Name: "vision-server.service", Label: "Vision Server (host install)", User: true},
		},
		Commands: []Command{
			{Name: "GPU (nvidia-smi)", Cmd: "nvidia-smi"},
			{Name: "Disk usage", Cmd: "df -h -x tmpfs -x overlay"},
			{Name: "Docker disk usage", Cmd: "docker system df -v", Root: true},
			{Name: "Firewall status", Cmd: "spark-firewall status", Root: true},
			{Name: "Firewall live traffic", Cmd: "spark-firewall now", Root: true},
			{Name: "Sparkia: update now", Cmd: "systemctl start sparkia-update && journalctl -u sparkia-update -n 40 --no-pager", Root: true},
			{Name: "Swapper: follow its log", Cmd: "journalctl -u spark-swapper -f -n 200", Root: true},
			{Name: "Top (memory)", Cmd: "top -o %MEM"},
		},
	}
}

// SystemPath is the machine-wide file.
const SystemPath = "/etc/sparkctl/config.json"

// UserDir is ~/.config/sparkctl (XDG_CONFIG_HOME respected).
func UserDir() string {
	if d := os.Getenv("XDG_CONFIG_HOME"); d != "" {
		return filepath.Join(d, "sparkctl")
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return filepath.Join(os.TempDir(), "sparkctl")
	}
	return filepath.Join(home, ".config", "sparkctl")
}

// Load reads the defaults, then the machine file, then the person's own config.json, each overriding the last.
// A file that's missing is skipped; one that's broken is an error naming it.
func Load(paths ...string) (Config, error) {
	c := Defaults()
	if len(paths) == 0 {
		paths = []string{SystemPath, filepath.Join(UserDir(), "config.json")}
	}
	for _, p := range paths {
		b, err := os.ReadFile(p)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return c, err
		}
		if err := json.Unmarshal(b, &c); err != nil {
			return Defaults(), &BadFile{Path: p, Err: err}
		}
	}
	if c.RefreshSecs < 1 {
		c.RefreshSecs = 3
	}
	if c.LogLines < 50 {
		c.LogLines = 500
	}
	return c, nil
}

// BadFile is a config file that isn't valid JSON.
type BadFile struct {
	Path string
	Err  error
}

func (e *BadFile) Error() string { return e.Path + ": " + e.Err.Error() }

// State is what sparkctl remembers for the person between runs.
type State struct {
	SwapperSession string `json:"swapper_session,omitempty"`
	SwapperPin     string `json:"swapper_pin,omitempty"`
	SwapperUser    string `json:"swapper_user,omitempty"`
	LastTab        int    `json:"last_tab,omitempty"`
}

// StatePath is ~/.config/sparkctl/state.json.
func StatePath() string { return filepath.Join(UserDir(), "state.json") }

// LoadState reads the person's state (an empty one when there's none or it's unreadable).
func LoadState() State {
	var s State
	b, err := os.ReadFile(StatePath())
	if err == nil {
		_ = json.Unmarshal(b, &s)
	}
	return s
}

// Save writes the state readable by the person only (it holds a sign-in).
func (s State) Save() error {
	dir := UserDir()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".state-*")
	if err != nil {
		return err
	}
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if _, err := tmp.Write(b); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	return os.Rename(tmp.Name(), StatePath())
}
