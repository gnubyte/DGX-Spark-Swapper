//go:build !unix

package run

import "os/exec"

func setProcessGroup(c *exec.Cmd) {}
