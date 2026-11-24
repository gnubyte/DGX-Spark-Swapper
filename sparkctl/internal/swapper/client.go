// Package swapper talks to Spark Swapper's HTTPS API (the one its web page uses): sign-in, status, swaps, installs,
// model options, storage and updates.
package swapper

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// ErrSignIn: the Swapper wants a sign-in (none yet, or the session ended).
var ErrSignIn = errors.New("sign in to Spark Swapper")

// ErrCertChanged: the Swapper's certificate isn't the one trusted before.
type ErrCertChanged struct {
	Want, Got string
}

func (e *ErrCertChanged) Error() string {
	return fmt.Sprintf("the Swapper's certificate changed (trusted %s…, now %s…): if you reinstalled it, trust the new one (T)",
		short(e.Want), short(e.Got))
}

func short(fp string) string {
	if len(fp) > 16 {
		return fp[:16]
	}
	return fp
}

// APIError is an error the Swapper answered with.
type APIError struct {
	Status int
	Msg    string
}

func (e *APIError) Error() string { return e.Msg }

// Client is one Swapper. It trusts the certificate it saw first (the Swapper's is self-signed) and keeps the
// session cookie the sign-in returns.
type Client struct {
	Base string

	mu      sync.Mutex
	session string
	pin     string // sha256 of the trusted certificate, hex
	seen    string // the certificate's fingerprint on the last connection
	http    *http.Client
	// OnSession is called with a new session cookie (to keep it for the next run); "" when it ended.
	OnSession func(string)
	// OnPin is called with the fingerprint first trusted.
	OnPin func(string)
}

// New returns a client for base (https://127.0.0.1:8999), trusting pin (empty: the first certificate seen).
func New(base, session, pin string) *Client {
	c := &Client{Base: strings.TrimRight(base, "/"), session: session, pin: strings.ToLower(pin)}
	tr := &http.Transport{
		Proxy:               nil,
		DialContext:         (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout: 8 * time.Second,
		MaxIdleConns:        4,
		IdleConnTimeout:     60 * time.Second,
		TLSClientConfig: &tls.Config{
			MinVersion: tls.VersionTLS12,
			// The Swapper's certificate is self-signed: it's checked against the pinned fingerprint instead.
			InsecureSkipVerify: true,
			VerifyConnection:   c.verify,
		},
	}
	c.http = &http.Client{Transport: tr, Timeout: 30 * time.Second}
	return c
}

// Fingerprint returns the sha256 of a DER certificate, hex.
func Fingerprint(der []byte) string {
	sum := sha256.Sum256(der)
	return hex.EncodeToString(sum[:])
}

func (c *Client) verify(cs tls.ConnectionState) error {
	if len(cs.PeerCertificates) == 0 {
		return errors.New("the Swapper sent no certificate")
	}
	got := Fingerprint(cs.PeerCertificates[0].Raw)
	c.mu.Lock()
	c.seen = got
	first := c.pin == ""
	if first {
		c.pin = got
	}
	want, cb := c.pin, c.OnPin
	c.mu.Unlock()
	if first {
		if cb != nil {
			cb(got)
		}
		return nil
	}
	if want != got {
		return &ErrCertChanged{Want: want, Got: got}
	}
	return nil
}

// TrustSeen pins the certificate seen last (after the person confirmed it changed on purpose).
func (c *Client) TrustSeen() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.pin = c.seen
	return c.pin
}

// Pin is the fingerprint trusted now.
func (c *Client) Pin() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.pin
}

// TrustCA trusts a CA (for a Swapper behind a real certificate): verification then goes through the system roots.
func (c *Client) TrustCA(pool *x509.CertPool) {
	tr := c.http.Transport.(*http.Transport)
	tr.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool}
}

// SignedIn reports whether a session cookie is held.
func (c *Client) SignedIn() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.session != ""
}

func (c *Client) setSession(v string) {
	c.mu.Lock()
	c.session = v
	cb := c.OnSession
	c.mu.Unlock()
	if cb != nil {
		cb(v)
	}
}

func (c *Client) do(ctx context.Context, method, path string, body any, out any) error {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Base+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "sparkctl")
	if method == http.MethodPost {
		// The Swapper refuses a state change without its own header (no cross-site form can send it).
		req.Header.Set("X-Swapper", "1")
		req.Header.Set("Content-Type", "application/json")
	}
	c.mu.Lock()
	if c.session != "" {
		req.AddCookie(&http.Cookie{Name: "swapper_session", Value: c.session})
	}
	c.mu.Unlock()
	resp, err := c.http.Do(req)
	if err != nil {
		var changed *ErrCertChanged
		if errors.As(err, &changed) {
			return changed
		}
		var ue *url.Error
		if errors.As(err, &ue) && ue.Timeout() {
			return fmt.Errorf("Spark Swapper at %s didn't answer in time", c.Base)
		}
		return fmt.Errorf("can't reach Spark Swapper at %s: %w", c.Base, unwrapNet(err))
	}
	defer resp.Body.Close()
	for _, ck := range resp.Cookies() {
		if ck.Name == "swapper_session" {
			c.setSession(ck.Value)
		}
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		if path != "/api/login" {
			c.setSession("")
			return ErrSignIn
		}
	}
	if resp.StatusCode >= 400 {
		var e struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(data, &e) == nil && e.Error != "" {
			return &APIError{Status: resp.StatusCode, Msg: e.Error}
		}
		return &APIError{Status: resp.StatusCode, Msg: fmt.Sprintf("Spark Swapper answered %s", resp.Status)}
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			return fmt.Errorf("unexpected answer from Spark Swapper: %w", err)
		}
	}
	return nil
}

