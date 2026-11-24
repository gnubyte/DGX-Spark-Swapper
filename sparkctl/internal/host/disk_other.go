//go:build !unix

package host

import "errors"

// ReadDisk is only available on Linux.
func ReadDisk(path string) (Disk, error) { return Disk{Path: path}, errors.New("not supported here") }
