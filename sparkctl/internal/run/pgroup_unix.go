//go:build unix

package run

import (
	"os/exec"
	"syscall"
)

// A command runs in its own process group, so a cancelled call stops what it started too (docker logs -f, journalctl -f).
func setProcessGroup(c *exec.Cmd) {
	c.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	c.Cancel = func() error {
		if c.Process == nil {
			return nil
		}
		return syscall.Kill(-c.Process.Pid, syscall.SIGTERM)
	}
}