func unwrapNet(err error) error {
	var op *net.OpError
	if errors.As(err, &op) {
		if op.Err != nil {
			return op.Err
		}
	}
	return err
}

func (c *Client) get(ctx context.Context, path string, out any) error {
	return c.do(ctx, http.MethodGet, path, nil, out)
}

func (c *Client) post(ctx context.Context, path string, body, out any) error {
	if body == nil {
		body = map[string]any{}
	}
	return c.do(ctx, http.MethodPost, path, body, out)
}

// Session is GET /api/session (no sign-in needed).
func (c *Client) Session(ctx context.Context) (Session, error) {
	var s Session
	err := c.get(ctx, "/api/session", &s)
	return s, err
}

// Login signs in; the session cookie is kept.
func (c *Client) Login(ctx context.Context, user, password string) error {
	var r struct {
		OK bool `json:"ok"`
	}
	return c.post(ctx, "/api/login", map[string]string{"username": user, "password": password}, &r)
}

// Logout ends the session.
func (c *Client) Logout(ctx context.Context) error {
	err := c.post(ctx, "/api/logout", nil, nil)
	c.setSession("")
	return err
}

// Status is GET /api/status.
func (c *Client) Status(ctx context.Context) (*Status, error) {
	var s Status
	if err := c.get(ctx, "/api/status", &s); err != nil {
		return nil, err
	}
	return &s, nil
}

type jobReply struct {
	OK         bool `json:"ok"`
	Job        *Job `json:"job"`
	Restarting bool `json:"restarting"`
}

// Swap starts a swap to the model key.
func (c *Client) Swap(ctx context.Context, key string) (*Job, error) {
	var r jobReply
	err := c.post(ctx, "/api/swap", map[string]string{"target": key}, &r)
	return r.Job, err
}

// Cancel cancels the running swap or settings job.
func (c *Client) Cancel(ctx context.Context) error {
	return c.post(ctx, "/api/swap/cancel", nil, nil)
}

// Settings changes a model's window and switches; the job is nil when nothing had to restart.
func (c *Client) Settings(ctx context.Context, key string, context int, switches map[string]bool) (*Job, error) {
	body := map[string]any{"key": key}
	if context > 0 {
		body["context"] = context
	}
	if len(switches) > 0 {
		body["switches"] = switches
	}
	var r jobReply
	err := c.post(ctx, "/api/settings", body, &r)
	return r.Job, err
}

// Install installs a model (or one of its other builds, variant).
func (c *Client) Install(ctx context.Context, key, variant string) (*Job, error) {
	body := map[string]string{"key": key}
	if variant != "" {
		body["variant"] = variant
	}
	var r jobReply
	err := c.post(ctx, "/api/models/install", body, &r)
	return r.Job, err
}

// Delete removes a model's files (admin).
func (c *Client) Delete(ctx context.Context, key string) (*Job, error) {
	var r jobReply
	err := c.post(ctx, "/api/models/delete", map[string]string{"key": key}, &r)
	return r.Job, err
}

// Measure runs the speed check on the serving model.
func (c *Client) Measure(ctx context.Context) error { return c.post(ctx, "/api/measure", nil, nil) }

// Pictures turns Qwen-Image on or off.
func (c *Client) Pictures(ctx context.Context, on bool) error {
	return c.post(ctx, "/api/pictures", map[string]bool{"enabled": on}, nil)
}

// Activity is the Swapper's recent activity log.
func (c *Client) Activity(ctx context.Context) ([]string, error) {
	var r struct {
		Lines []string `json:"lines"`
	}
	err := c.get(ctx, "/api/activity", &r)
	return r.Lines, err
}

// Storage is GET /api/storage.
func (c *Client) Storage(ctx context.Context, fresh bool) (*Storage, error) {
	var s Storage
	p := "/api/storage"
	if fresh {
		p += "?fresh=1"
	}
	err := c.get(ctx, p, &s)
	return &s, err
}

// Updates is GET /api/updates (admin).
func (c *Client) Updates(ctx context.Context) (*Updates, error) {
	var u Updates
	err := c.get(ctx, "/api/updates", &u)
	return &u, err
}

// CheckUpdates asks GitHub for a newer Swapper (admin).
func (c *Client) CheckUpdates(ctx context.Context) (*Updates, error) {
	var u Updates
	err := c.post(ctx, "/api/updates/check", nil, &u)
	return &u, err
}

// InstallUpdate installs the newest Swapper (admin); the Swapper restarts.
func (c *Client) InstallUpdate(ctx context.Context) error {
	return c.post(ctx, "/api/updates/install", nil, nil)
}

// Provisioning is GET /api/provisioning: what the host has (git, docker, recipes, nginx, …).
func (c *Client) Provisioning(ctx context.Context) (map[string]ProvisionItem, error) {
	var p map[string]ProvisionItem
	err := c.get(ctx, "/api/provisioning", &p)
	return p, err
}

// ProvisioningJob is the running (or last) provisioning / install job.
func (c *Client) ProvisioningJob(ctx context.Context) (*Job, error) {
	var r struct {
		Job *Job `json:"job"`
	}
	err := c.get(ctx, "/api/provisioning/job", &r)
	return r.Job, err
}

// Connect is GET /api/connect: how apps reach the serving model.
func (c *Client) Connect(ctx context.Context) (map[string]any, error) {
	var m map[string]any
	err := c.get(ctx, "/api/connect", &m)
	return m, err
}

// Users is GET /api/users (admin).
func (c *Client) Users(ctx context.Context) ([]User, error) {
	var r struct {
		Users []User `json:"users"`
	}
	err := c.get(ctx, "/api/users", &r)
	return r.Users, err
}
