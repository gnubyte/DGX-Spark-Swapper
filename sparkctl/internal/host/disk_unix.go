//go:build unix

package host

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// ReadDisk reports the filesystem path lives on (the nearest existing parent of path).
func ReadDisk(path string) (Disk, error) {
	p := path
	for {
		if _, err := os.Stat(p); err == nil {
			break
		}
		parent := filepath.Dir(p)
		if parent == p {
			break
		}
		p = parent
	}
	var st syscall.Statfs_t
	if err := syscall.Statfs(p, &st); err != nil {
		return Disk{Path: path}, err
	}
	d := Disk{Path: path, Total: st.Blocks * uint64(st.Bsize), Free: st.Bavail * uint64(st.Bsize)}
	d.Mount, d.Device = mountOf(p)
	return d, nil
}

// mountOf finds the mount point (and device) holding p, from /proc/self/mounts.
func mountOf(p string) (string, string) {
	f, err := os.Open("/proc/self/mounts")
	if err != nil {
		return "", ""
	}
	defer f.Close()
	best, dev := "", ""
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fl := strings.Fields(sc.Text())
		if len(fl) < 2 {
			continue
		}
		mnt := strings.ReplaceAll(fl[1], `\040`, " ")
		if (p == mnt || strings.HasPrefix(p, strings.TrimRight(mnt, "/")+"/") || mnt == "/") && len(mnt) >= len(best) {
			best, dev = mnt, fl[0]
		}
	}
	return best, dev
}
