package swapper

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
)

// fakeSwapper serves the API the way Spark Swapper does: a cookie from /api/login, 401 without it, the X-Swapper
// header required on every POST.
type fakeSwapper struct {
	mu      sync.Mutex
	status  []byte
	swapped string
	posts   []string
}

func (f *fakeSwapper) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	if r.Method == http.MethodPost {
		if r.Header.Get("X-Swapper") != "1" {
			w.WriteHeader(403)
			w.Write([]byte(`{"error":"Cross-site request refused."}`))
			return
		}
		f.posts = append(f.posts, r.URL.Path)
	}
	switch r.URL.Path {
	case "/api/session":
		w.Write([]byte(`{"setup_needed":false,"user":null,"version":"2.3.1","hostname":"spark"}`))
		return
	case "/api/login":
		var b map[string]string
		json.NewDecoder(r.Body).Decode(&b)
		if b["username"] != "admin" || b["password"] != "secret-pw" {
			w.WriteHeader(401)
			w.Write([]byte(`{"error":"That username and password don't match."}`))
			return
		}
		http.SetCookie(w, &http.Cookie{Name: "swapper_session", Value: "sess-123", Path: "/"})
		w.Write([]byte(`{"ok":true}`))
		return
	}
	if ck, err := r.Cookie("swapper_session"); err != nil || ck.Value != "sess-123" {
		w.WriteHeader(401)
		w.Write([]byte(`{"error":"Please sign in."}`))
		return
	}
	switch r.URL.Path {
	case "/api/status":
		w.Write(f.status)
	case "/api/swap":
		var b map[string]string
		json.NewDecoder(r.Body).Decode(&b)
		f.swapped = b["target"]
		if b["target"] == "nope" {
			w.WriteHeader(400)
			w.Write([]byte(`{"error":"Unknown model."}`))
			return
		}
		w.WriteHeader(202)
		w.Write([]byte(`{"ok":true,"job":{"id":"j1","kind":"swap","target":"` + b["target"] + `","state":"running","steps":[{"key":"stop","label":"Stopping","state":"running"}]}}`))
	case "/api/settings":
		w.Write([]byte(`{"ok":true,"restarting":false}`))
	default:
		w.WriteHeader(404)
		w.Write([]byte(`{"error":"not found"}`))
	}
}

func newFake(t *testing.T) (*fakeSwapper, *httptest.Server) {
	t.Helper()
	b, err := os.ReadFile("testdata/status.json")
	if err != nil {
		t.Fatal(err)
	}
	f := &fakeSwapper{status: b}
	srv := httptest.NewTLSServer(f)
	t.Cleanup(srv.Close)
	return f, srv
}

func TestSignInThenStatusParsesTheSwappersRealAnswer(t *testing.T) {
	_, srv := newFake(t)
	var saved string
	c := New(srv.URL, "", "")
	c.OnSession = func(v string) { saved = v }
	ctx := context.Background()
	if _, err := c.Status(ctx); !errors.Is(err, ErrSignIn) {
		t.Fatalf("status before sign-in: %v, want ErrSignIn", err)
	}
	if err := c.Login(ctx, "admin", "wrong"); err == nil || !strings.Contains(err.Error(), "don't match") {
		t.Fatalf("bad password: %v", err)
	}
	if err := c.Login(ctx, "admin", "secret-pw"); err != nil {
		t.Fatal(err)
	}
	if saved != "sess-123" || !c.SignedIn() {
		t.Fatalf("session not kept: %q", saved)
	}
	st, err := c.Status(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if st.Active != "tf27b" || len(st.Models) < 5 {
		t.Fatalf("active %q, %d models", st.Active, len(st.Models))
	}
	m := st.Models["tfflash"]
	if m.Title != "Qwen3.8 Flash Next" || len(m.Contexts) != 4 || len(m.Switches) == 0 {
		t.Fatalf("tfflash: %+v", m)
	}
	var long *ContextOpt
	for i := range m.Contexts {
		if m.Contexts[i].Label == "512K" {
			long = &m.Contexts[i]
		}
	}
	if long == nil || long.Variant != "long" || long.Installed || long.Install == nil || long.Install.SizeGB != 160 {
		t.Fatalf("512K window: %+v", long)
	}
	sorted := st.Sorted()
	for i := 1; i < len(sorted); i++ {
		if sorted[i-1].Order > sorted[i].Order {
			t.Fatalf("not in the page's order: %v then %v", sorted[i-1].Key, sorted[i].Key)
		}
	}
}

func TestSwapSendsTheSwappersHeaderAndReportsItsErrors(t *testing.T) {
	f, srv := newFake(t)
	c := New(srv.URL, "sess-123", "")
	ctx := context.Background()
	j, err := c.Swap(ctx, "bolt")
	if err != nil {
		t.Fatal(err)
	}
	if f.swapped != "bolt" || j.ID != "j1" || !j.Running() {
		t.Fatalf("swapped %q, job %+v", f.swapped, j)
	}
	_, err = c.Swap(ctx, "nope")
	var api *APIError
	if !errors.As(err, &api) || api.Status != 400 || api.Msg != "Unknown model." {
		t.Fatalf("error: %v", err)
	}
	job, err := c.Settings(ctx, "tfflash", 524288, map[string]bool{"ablit": true})
	if err != nil || job != nil {
		t.Fatalf("settings: %v %v", job, err)
	}
}

func TestTheCertificateIsPinnedOnFirstUseAndAChangeIsRefused(t *testing.T) {
	_, srv := newFake(t)
	var pinned string
	c := New(srv.URL, "", "")
	c.OnPin = func(fp string) { pinned = fp }
	if _, err := c.Session(context.Background()); err != nil {
		t.Fatal(err)
	}
	want := Fingerprint(srv.Certificate().Raw)
	if c.Pin() != want || pinned != want {
		t.Fatalf("pin %s (saved %s), want %s", c.Pin(), pinned, want)
	}
	// a different certificate (another server): refused until trusted
	other := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(`{}`)) }))
	defer other.Close()
	c2 := New(other.URL, "", "deadbeef")
	_, err := c2.Session(context.Background())
	var changed *ErrCertChanged
	if !errors.As(err, &changed) {
		t.Fatalf("got %v, want ErrCertChanged", err)
	}
	c2.TrustSeen()
	if _, err := c2.Session(context.Background()); err != nil {
		t.Fatalf("after trusting: %v", err)
	}
}

func TestAnUnreachableSwapperSaysSo(t *testing.T) {
	c := New("https://127.0.0.1:1", "", "")
	_, err := c.Session(context.Background())
	if err == nil || !strings.Contains(err.Error(), "can't reach Spark Swapper") {
		t.Fatalf("got %v", err)
	}
}
