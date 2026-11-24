package ui

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/x/ansi"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/config"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/docker"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/sparkia"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/swapper"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/systemd"
)

type fakeAPI struct {
	mu     sync.Mutex
	status []byte
	posts  map[string]string
}

func (f *fakeAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Method == http.MethodPost {
		b, _ := json.Marshal(map[string]any{})
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		b, _ = json.Marshal(body)
		f.posts[r.URL.Path] = string(b)
	}
	switch r.URL.Path {
	case "/api/session":
		w.Write([]byte(`{"setup_needed":false,"version":"2.3.1","hostname":"spark","platform_label":"DGX Spark"}`))
		return
	case "/api/login":
		http.SetCookie(w, &http.Cookie{Name: "swapper_session", Value: "s1"})
		w.Write([]byte(`{"ok":true}`))
		return
	}
	if ck, err := r.Cookie("swapper_session"); err != nil || ck.Value != "s1" {
		w.WriteHeader(401)
		w.Write([]byte(`{"error":"Please sign in."}`))
		return
	}
	switch r.URL.Path {
	case "/api/status":
		w.Write(f.status)
	case "/api/swap":
		w.WriteHeader(202)
		w.Write([]byte(`{"ok":true,"job":{"id":"j9","kind":"swap","target":"tfflash","state":"running","steps":[{"label":"Stopping the current model","state":"running"}],"log":[{"t":1795494920,"m":"stopping tf27b"}]}}`))
	default:
		w.Write([]byte(`{"ok":true}`))
	}
}

const ps = `{"ID":"a","Names":"sparkia-vision","Image":"ghcr.io/gnubyte/sparkia-server:latest","State":"running","Status":"Up 2 days","Labels":"com.docker.compose.project=sparkia"}
{"ID":"b","Names":"old-thing","Image":"busybox","State":"exited","Status":"Exited (1) 3 days ago","Labels":""}`

func newTestApp(t *testing.T) (*App, *fakeAPI, *run.Fake) {
	t.Helper()
	b, err := os.ReadFile("../swapper/testdata/status.json")
	if err != nil {
		t.Fatal(err)
	}
	api := &fakeAPI{status: b, posts: map[string]string{}}
	srv := httptest.NewTLSServer(api)
	t.Cleanup(srv.Close)
	f := run.NewFake()
	f.On("docker ps -a --no-trunc --format {{json .}}", ps)
	f.On("docker version --format {{.Server.Version}}", "27.3.1\n")
	f.On("docker images --digests --format {{json .}}", `{"ID":"sha256:1","Repository":"busybox","Tag":"latest","Size":"4MB","CreatedSince":"1 year ago"}`)
	f.On("systemctl show --property="+"Id,Description,LoadState,ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,MainPID,MemoryCurrent,NRestarts,Result,NextElapseUSecRealtime,LastTriggerUSec"+" -- spark-swapper.service",
		"LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n")
	f.Missing["nvidia-smi"] = true
	cfg := config.Defaults()
	cfg.SparkiaDir = t.TempDir() // not installed
	st := &config.State{}
	d := Deps{Cfg: cfg, State: st, R: f, Docker: &docker.Client{R: f}, Sys: &systemd.Client{R: f},
		Swap: swapper.New(srv.URL, "", ""), Stack: &sparkia.Stack{R: f, Dir: cfg.SparkiaDir}}
	a := New(d)
	a.active = 0
	a.Update(tea.WindowSizeMsg{Width: 140, Height: 40})
	return a, api, f
}

// drain runs a command and feeds what it returns back into the app, the way the program would (ticks skipped).
func drain(t *testing.T, a *App, cmd tea.Cmd) {
	t.Helper()
	if cmd == nil {
		return
	}
	done := make(chan tea.Msg, 1)
	go func() { done <- cmd() }()
	var msg tea.Msg
	select {
	case msg = <-done:
	case <-time.After(3 * time.Second):
		return // a tick or a blocking stream: not part of the step
	}
	switch m := msg.(type) {
	case nil:
		return
	case tea.BatchMsg:
		for _, c := range m {
			drain(t, a, c)
		}
		return
	case tickMsg:
		return
	}
	if v := reflect.ValueOf(msg); v.Kind() == reflect.Slice && strings.Contains(reflect.TypeOf(msg).String(), "sequenceMsg") {
		for i := 0; i < v.Len(); i++ {
			if c, ok := v.Index(i).Interface().(tea.Cmd); ok {
				drain(t, a, c)
			}
		}
		return
	}
	if strings.Contains(reflect.TypeOf(msg).String(), "spinner") || strings.Contains(reflect.TypeOf(msg).String(), "execMsg") {
		return
	}
	_, next := a.Update(msg)
	drain(t, a, next)
}

