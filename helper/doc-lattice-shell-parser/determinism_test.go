// Package main verifies deterministic certification and response ownership.
package main

import (
	"bytes"
	"encoding/json"
	"path/filepath"
	"testing"
)

func TestDeterminismForRepresentativeBatch(t *testing.T) {
	request := representativeConformanceRequest(t)
	requestBefore, err := json.Marshal(request)
	if err != nil {
		t.Fatalf("marshal request snapshot: %v", err)
	}

	first, err := Certify(request)
	if err != nil {
		t.Fatalf("first Certify: %v", err)
	}
	firstBytes, err := EncodeResponse(first)
	if err != nil {
		t.Fatalf("encode first response: %v", err)
	}
	second, err := Certify(request)
	if err != nil {
		t.Fatalf("second Certify: %v", err)
	}
	secondBytes, err := EncodeResponse(second)
	if err != nil {
		t.Fatalf("encode second response: %v", err)
	}
	if !bytes.Equal(firstBytes, secondBytes) {
		t.Fatalf("repeated certification differs:\nfirst:  %s\nsecond: %s", firstBytes, secondBytes)
	}

	requestAfter, err := json.Marshal(request)
	if err != nil {
		t.Fatalf("marshal request after certification: %v", err)
	}
	if !bytes.Equal(requestBefore, requestAfter) {
		t.Fatalf("Certify mutated its request:\nbefore: %s\nafter:  %s", requestBefore, requestAfter)
	}

	assertIndependentResponses(t, first, second)
}

func representativeConformanceRequest(t *testing.T) *Request {
	t.Helper()
	names := []string{
		"empty-events.json",
		"multi-prefix.json",
		"dynamic-word.json",
		"single-refusal.json",
	}
	request := &Request{ProtocolVersion: 1, Sources: make([]Source, 0, len(names))}
	for _, name := range names {
		path := filepath.Join(checkpointProtocol, "conformance", name)
		fixture := readConformanceFixture(t, path)
		decoded, err := DecodeRequest(fixture.Request)
		if err != nil {
			t.Fatalf("representative fixture request %q rejected: %v", path, err)
		}
		if len(decoded.Sources) != 1 {
			t.Fatalf("representative fixture %q has %d sources, want 1", path, len(decoded.Sources))
		}
		source := decoded.Sources[0]
		source.ID = len(request.Sources)
		request.Sources = append(request.Sources, source)
	}
	return request
}

func assertIndependentResponses(t *testing.T, first, second *Response) {
	t.Helper()
	if len(first.Results) < 2 || len(second.Results) < 2 ||
		len(first.Results[1].Events) == 0 || len(second.Results[1].Events) == 0 ||
		len(first.Results[1].Events[0].Assignments) == 0 || len(second.Results[1].Events[0].Assignments) == 0 ||
		len(first.Results[1].Events[0].Argv) == 0 || len(second.Results[1].Events[0].Argv) == 0 ||
		first.Results[1].Events[0].Argv[0].Text == nil || second.Results[1].Events[0].Argv[0].Text == nil {
		t.Fatal("representative fixtures no longer contain nested command facts for alias testing")
	}

	secondID := second.Results[1].ID
	secondKind := second.Results[1].Events[0].Kind
	secondAssignmentName := second.Results[1].Events[0].Assignments[0].Name
	secondText := *second.Results[1].Events[0].Argv[0].Text
	first.Results[1].ID = -1
	first.Results[1].Events[0].Kind = "mutated"
	first.Results[1].Events[0].Assignments[0].Name = "MUTATED"
	*first.Results[1].Events[0].Argv[0].Text = "mutated"
	if second.Results[1].ID != secondID || second.Results[1].Events[0].Kind != secondKind ||
		second.Results[1].Events[0].Assignments[0].Name != secondAssignmentName ||
		*second.Results[1].Events[0].Argv[0].Text != secondText {
		t.Fatal("independent Certify responses alias mutable result, event, assignment, or word storage")
	}
}
