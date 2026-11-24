// Package host reads the machine's own state: memory, disks, load, uptime and the GPU.
package host

import (
	"bufio"
	"context"
	"encoding/csv"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
)

// Memory from /proc/meminfo, in bytes.
type Memory struct {
	Total, Available, Free, SwapTotal, SwapFree uint64
}

// UsedPct is how much of the memory is taken.
func (m Memory) UsedPct() float64 {
	if m.Total == 0 {
		return 0
	}
	return 100 * float64(m.Total-m.Available) / float64(m.Total)
}

// ParseMeminfo reads /proc/meminfo's text.
func ParseMeminfo(text string) Memory {
	var m Memory
	sc := bufio.NewScanner(strings.NewReader(text))
	for sc.Scan() {
		f := strings.Fields(sc.Text())
		if len(f) < 2 {
			continue
		}
		n, err := strconv.ParseUint(f[1], 10, 64)
		if err != nil {
			continue
		}
		if len(f) > 2 && f[2] == "kB" {
			n *= 1024
		}
		switch strings.TrimSuffix(f[0], ":") {
		case "MemTotal":
			m.Total = n
		case "MemAvailable":
			m.Available = n
		case "MemFree":
			m.Free = n
		case "SwapTotal":
			m.SwapTotal = n
		case "SwapFree":
			m.SwapFree = n
		}
	}
	return m
}

// ReadMemory reads this machine's memory.
func ReadMemory() (Memory, error) {
	b, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return Memory{}, err
	}
	return ParseMeminfo(string(b)), nil
}

// Load is the 1, 5 and 15-minute load averages.
type Load [3]float64

// ReadLoad reads /proc/loadavg.
func ReadLoad() (Load, error) {
	var l Load
	b, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return l, err
	}
	f := strings.Fields(string(b))
	for i := 0; i < 3 && i < len(f); i++ {
		l[i], _ = strconv.ParseFloat(f[i], 64)
	}
	return l, nil
}

// ReadUptime reads /proc/uptime.
func ReadUptime() (time.Duration, error) {
	b, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0, err
	}
	f := strings.Fields(string(b))
	if len(f) == 0 {
		return 0, fmt.Errorf("empty /proc/uptime")
	}
	s, err := strconv.ParseFloat(f[0], 64)
	return time.Duration(s * float64(time.Second)), err
}

// Hostname is the machine's name.
func Hostname() string {
	h, _ := os.Hostname()
	return h
}

// Disk is one filesystem's space.
type Disk struct {
	Path   string
	Mount  string
	Device string
	Total  uint64
	Free   uint64 // available to unprivileged users
}

// UsedPct is how full the disk is.
func (d Disk) UsedPct() float64 {
	if d.Total == 0 {
		return 0
	}
	return 100 * float64(d.Total-d.Free) / float64(d.Total)
}

// GPU is one row of nvidia-smi.
type GPU struct {
	Name      string
	Util      string // %
	MemUsed   string // MiB, or "[N/A]" on unified memory
	MemTotal  string
	Temp      string // °C
	Power     string // W
	Driver    string
	Processes []GPUProcess
}

type GPUProcess struct {
	PID  string
	Name string
	Mem  string
}

// ReadGPUs asks nvidia-smi; nil without an NVIDIA GPU.
func ReadGPUs(ctx context.Context, r run.Runner) ([]GPU, error) {
	if !r.Has("nvidia-smi") {
		return nil, nil
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	res, err := r.Run(ctx, false, "nvidia-smi",
		"--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,driver_version",
		"--format=csv,noheader,nounits")
	if err != nil {
		return nil, err
	}
	gpus := ParseGPUs(res.Stdout)
	if len(gpus) > 0 {
		pr, err := r.Run(ctx, false, "nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits")
		if err == nil {
			gpus[0].Processes = ParseGPUProcesses(pr.Stdout)
		}
	}
	return gpus, nil
}

// ParseGPUs reads nvidia-smi's CSV.
func ParseGPUs(out string) []GPU {
	var gpus []GPU
	rd := csv.NewReader(strings.NewReader(out))
	rd.TrimLeadingSpace = true
	rd.FieldsPerRecord = -1
	recs, _ := rd.ReadAll()
	for _, f := range recs {
		if len(f) < 7 {
			continue
		}
		gpus = append(gpus, GPU{Name: f[0], Util: f[1], MemUsed: f[2], MemTotal: f[3], Temp: f[4], Power: f[5], Driver: f[6]})
	}
	return gpus
}

// ParseGPUProcesses reads nvidia-smi's compute-apps CSV.
func ParseGPUProcesses(out string) []GPUProcess {
	var ps []GPUProcess
	rd := csv.NewReader(strings.NewReader(out))
	rd.TrimLeadingSpace = true
	rd.FieldsPerRecord = -1
	recs, _ := rd.ReadAll()
	for _, f := range recs {
		if len(f) >= 3 {
			ps = append(ps, GPUProcess{PID: f[0], Name: f[1], Mem: f[2]})
		}
	}
	return ps
}

// Bytes renders a size the way df -h does: 1.8T, 97G, 512M.
func Bytes(n uint64) string { return BytesF(float64(n)) }

// BytesF is Bytes for a float.
func BytesF(n float64) string {
	units := []string{"B", "K", "M", "G", "T", "P"}
	i := 0
	for n >= 1024 && i < len(units)-1 {
		n /= 1024
		i++
	}
	if i == 0 {
		return fmt.Sprintf("%.0fB", n)
	}
	if n < 10 {
		return fmt.Sprintf("%.1f%s", n, units[i])
	}
	return fmt.Sprintf("%.0f%s", n, units[i])
}

// Duration renders an uptime: 3d 4h, 5h 12m, 7m.
func Duration(d time.Duration) string {
	d = d.Round(time.Minute)
	days := int(d.Hours()) / 24
	h := int(d.Hours()) % 24
	m := int(d.Minutes()) % 60
	switch {
	case days > 0:
		return fmt.Sprintf("%dd %dh", days, h)
	case h > 0:
		return fmt.Sprintf("%dh %dm", h, m)
	default:
		return fmt.Sprintf("%dm", m)
	}
}

// Ago renders how long ago t was.
func Ago(t time.Time) string {
	if t.IsZero() {
		return "—"
	}
	d := time.Since(t)
	if d < 0 {
		return "in " + Duration(-d)
	}
	if d < time.Minute {
		return fmt.Sprintf("%ds ago", int(d.Seconds()))
	}
	return Duration(d) + " ago"
}
