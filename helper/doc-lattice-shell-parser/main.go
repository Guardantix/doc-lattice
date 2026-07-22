// Command shell-parser certifies Bash sources for the doc-lattice GitHub CI audit.
package main

//go:generate /usr/local/go/bin/go run gen_limits.go
//go:generate /usr/local/go/bin/go run gen_tables.go

import (
	"errors"
	"io"
	"os"
)

const (
	temporaryHelperVersion = "task1-placeholder"
	temporaryParserVersion = "mvdan.cc/sh/v3@v3.13.1"
)

func main() { os.Exit(run(os.Stdin, os.Stdout, os.Stderr)) }

// run reads one request from in, writes one response to out, and returns the process exit
// code. A malformed request or any internal failure yields exit code 2 and no stdout bytes.
func run(in io.Reader, out, errOut io.Writer) int {
	_ = errOut
	data, err := io.ReadAll(io.LimitReader(in, int64(aggregateRequestCapBytes)+1))
	if err != nil {
		return 2
	}
	req, err := DecodeRequest(data)
	if err != nil {
		return 2
	}
	resp, err := Certify(req)
	if err != nil {
		return 2
	}
	payload, err := EncodeResponse(resp)
	if err != nil {
		return 2
	}
	if _, err := out.Write(payload); err != nil {
		return 2
	}
	return 0
}

// Certify returns the temporary Task 1 response for a validated request.
func Certify(request *Request) (*Response, error) {
	if request == nil {
		return nil, errors.New("request is nil")
	}
	results := make([]Result, len(request.Sources))
	for index, source := range request.Sources {
		results[index] = Result{
			ID:        source.ID,
			Events:    []Event{},
			WorkUnits: 1,
		}
	}
	return &Response{
		ProtocolVersion: 1,
		HelperVersion:   temporaryHelperVersion,
		ParserVersion:   temporaryParserVersion,
		Results:         results,
	}, nil
}
