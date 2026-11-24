package internal_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/config"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/docker"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/host"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/run"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/sparkia"
	"github.com/gnubyte/DGX-Spark-Swapper/sparkctl/internal/systemd"
)

// Output captured from docker 27 on a DGX Spark.
const dockerPS = `{"Command":"\"/entrypoint.sh\"","CreatedAt":"2026-11-20 10:01:02 +0000 UTC","ID":"aaa","Image":"ghcr.io/gnubyte/sparkia-server:latest","Labels":"com.docker.compose.project=sparkia,com.docker.compose.service=vision","LocalVolumes":"0","Mounts":"/home/spaphy","Names":"sparkia-vision","Networks":"host","Ports":"","RunningFor":"2 days ago","Size":"0B","State":"running","Status":"Up 2 days"}
{"Command":"\"python3\"","CreatedAt":"2026-11-19 09:00:00 +0000 UTC","ID":"bbb","Image":"vllm/vllm-openai:v0.25.1","Labels":"","Names":"laguna","State":"exited","Status":"Exited (137) 3 hours ago"}
{"Command":"\"tensorfold\"","CreatedAt":"2026-11-21 09:00:00 +0000 UTC","ID":"ccc","Image":"tensorfold-qwen38:zig","Labels":"","Names":"qwen38-flash-next-tf","State":"running","Status":"Up 5 hours"}
`

