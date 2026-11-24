package swapper

import (
	"sort"
	"time"
)

// Session is GET /api/session.
type Session struct {
	SetupNeeded   bool   `json:"setup_needed"`
	User          string `json:"user"`
	Role          string `json:"role"`
	Via           string `json:"via"`
	Demo          bool   `json:"demo"`
	Version       string `json:"version"`
	Platform      string `json:"platform"`
	PlatformLabel string `json:"platform_label"`
	Hostname      string `json:"hostname"`
}

// Status is GET /api/status.
type Status struct {
	Active          string           `json:"active"`
	Loading         string           `json:"loading"`
	Desired         string           `json:"desired"`
	Models          map[string]Model `json:"models"`
	Memory          Memory           `json:"memory"`
	OpenclawPrimary string           `json:"openclaw_primary"`
	Nginx           bool             `json:"nginx"`
	Front           Front            `json:"front"`
	Gateway         bool             `json:"gateway"`
	Vision          *Vision          `json:"vision"`
	Busy            bool             `json:"busy"`
	Measuring       string           `json:"measuring"`
	Version         string           `json:"version"`
	Platform        string           `json:"platform"`
	PlatformLabel   string           `json:"platform_label"`
	Hostname        string           `json:"hostname"`
	Disk            *Disk            `json:"disk"`
	Job             *Job             `json:"job"`
	InstallJob      *Job             `json:"install_job"`
	Update          *UpdateBrief     `json:"update"`
	Pictures        *Pictures        `json:"pictures"`
}

// Sorted is the models in the page's order.
func (s *Status) Sorted() []Model {
	out := make([]Model, 0, len(s.Models))
	for _, m := range s.Models {
		out = append(out, m)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Order != out[j].Order {
			return out[i].Order < out[j].Order
		}
		return out[i].Title < out[j].Title
	})
	return out
}

type Memory struct {
	Total     float64 `json:"total"`
	Available float64 `json:"available"`
	Free      float64 `json:"free"`
}

type Front struct {
	Up          bool `json:"up"`
	Ours        bool `json:"ours"`
	EnforcesKey bool `json:"enforces_key"`
}

type Vision struct {
	Port int  `json:"port"`
	Up   bool `json:"up"`
}

type Disk struct {
	Total float64 `json:"total"`
	Free  float64 `json:"free"`
}

type UpdateBrief struct {
	Available   bool   `json:"available"`
	Latest      string `json:"latest"`
	AutoInstall bool   `json:"auto_install"`
}

type Pictures struct {
	Installed    bool    `json:"installed"`
	Enabled      bool    `json:"enabled"`
	Running      bool    `json:"running"`
	Ready        bool    `json:"ready"`
	Room         bool    `json:"room"`
	AvailableGiB float64 `json:"available_gib"`
	NeedGiB      float64 `json:"need_gib"`
	Summary      string  `json:"summary"`
}

// Model is one card.
type Model struct {
	Key           string       `json:"key"`
	Title         string       `json:"title"`
	Tagline       string       `json:"tagline"`
	Engine        string       `json:"engine"`
	Context       int          `json:"context"`
	ServedContext int          `json:"served_context"`
	ServedID      string       `json:"served_id"`
	Vision        bool         `json:"vision"`
	Running       bool         `json:"running"`
	Healthy       bool         `json:"healthy"`
	Order         int          `json:"order"`
	Kind          string       `json:"kind"`
	Installed     bool         `json:"installed"`
	InstallDetail string       `json:"install_detail"`
	Install       *InstallInfo `json:"install"`
	Updatable     bool         `json:"updatable"`
	Perf          *Perf        `json:"perf"`
	Measured      *Perf        `json:"measured"`
	Recommended   bool         `json:"recommended"`
	SupersededBy  string       `json:"superseded_by"`
	ETA           string       `json:"eta"`
	Repo          string       `json:"repo"`
	Experimental  bool         `json:"experimental"`
	Custom        bool         `json:"custom"`
	HFRepos       []string     `json:"hf_repos"`
	DiskBytes     float64      `json:"disk_bytes"`
	Notes         []string     `json:"notes"`
	Contexts      []ContextOpt `json:"contexts"`
	Variant       string       `json:"variant"`
	Switches      []Switch     `json:"switches"`
	Inputs        []string     `json:"inputs"`
}

