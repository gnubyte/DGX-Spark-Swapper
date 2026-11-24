// Package docker drives the docker CLI (the same commands a person types), reading its JSON output.
package docker

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
)

// Container is one row of `docker ps -a`.
type Container struct {
	ID         string `json:"ID"`
	Names      string `json:"Names"`
	Image      string `json:"Image"`
	Command    string `json:"Command"`
	State      string `json:"State"`
	Status     string `json:"Status"`
	RunningFor string `json:"RunningFor"`
	CreatedAt  string `json:"CreatedAt"`
	Ports      string `json:"Ports"`
	Labels     string `json:"Labels"`
	Networks   string `json:"Networks"`
	Mounts     string `json:"Mounts"`
	Size       string `json:"Size"`
}

// Label returns one of the container's labels.
func (c Container) Label(name string) string {
	for _, kv := range strings.Split(c.Labels, ",") {
		k, v, ok := strings.Cut(kv, "=")
		if ok && k == name {
			return v
		}
	}
	return ""
}

// Running reports whether the container is up.
func (c Container) Running() bool { return c.State == "running" }

// Project is the compose project the container belongs to, if any.
func (c Container) Project() string { return c.Label("com.docker.compose.project") }

// Image is one row of `docker images`.
type Image struct {
	ID           string `json:"ID"`
	Repository   string `json:"Repository"`
	Tag          string `json:"Tag"`
	Digest       string `json:"Digest"`
	CreatedSince string `json:"CreatedSince"`
	CreatedAt    string `json:"CreatedAt"`
	Size         string `json:"Size"`
	Containers   string `json:"Containers"`
}

// Ref is how the image is named on a command line: repo:tag, or its ID when it has no name.
func (i Image) Ref() string {
	if i.Repository == "" || i.Repository == "<none>" {
		return i.ID
	}
	if i.Tag == "" || i.Tag == "<none>" {
		return i.Repository + "@" + i.Digest
	}
	return i.Repository + ":" + i.Tag
}

// Dangling: no name and no tag (left behind by a rebuild or pull).
func (i Image) Dangling() bool { return i.Repository == "<none>" && i.Tag == "<none>" }

// Usage is one row of `docker system df`.
type Usage struct {
	Type        string `json:"Type"`
	TotalCount  string `json:"TotalCount"`
	Active      string `json:"Active"`
	Size        string `json:"Size"`
	Reclaimable string `json:"Reclaimable"`
}

// Stats is one row of `docker stats --no-stream`.
type Stats struct {
	Name     string `json:"Name"`
	ID       string `json:"ID"`
	CPUPerc  string `json:"CPUPerc"`
	MemUsage string `json:"MemUsage"`
	MemPerc  string `json:"MemPerc"`
	NetIO    string `json:"NetIO"`
	BlockIO  string `json:"BlockIO"`
	PIDs     string `json:"PIDs"`
}

// Volume is one row of `docker volume ls`.
type Volume struct {
	Name       string `json:"Name"`
	Driver     string `json:"Driver"`
	Mountpoint string `json:"Mountpoint"`
	Labels     string `json:"Labels"`
}

// Client runs docker. Priv says whether docker needs root here (the user isn't in the docker group).
type Client struct {
	R    run.Runner
	Priv bool
}

func (c *Client) run(ctx context.Context, timeout time.Duration, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	res, err := c.R.Run(ctx, c.Priv, "docker", args...)
	return res.Stdout, err
}

// Available says whether docker answers, and why not.
func (c *Client) Available(ctx context.Context) error {
	if !c.R.Has("docker") {
		return fmt.Errorf("docker: %w", run.ErrNotFound)
	}
	_, err := c.run(ctx, 10*time.Second, "version", "--format", "{{.Server.Version}}")
	return err
}

// Version is the daemon's version.
func (c *Client) Version(ctx context.Context) (string, error) {
	out, err := c.run(ctx, 10*time.Second, "version", "--format", "{{.Server.Version}}")
	return strings.TrimSpace(out), err
}

// lines decodes one JSON object a line.
func lines[T any](out string) ([]T, error) {
	var items []T
	sc := bufio.NewScanner(strings.NewReader(out))
	sc.Buffer(make([]byte, 64*1024), 4<<20)
	for sc.Scan() {
		l := strings.TrimSpace(sc.Text())
		if l == "" {
			continue
		}
		var it T
		if err := json.Unmarshal([]byte(l), &it); err != nil {
			return items, fmt.Errorf("unexpected docker output: %w", err)
		}
		items = append(items, it)
	}
	return items, nil
}

// Containers lists every container, running first, then by name.
func (c *Client) Containers(ctx context.Context) ([]Container, error) {
	out, err := c.run(ctx, 20*time.Second, "ps", "-a", "--no-trunc", "--format", "{{json .}}")
	if err != nil {
		return nil, err
	}
	cs, err := lines[Container](out)
	sort.SliceStable(cs, func(i, j int) bool {
		if cs[i].Running() != cs[j].Running() {
			return cs[i].Running()
		}
		return cs[i].Names < cs[j].Names
	})
	return cs, err
}

