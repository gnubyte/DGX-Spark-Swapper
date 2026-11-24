VERSION := $(shell tr -d ' \n' < VERSION)

.PHONY: build test deb clean run

build:
	CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$(VERSION)" -o dist/sparkctl .

test:
	gofmt -l . | (! grep .) && go vet ./... && go test -race ./...

deb:
	bash packaging/build-deb.sh arm64 amd64

run:
	go run .

clean:
	rm -rf dist