func key(s string) tea.KeyMsg {
	switch s {
	case "enter":
		return tea.KeyMsg{Type: tea.KeyEnter}
	case "esc":
		return tea.KeyMsg{Type: tea.KeyEsc}
	case "down":
		return tea.KeyMsg{Type: tea.KeyDown}
	case "up":
		return tea.KeyMsg{Type: tea.KeyUp}
	case "tab":
		return tea.KeyMsg{Type: tea.KeyTab}
	}
	return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(s)}
}

func press(t *testing.T, a *App, keys ...string) {
	t.Helper()
	for _, k := range keys {
		_, cmd := a.Update(key(k))
		drain(t, a, cmd)
	}
}

func typeText(t *testing.T, a *App, s string) {
	t.Helper()
	for _, r := range s {
		_, cmd := a.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}})
		drain(t, a, cmd)
	}
}

func screen(a *App) string { return ansi.Strip(a.View()) }

func TestSignInAndSwapThroughTheSwapperTab(t *testing.T) {
	a, api, _ := newTestApp(t)
	press(t, a, "2")
	if s := screen(a); !strings.Contains(s, "Sign in to Spark Swapper") {
		t.Fatalf("signed-out screen:\n%s", s)
	}
	press(t, a, "enter")
	if a.dlg == nil {
		t.Fatal("no sign-in dialog")
	}
	typeText(t, a, "admin")
	press(t, a, "enter")
	if s := screen(a); !strings.Contains(s, "Password for admin") {
		t.Fatalf("no password prompt:\n%s", s)
	}
	typeText(t, a, "pw-123456")
	press(t, a, "enter")
	if strings.Contains(screen(a), "pw-123456") {
		t.Fatal("the password shows on screen")
	}
	drain(t, a, a.refreshTab(1, true))
	s := screen(a)
	if !strings.Contains(s, "Qwen3.8 Flash Next") || !strings.Contains(s, "serving") {
		t.Fatalf("models not listed:\n%s", s)
	}
	if a.D.State.SwapperUser != "admin" {
		t.Fatalf("user not remembered: %+v", a.D.State)
	}
	// select Flash Next (TensorFold) and swap
	a.tabs[1].(*swapperTab).tbl.Select("tfflash")
	press(t, a, "enter")
	if s := screen(a); !strings.Contains(s, "Swap to Qwen3.8 Flash Next?") {
		t.Fatalf("no confirmation:\n%s", s)
	}
	press(t, a, "y")
	if got := api.posts["/api/swap"]; !strings.Contains(got, `"target":"tfflash"`) {
		t.Fatalf("swap posted %q", got)
	}
	if a.view == nil || !strings.Contains(screen(a), "stopping tf27b") {
		t.Fatalf("the job log should open:\n%s", screen(a))
	}
	press(t, a, "esc")
	// options: the 512K window needs its build first
	press(t, a, "o")
	if s := screen(a); !strings.Contains(s, "Window 512K") {
		t.Fatalf("options:\n%s", s)
	}
	press(t, a, "esc")
}