type InstallInfo struct {
	SizeGB   float64  `json:"size_gb"`
	Time     string   `json:"time"`
	Requires []string `json:"requires"`
	Note     string   `json:"note"`
}

type Perf struct {
	Decode           float64 `json:"decode"`
	DecodeConcurrent float64 `json:"decode_concurrent"`
	Concurrency      int     `json:"concurrency"`
	Prefill          float64 `json:"prefill"`
	Source           string  `json:"source"`
	Estimate         bool    `json:"estimate"`
}

// ContextOpt is one window a model offers.
type ContextOpt struct {
	Tokens       int          `json:"tokens"`
	Label        string       `json:"label"`
	Detail       string       `json:"detail"`
	Pictures     bool         `json:"pictures"`
	Variant      string       `json:"variant"`
	VariantLabel string       `json:"variant_label"`
	Installed    bool         `json:"installed"`
	Install      *InstallInfo `json:"install"`
}

// Switch is one on/off option a model offers.
type Switch struct {
	Name      string `json:"name"`
	Label     string `json:"label"`
	OffLabel  string `json:"off_label"`
	Detail    string `json:"detail"`
	AdminOnly bool   `json:"admin_only"`
	On        bool   `json:"on"`
	Default   bool   `json:"default"`
	Warn      string `json:"warn"`
	Available *bool  `json:"available"`
	Why       string `json:"why"`
}

// Job is a swap, settings change, install or delete.
type Job struct {
	ID              string    `json:"id"`
	Kind            string    `json:"kind"`
	Target          string    `json:"target"`
	Source          string    `json:"source"`
	Change          string    `json:"change"`
	Action          string    `json:"action"`
	Title           string    `json:"title"`
	State           string    `json:"state"`
	CancelRequested bool      `json:"cancel_requested"`
	Error           string    `json:"error"`
	Note            string    `json:"note"`
	Started         float64   `json:"started"`
	Finished        float64   `json:"finished"`
	Steps           []Step    `json:"steps"`
	LogTotal        int       `json:"log_total"`
	Log             []LogLine `json:"log"`
}

// Running reports whether the job is still going.
func (j *Job) Running() bool { return j != nil && (j.State == "running" || j.State == "pending") }

// StartedAt is when the job started.
func (j *Job) StartedAt() time.Time { return unix(j.Started) }

// FinishedAt is when it ended (zero while it runs).
func (j *Job) FinishedAt() time.Time { return unix(j.Finished) }

func unix(f float64) time.Time {
	if f <= 0 {
		return time.Time{}
	}
	return time.Unix(int64(f), int64((f-float64(int64(f)))*1e9))
}

type Step struct {
	Key    string `json:"key"`
	Label  string `json:"label"`
	State  string `json:"state"`
	Detail string `json:"detail"`
}

type LogLine struct {
	T float64 `json:"t"`
	M string  `json:"m"`
}

// At is when the line was written.
func (l LogLine) At() time.Time { return unix(l.T) }

// Storage is GET /api/storage.
type Storage struct {
	Disks []struct {
		Mount string  `json:"mount"`
		Total float64 `json:"total"`
		Used  float64 `json:"used"`
		Free  float64 `json:"free"`
		Roles []struct {
			Label string `json:"label"`
			Path  string `json:"path"`
		} `json:"roles"`
	} `json:"disks"`
	Models []struct {
		Key        string  `json:"key"`
		Title      string  `json:"title"`
		Installed  bool    `json:"installed"`
		Bytes      float64 `json:"bytes"`
		BytesFreed float64 `json:"bytes_freed"`
	} `json:"models"`
	Busy bool `json:"busy"`
}

type Updates struct {
	Current     string  `json:"current"`
	Latest      string  `json:"latest"`
	Available   bool    `json:"available"`
	AutoInstall bool    `json:"auto_install"`
	Check       bool    `json:"check"`
	LastCheck   float64 `json:"last_check"`
	LastError   string  `json:"last_error"`
	Notes       string  `json:"notes"`
	Published   string  `json:"published"`
}

type ProvisionItem struct {
	Present bool   `json:"present"`
	Detail  string `json:"detail"`
	Label   string `json:"label"`
	Desc    string `json:"desc"`
	Order   int    `json:"order"`
}

type User struct {
	Username string `json:"username"`
	Role     string `json:"role"`
}