// Images lists the images, newest first.
func (c *Client) Images(ctx context.Context) ([]Image, error) {
	out, err := c.run(ctx, 20*time.Second, "images", "--digests", "--format", "{{json .}}")
	if err != nil {
		return nil, err
	}
	is, err := lines[Image](out)
	sort.SliceStable(is, func(i, j int) bool { return is[i].CreatedAt > is[j].CreatedAt })
	return is, err
}

// Volumes lists the volumes.
func (c *Client) Volumes(ctx context.Context) ([]Volume, error) {
	out, err := c.run(ctx, 20*time.Second, "volume", "ls", "--format", "{{json .}}")
	if err != nil {
		return nil, err
	}
	return lines[Volume](out)
}

// DiskUsage is `docker system df`.
func (c *Client) DiskUsage(ctx context.Context) ([]Usage, error) {
	out, err := c.run(ctx, 60*time.Second, "system", "df", "--format", "{{json .}}")
	if err != nil {
		return nil, err
	}
	return lines[Usage](out)
}

// Stats is one sample of every running container's CPU and memory.
func (c *Client) Stats(ctx context.Context) (map[string]Stats, error) {
	out, err := c.run(ctx, 20*time.Second, "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}")
	if err != nil {
		return nil, err
	}
	rows, err := lines[Stats](out)
	m := make(map[string]Stats, len(rows))
	for _, s := range rows {
		m[s.Name] = s
	}
	return m, err
}

// Inspect is `docker inspect`, pretty-printed.
func (c *Client) Inspect(ctx context.Context, ref string) (string, error) {
	out, err := c.run(ctx, 20*time.Second, "inspect", ref)
	if err != nil {
		return "", err
	}
	var v any
	if json.Unmarshal([]byte(out), &v) == nil {
		if b, err := json.MarshalIndent(v, "", "  "); err == nil {
			return string(b), nil
		}
	}
	return out, nil
}

// Action is one docker command, with how long it may take.
type Action struct {
	Title   string
	Args    []string
	Timeout time.Duration
}

func (a Action) Line() string { return run.Quote(append([]string{"docker"}, a.Args...)) }

func Start(name string) Action {
	return Action{"Start " + name, []string{"start", name}, 2 * time.Minute}
}
func Stop(name string) Action {
	return Action{"Stop " + name, []string{"stop", "-t", "60", name}, 2 * time.Minute}
}
func Restart(name string) Action {
	return Action{"Restart " + name, []string{"restart", "-t", "60", name}, 3 * time.Minute}
}
func Kill(name string) Action {
	return Action{"Kill " + name, []string{"kill", name}, 30 * time.Second}
}
func RemoveContainer(name string, force bool) Action {
	args := []string{"rm", name}
	if force {
		args = []string{"rm", "-f", name}
	}
	return Action{"Remove container " + name, args, time.Minute}
}
func RemoveImage(ref string, force bool) Action {
	args := []string{"rmi", ref}
	if force {
		args = []string{"rmi", "-f", ref}
	}
	return Action{"Remove image " + ref, args, 5 * time.Minute}
}
func Pull(ref string) Action { return Action{"Pull " + ref, []string{"pull", ref}, 2 * time.Hour} }
func PruneImages(all bool) Action {
	if all {
		return Action{"Remove every image no container uses", []string{"image", "prune", "-a", "-f"}, 30 * time.Minute}
	}
	return Action{"Remove dangling images", []string{"image", "prune", "-f"}, 30 * time.Minute}
}
func PruneContainers() Action {
	return Action{"Remove stopped containers", []string{"container", "prune", "-f"}, 10 * time.Minute}
}
func PruneBuildCache() Action {
	return Action{"Clear the build cache", []string{"builder", "prune", "-a", "-f"}, 30 * time.Minute}
}
func RemoveVolume(name string) Action {
	return Action{"Remove volume " + name, []string{"volume", "rm", name}, time.Minute}
}

// Do runs an action and returns what docker printed.
func (c *Client) Do(ctx context.Context, a Action) (string, error) {
	out, err := c.run(ctx, a.Timeout, a.Args...)
	return strings.TrimSpace(out), err
}

// LogsArgs are the arguments for following a container's log.
func LogsArgs(name string, tail int, follow bool) []string {
	args := []string{"logs", "--timestamps", "--tail", fmt.Sprint(tail)}
	if follow {
		args = append(args, "--follow")
	}
	return append(args, name)
}

// ShellArgv is the command that opens a shell in a container (bash where it has one).
func ShellArgv(name string) []string {
	return []string{"docker", "exec", "-it", name, "sh", "-c", "command -v bash >/dev/null && exec bash || exec sh"}
}