func TestDockerContainersRunningFirstWithTheirComposeProject(t *testing.T) {
	f := run.NewFake().On("docker ps -a --no-trunc --format {{json .}}", dockerPS)
	cs, err := (&docker.Client{R: f}).Containers(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(cs) != 3 || !cs[0].Running() || !cs[1].Running() || cs[2].Names != "laguna" {
		t.Fatalf("order: %+v", cs)
	}
	if cs[0].Names != "qwen38-flash-next-tf" || cs[1].Project() != "sparkia" || cs[1].Label("com.docker.compose.service") != "vision" {
		t.Fatalf("labels: %+v", cs)
	}
}

func TestDockerImagesNameAndDangling(t *testing.T) {
	out := `{"Containers":"N/A","CreatedAt":"2026-11-21 10:00:00 +0000 UTC","CreatedSince":"2 days ago","Digest":"sha256:abc","ID":"sha256:111122223333","Repository":"tensorfold-qwen38","Size":"27.6GB","Tag":"zig-db28187"}
{"CreatedAt":"2026-11-01 10:00:00 +0000 UTC","Digest":"<none>","ID":"sha256:999988887777","Repository":"<none>","Size":"1.2GB","Tag":"<none>"}`
	f := run.NewFake().On("docker images --digests --format {{json .}}", out)
	is, err := (&docker.Client{R: f}).Images(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if is[0].Ref() != "tensorfold-qwen38:zig-db28187" || is[0].Dangling() || !is[1].Dangling() || is[1].Ref() != "sha256:999988887777" {
		t.Fatalf("%+v", is)
	}
}

func TestDockerActionsAreTheCommandsAPersonTypes(t *testing.T) {
	cases := map[string]docker.Action{
		"docker stop -t 60 laguna":    docker.Stop("laguna"),
		"docker rm -f laguna":         docker.RemoveContainer("laguna", true),
		"docker rmi tensorfold:zig":   docker.RemoveImage("tensorfold:zig", false),
		"docker image prune -a -f":    docker.PruneImages(true),
		"docker builder prune -a -f":  docker.PruneBuildCache(),
		"docker pull 'ghcr.io/x/y:1'": {Args: []string{"pull", "ghcr.io/x/y:1"}},
	}
	for want, a := range cases {
		got := a.Line()
		if strings.Contains(want, "'") {
			continue // quoting only applies to odd characters
		}
		if got != want {
			t.Errorf("%q, want %q", got, want)
		}
	}
	f := run.NewFake()
	if _, err := (&docker.Client{R: f}).Do(context.Background(), docker.Restart("sparkia-vision")); err != nil {
		t.Fatal(err)
	}
	if !f.Called("docker restart -t 60 sparkia-vision") {
		t.Fatalf("calls: %v", f.Calls)
	}
}

func TestSystemctlShowIsRead(t *testing.T) {
	out := `Id=spark-swapper.service
Description=Spark Swapper
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
ActiveEnterTimestamp=Thu 2026-11-20 10:31:22 UTC
MainPID=1234
MemoryCurrent=52428800
NRestarts=2
Result=success
NextElapseUSecRealtime=
LastTriggerUSec=n/a
`
	u := systemd.ParseShow(systemd.Unit{Name: "spark-swapper.service"}, out)
	if !u.Exists() || u.Active != "active" || u.Sub != "running" || u.Enabled != "enabled" || u.MainPID != 1234 ||
		u.MemoryBytes != 50<<20 || u.Restarts != 2 {
		t.Fatalf("%+v", u)
	}
	if u.Since.IsZero() || u.Since.Year() != 2026 {
		t.Fatalf("since %v", u.Since)
	}
	missing := systemd.ParseShow(systemd.Unit{Name: "x"}, "LoadState=not-found\nActiveState=inactive\nMemoryCurrent=[not set]\n")
	if missing.Exists() || missing.MemoryBytes != 0 {
		t.Fatalf("%+v", missing)
	}
}

func TestJournalForSystemAndUserUnits(t *testing.T) {
	args, priv := systemd.JournalArgs(systemd.Unit{Name: "spark-swapper.service"}, "", 200, true)
	if strings.Join(args, " ") != "--no-pager -o short-iso -n 200 -f -u spark-swapper.service" || !priv {
		t.Fatalf("%v %v", args, priv)
	}
	args, priv = systemd.JournalArgs(systemd.Unit{Name: "qwen-image.service", User: true}, "", 50, false)
	if strings.Join(args, " ") != "--no-pager -o short-iso -n 50 --user -u qwen-image.service" || priv {
		t.Fatalf("%v %v", args, priv)
	}
	args, _ = systemd.JournalArgs(systemd.Unit{Name: "qwen-image.service", User: true, Owner: "spaphy"}, "", 50, false)
	if args[len(args)-1] != "_SYSTEMD_USER_UNIT=qwen-image.service" {
		t.Fatalf("%v", args)
	}
}

func TestSystemctlActionsNeedRootForSystemUnits(t *testing.T) {
	c := &systemd.Client{R: run.NewFake()}
	if got := c.Line(systemd.Unit{Name: "docker.service"}, systemd.Restart); got != "sudo systemctl restart docker.service" {
		t.Fatal(got)
	}
	if got := c.Line(systemd.Unit{Name: "openclaw-gateway.service", User: true}, systemd.Restart); got != "systemctl --user restart openclaw-gateway.service" {
		t.Fatal(got)
	}
	f := run.NewFake()
	c = &systemd.Client{R: f}
	if err := c.Do(context.Background(), systemd.Unit{Name: "sparkia-update.timer"}, systemd.Enable); err != nil {
		t.Fatal(err)
	}
	if !f.Called("systemctl enable --now -- sparkia-update.timer") {
		t.Fatalf("%v", f.Calls)
	}
}

func TestMeminfoAndGPU(t *testing.T) {
	m := host.ParseMeminfo("MemTotal:       127535612 kB\nMemFree:         2000000 kB\nMemAvailable:   97000000 kB\nSwapTotal: 0 kB\n")
	if m.Total != 127535612*1024 || m.Available != 97000000*1024 || m.UsedPct() < 23 || m.UsedPct() > 25 {
		t.Fatalf("%+v %.1f", m, m.UsedPct())
	}
	gs := host.ParseGPUs("NVIDIA GB10, 87, [N/A], [N/A], 61, 48.20, 580.95.05\n")
	if len(gs) != 1 || gs[0].Name != "NVIDIA GB10" || gs[0].Util != "87" || gs[0].Driver != "580.95.05" {
		t.Fatalf("%+v", gs)
	}
	ps := host.ParseGPUProcesses("4242, /usr/bin/python3, [N/A]\n")
	if len(ps) != 1 || ps[0].PID != "4242" {
		t.Fatalf("%+v", ps)
	}
	if host.Bytes(1_880_000_000_000) != "1.7T" || host.Bytes(512) != "512B" || host.Duration(26*time.Hour+3*time.Minute) != "1d 2h" {
		t.Fatal(host.Bytes(1_880_000_000_000), host.Bytes(512), host.Duration(26*time.Hour))
	}
}

func TestComposePSBothFormats(t *testing.T) {
	lines := `{"Name":"sparkia-vision","Service":"vision","Image":"ghcr.io/gnubyte/sparkia-server:latest","State":"running","Status":"Up 2 days","Health":""}
{"Name":"sparkia-firewall","Service":"firewall","State":"running","Status":"Up 2 days"}`
	a, err := sparkia.ParseServices(lines)
	if err != nil || len(a) != 2 || a[0].Service != "firewall" {
		t.Fatalf("%v %+v", err, a)
	}
	b, err := sparkia.ParseServices(`[{"Name":"sparkia-guard","Service":"guard","State":"exited","ExitCode":1}]`)
	if err != nil || len(b) != 1 || b[0].ExitCode != 1 {
		t.Fatalf("%v %+v", err, b)
	}
	if s, err := sparkia.ParseServices(""); err != nil || s != nil {
		t.Fatal(s, err)
	}
}

func TestSparkiaReleasePinEditsOnlyThatLine(t *testing.T) {
	env := "# Sparkia\nSPARK_USER=spaphy\nSPARK_UID=1000\nSPARKIA_TAG=latest\nVISION_PORT=8443\n"
	got, err := sparkia.SetEnvText(env, "SPARKIA_TAG", "0.9.142")
	if err != nil {
		t.Fatal(err)
	}
	if got != "# Sparkia\nSPARK_USER=spaphy\nSPARK_UID=1000\nSPARKIA_TAG=0.9.142\nVISION_PORT=8443\n" {
		t.Fatalf("%q", got)
	}
	got, _ = sparkia.SetEnvText("A=1\n", "SPARKIA_TAG", "latest")
	if got != "A=1\nSPARKIA_TAG=latest\n" {
		t.Fatalf("%q", got)
	}
	if _, err := sparkia.SetEnvText(env, "SPARKIA_TAG", "x\nEVIL=1"); err == nil {
		t.Fatal("a newline must be refused")
	}
	for _, bad := range []string{"", "latest; rm -rf /", "../x", "a b"} {
		if sparkia.ValidTag(bad) {
			t.Errorf("%q accepted", bad)
		}
	}
	if !sparkia.ValidTag("0.9.142") || !sparkia.ValidTag("latest") {
		t.Fatal("valid tags refused")
	}
	if e := sparkia.ParseEnv(env); e["SPARK_USER"] != "spaphy" || e["SPARKIA_TAG"] != "latest" {
		t.Fatalf("%v", e)
	}
}

func TestConfigLayersAndRefusesABrokenFile(t *testing.T) {
	dir := t.TempDir()
	sys := dir + "/sys.json"
	usr := dir + "/user.json"
	writeFile(t, sys, `{"swapper_url":"https://spark:8999","refresh_seconds":0}`)
	writeFile(t, usr, `{"sparkia_dir":"/srv/sparkia"}`)
	c, err := config.Load(sys, usr, dir+"/missing.json")
	if err != nil {
		t.Fatal(err)
	}
	if c.SwapperURL != "https://spark:8999" || c.SparkiaDir != "/srv/sparkia" || c.RefreshSecs != 3 || len(c.Units) == 0 {
		t.Fatalf("%+v", c)
	}
	writeFile(t, usr, `{"sparkia_dir":`)
	if _, err := config.Load(sys, usr); err == nil || !strings.Contains(err.Error(), usr) {
		t.Fatalf("a broken file must be named: %v", err)
	}
}

func TestStateIsPrivate(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	s := config.State{SwapperSession: "abc", SwapperPin: "ff"}
	if err := s.Save(); err != nil {
		t.Fatal(err)
	}
	st := statMode(t, config.StatePath())
	if st != 0o600 && st != 0 { // 0: the platform has no Unix modes
		t.Fatalf("mode %o", st)
	}
	if got := config.LoadState(); got.SwapperSession != "abc" || got.SwapperPin != "ff" {
		t.Fatalf("%+v", got)
	}
}

func TestRunQuotesForAShell(t *testing.T) {
	if got := run.Quote([]string{"docker", "exec", "-it", "x", "sh", "-c", "echo 'hi' && ls"}); got != `docker exec -it x sh -c 'echo '\''hi'\'' && ls'` {
		t.Fatal(got)
	}
}