func TestDockerRemoveAsksThenRunsDockerRm(t *testing.T) {
	a, _, f := newTestApp(t)
	press(t, a, "4")
	drain(t, a, a.refreshTab(3, true))
	if s := screen(a); !strings.Contains(s, "old-thing") || !strings.Contains(s, "sparkia-vision") {
		t.Fatalf("containers:\n%s", s)
	}
	a.tabs[3].(*dockerTab).ctbl.Select("old-thing")
	press(t, a, "x")
	if s := screen(a); !strings.Contains(s, "Remove container old-thing?") || !strings.Contains(s, "docker rm old-thing") {
		t.Fatalf("confirmation:\n%s", s)
	}
	press(t, a, "n")
	if f.Called("docker rm old-thing") {
		t.Fatal("ran without a yes")
	}
	press(t, a, "x", "y")
	if !f.Called("docker rm old-thing") {
		t.Fatalf("calls: %v", f.Calls)
	}
	// a Sparkia container warns that compose manages it
	a.tabs[3].(*dockerTab).ctbl.Select("sparkia-vision")
	press(t, a, "s")
	if s := screen(a); !strings.Contains(s, "part of Sparkia") {
		t.Fatalf("no warning:\n%s", s)
	}
	press(t, a, "esc")
	// filter
	press(t, a, "/")
	typeText(t, a, "old")
	press(t, a, "enter")
	if s := screen(a); strings.Contains(s, "sparkia-vision") {
		t.Fatalf("filter didn't narrow:\n%s", s)
	}
}

func TestLogViewerFollowsSearchesAndSaves(t *testing.T) {
	a, _, f := newTestApp(t)
	f.Streamed["docker logs --timestamps --tail 500 --follow old-thing"] = []string{"booting", "\x1b[31mERROR\x1b[0m disk full", "ready"}
	press(t, a, "4")
	drain(t, a, a.refreshTab(3, true))
	a.tabs[3].(*dockerTab).ctbl.Select("old-thing")
	press(t, a, "enter")
	if a.view == nil {
		t.Fatal("no viewer")
	}
	deadline := time.Now().Add(2 * time.Second)
	for len(a.view.lines) < 3 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	s := screen(a)
	if !strings.Contains(s, "disk full") || !strings.Contains(s, "ready") {
		t.Fatalf("log:\n%s", s)
	}
	press(t, a, "/")
	typeText(t, a, "error")
	press(t, a, "enter")
	if a.view.search != "error" {
		t.Fatalf("search %q", a.view.search)
	}
	press(t, a, "F")
	if s := screen(a); strings.Contains(s, "booting") {
		t.Fatalf("only matches:\n%s", s)
	}
	t.Setenv("HOME", t.TempDir())
	press(t, a, "s")
	if !strings.Contains(a.view.status, "Saved 3 lines") {
		t.Fatalf("save: %s", a.view.status)
	}
	press(t, a, "esc", "esc")
	if a.view != nil {
		t.Fatal("viewer still open")
	}
}

func TestEveryTabDrawsAtEverySize(t *testing.T) {
	a, _, _ := newTestApp(t)
	for i := range a.tabs {
		drain(t, a, a.refreshTab(i, true))
	}
	for _, size := range [][2]int{{50, 12}, {80, 24}, {120, 40}, {220, 60}, {30, 8}} {
		a.Update(tea.WindowSizeMsg{Width: size[0], Height: size[1]})
		for i := range a.tabs {
			a.active = i
			out := a.View()
			lines := strings.Split(out, "\n")
			if size[0] >= 50 && len(lines) > size[1] {
				t.Errorf("tab %d at %v: %d lines", i, size, len(lines))
			}
			for _, l := range lines {
				if w := ansi.StringWidth(l); w > size[0] {
					t.Errorf("tab %d at %v: a line %d wide", i, size, w)
					break
				}
			}
		}
	}
	press(t, a, "?")
	if a.dlg == nil {
		t.Fatal("no help")
	}
}

func TestRootActionsShowTheirCommandAndSparkiaNotInstalledIsClear(t *testing.T) {
	a, _, _ := newTestApp(t)
	press(t, a, "3")
	drain(t, a, a.refreshTab(2, true))
	if s := screen(a); !strings.Contains(s, "Sparkia isn't installed here") {
		t.Fatalf("%s", s)
	}
	press(t, a, "5")
	drain(t, a, a.refreshTab(4, true))
	if s := screen(a); !strings.Contains(s, "spark-swapper.service") {
		t.Fatalf("services:\n%s", s)
	}
	press(t, a, "r")
	if s := screen(a); !strings.Contains(s, "sudo systemctl restart spark-swapper.service") {
		t.Fatalf("restart should show its command:\n%s", s)
	}
}
