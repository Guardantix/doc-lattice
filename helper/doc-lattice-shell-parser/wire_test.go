// Package main tests the shell-parser wire contract.
package main

import (
	"bytes"
	"fmt"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestDecodeRequestRejectsBadInputs(t *testing.T) {
	cases := map[string]string{
		"wrong version":     `{"protocol_version":2,"sources":[{"id":0,"source":"true"}]}`,
		"unknown field":     `{"protocol_version":1,"extra":1,"sources":[]}`,
		"duplicate field":   `{"protocol_version":1,"protocol_version":1,"sources":[]}`,
		"empty batch":       `{"protocol_version":1,"sources":[]}`,
		"non-contiguous id": `{"protocol_version":1,"sources":[{"id":0,"source":"a"},{"id":2,"source":"b"}]}`,
		"trailing document": `{"protocol_version":1,"sources":[{"id":0,"source":"a"}]} {}`,
		"bool as int id":    `{"protocol_version":1,"sources":[{"id":true,"source":"a"}]}`,
	}
	for name, body := range cases {
		if _, err := DecodeRequest([]byte(body)); err == nil {
			t.Errorf("%s: expected rejection, got nil", name)
		}
	}
}

func TestDecodeRequestAcceptsValid(t *testing.T) {
	req, err := DecodeRequest([]byte(`{"protocol_version":1,"sources":[{"id":0,"source":"true"},{"id":1,"source":"x"}]}`))
	if err != nil {
		t.Fatalf("valid request rejected: %v", err)
	}
	if req.ProtocolVersion != 1 || len(req.Sources) != 2 || req.Sources[1].ID != 1 {
		t.Fatalf("decoded request wrong: %+v", req)
	}
}

func TestDecodeRequestAcceptsNegativeZeroID(t *testing.T) {
	req, err := DecodeRequest([]byte(`{"protocol_version":1,"sources":[{"id":-0,"source":"true"}]}`))
	if err != nil {
		t.Fatalf("negative zero ID rejected: %v", err)
	}
	if got := req.Sources[0].ID; got != 0 {
		t.Fatalf("decoded ID = %d, want 0", got)
	}
}

func TestDecodeRequestRejectsInvalidUnicode(t *testing.T) {
	validPrefix := []byte(`{"protocol_version":1,"sources":[{"id":0,"source":"`)
	validSuffix := []byte(`"}]}`)
	invalidUTF8 := append(append(append([]byte{}, validPrefix...), 0xff), validSuffix...)
	rawSurrogate := append(append(append([]byte{}, validPrefix...), 0xed, 0xa0, 0x80), validSuffix...)

	cases := map[string][]byte{
		"invalid UTF-8":          invalidUTF8,
		"raw UTF-8 surrogate":    rawSurrogate,
		"escaped high surrogate": []byte(`{"protocol_version":1,"sources":[{"id":0,"source":"X\uD800X"}]}`),
		"escaped low surrogate":  []byte(`{"protocol_version":1,"sources":[{"id":0,"source":"X\uDC00X"}]}`),
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := DecodeRequest(body); err == nil {
				t.Fatal("expected rejection, got nil")
			}
		})
	}
}

func TestDecodeRequestAcceptsReplacementRuneAndSurrogatePair(t *testing.T) {
	cases := map[string]struct {
		body string
		want string
	}{
		"literal replacement rune": {
			body: `{"protocol_version":1,"sources":[{"id":0,"source":"�"}]}`,
			want: "�",
		},
		"escaped surrogate pair": {
			body: `{"protocol_version":1,"sources":[{"id":0,"source":"\uD83D\uDE00"}]}`,
			want: "😀",
		},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			req, err := DecodeRequest([]byte(tc.body))
			if err != nil {
				t.Fatalf("valid Unicode rejected: %v", err)
			}
			if got := req.Sources[0].Source; got != tc.want || !utf8.ValidString(got) {
				t.Fatalf("source = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestDecodeRequestRejectsNonFiniteNumbersAndNestedDuplicateKeys(t *testing.T) {
	cases := map[string]string{
		"NaN":                         `{"protocol_version":NaN,"sources":[{"id":0,"source":"a"}]}`,
		"Infinity":                    `{"protocol_version":Infinity,"sources":[{"id":0,"source":"a"}]}`,
		"negative Infinity":           `{"protocol_version":-Infinity,"sources":[{"id":0,"source":"a"}]}`,
		"duplicate nested id":         `{"protocol_version":1,"sources":[{"id":0,"source":"a","id":0}]}`,
		"duplicate nested source":     `{"protocol_version":1,"sources":[{"id":0,"source":"a","source":"b"}]}`,
		"unknown nested source field": `{"protocol_version":1,"sources":[{"id":0,"source":"a","extra":0}]}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := DecodeRequest([]byte(body)); err == nil {
				t.Fatal("expected rejection, got nil")
			}
		})
	}
}

func TestDecodeRequestRejectsWrongExactTypes(t *testing.T) {
	cases := map[string]string{
		"bool protocol version":     `{"protocol_version":true,"sources":[{"id":0,"source":"a"}]}`,
		"fraction protocol version": `{"protocol_version":1.0,"sources":[{"id":0,"source":"a"}]}`,
		"exponent protocol version": `{"protocol_version":1e0,"sources":[{"id":0,"source":"a"}]}`,
		"string protocol version":   `{"protocol_version":"1","sources":[{"id":0,"source":"a"}]}`,
		"null protocol version":     `{"protocol_version":null,"sources":[{"id":0,"source":"a"}]}`,
		"fraction id":               `{"protocol_version":1,"sources":[{"id":0.0,"source":"a"}]}`,
		"exponent id":               `{"protocol_version":1,"sources":[{"id":0e0,"source":"a"}]}`,
		"overflow id":               `{"protocol_version":1,"sources":[{"id":999999999999999999999999999999999,"source":"a"}]}`,
		"string id":                 `{"protocol_version":1,"sources":[{"id":"0","source":"a"}]}`,
		"null id":                   `{"protocol_version":1,"sources":[{"id":null,"source":"a"}]}`,
		"object sources":            `{"protocol_version":1,"sources":{}}`,
		"null sources":              `{"protocol_version":1,"sources":null}`,
		"non-object source":         `{"protocol_version":1,"sources":["a"]}`,
		"null source text":          `{"protocol_version":1,"sources":[{"id":0,"source":null}]}`,
		"numeric source text":       `{"protocol_version":1,"sources":[{"id":0,"source":1}]}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := DecodeRequest([]byte(body)); err == nil {
				t.Fatal("expected rejection, got nil")
			}
		})
	}
}

func TestDecodeRequestRequiresEveryField(t *testing.T) {
	cases := []string{
		`{}`,
		`{"protocol_version":1}`,
		`{"sources":[{"id":0,"source":"a"}]}`,
		`{"protocol_version":1,"sources":[{"source":"a"}]}`,
		`{"protocol_version":1,"sources":[{"id":0}]}`,
	}
	for _, body := range cases {
		if _, err := DecodeRequest([]byte(body)); err == nil {
			t.Errorf("expected rejection for %s", body)
		}
	}
}

func TestDecodeRequestSourceByteBoundary(t *testing.T) {
	const sourceCap = 4_194_304
	for name, source := range map[string]string{
		"ASCII":           strings.Repeat("a", sourceCap),
		"four-byte UTF-8": strings.Repeat("😀", sourceCap/4),
	} {
		t.Run(name, func(t *testing.T) {
			body := []byte(`{"protocol_version":1,"sources":[{"id":0,"source":"` + source + `"}]}`)
			req, err := DecodeRequest(body)
			if err != nil {
				t.Fatalf("at-limit source rejected: %v", err)
			}
			if got := len([]byte(req.Sources[0].Source)); got != sourceCap {
				t.Fatalf("decoded source has %d bytes, want %d", got, sourceCap)
			}
		})
	}

	overLimit := []byte(`{"protocol_version":1,"sources":[{"id":0,"source":"` + strings.Repeat("a", sourceCap+1) + `"}]}`)
	if _, err := DecodeRequest(overLimit); err == nil {
		t.Fatal("over-limit source accepted")
	}
}

func TestDecodeRequestAggregateByteBoundary(t *testing.T) {
	const requestCap = 8_388_608
	body := []byte(`{"protocol_version":1,"sources":[{"id":0,"source":"a"}]}`)
	atLimit := append(append([]byte{}, body...), bytes.Repeat([]byte(" "), requestCap-len(body))...)
	if _, err := DecodeRequest(atLimit); err != nil {
		t.Fatalf("at-limit request rejected: %v", err)
	}
	overLimit := append(atLimit, ' ')
	if _, err := DecodeRequest(overLimit); err == nil {
		t.Fatal("over-limit request accepted")
	}
}

func TestDecodeRequestSourceCountBoundary(t *testing.T) {
	const sourceCountCap = 4_096
	atLimit := batchRequest(sourceCountCap)
	req, err := DecodeRequest(atLimit)
	if err != nil {
		t.Fatalf("at-limit source count rejected: %v", err)
	}
	if got := len(req.Sources); got != sourceCountCap {
		t.Fatalf("decoded %d sources, want %d", got, sourceCountCap)
	}
	if _, err := DecodeRequest(batchRequest(sourceCountCap + 1)); err == nil {
		t.Fatal("over-limit source count accepted")
	}
}

func TestDecodeRequestJSONDepthBoundary(t *testing.T) {
	const depthCap = 64
	atLimit := []byte(strings.Repeat("[", depthCap) + "0" + strings.Repeat("]", depthCap))
	if _, err := DecodeRequest(atLimit); err == nil || err.Error() == "request exceeds maximum JSON depth" {
		t.Fatalf("depth-%d document should reach shape validation, got %v", depthCap, err)
	}
	overLimit := []byte(strings.Repeat("[", depthCap+1) + "0" + strings.Repeat("]", depthCap+1))
	if _, err := DecodeRequest(overLimit); err == nil || err.Error() != "request exceeds maximum JSON depth" {
		t.Fatalf("depth-%d document got %v", depthCap+1, err)
	}
}

func TestDecodeRequestAcceptsOnlyTrailingWhitespace(t *testing.T) {
	body := []byte(" \n\t" + `{"protocol_version":1,"sources":[{"id":0,"source":"a"}]}` + "\r\n ")
	if _, err := DecodeRequest(body); err != nil {
		t.Fatalf("request with surrounding whitespace rejected: %v", err)
	}
	if _, err := DecodeRequest(append(body, 'x')); err == nil {
		t.Fatal("trailing non-whitespace accepted")
	}
}

func TestDecodeRequestErrorsAreStableAndDoNotLeakInput(t *testing.T) {
	body := []byte(`{"protocol_version":1,"SECRET_SENTINEL":1,"sources":[{"id":0,"source":"a"}]}`)
	_, first := DecodeRequest(body)
	_, second := DecodeRequest(body)
	if first == nil || second == nil {
		t.Fatal("expected rejection, got nil")
	}
	if first.Error() != second.Error() {
		t.Fatalf("unstable errors: %q and %q", first, second)
	}
	if strings.Contains(first.Error(), "SECRET_SENTINEL") {
		t.Fatalf("error leaked offending input: %q", first)
	}
}

func TestCertifyStubAndEncodeResponseUseSchemaArrays(t *testing.T) {
	req := &Request{ProtocolVersion: 1, Sources: []Source{{ID: 0, Source: "true"}}}
	resp, err := Certify(req)
	if err != nil {
		t.Fatalf("Certify failed: %v", err)
	}
	if resp.ProtocolVersion != 1 || len(resp.Results) != 1 || resp.Results[0].ID != 0 || resp.Results[0].WorkUnits != 1 {
		t.Fatalf("stub response wrong: %+v", resp)
	}
	payload, err := EncodeResponse(resp)
	if err != nil {
		t.Fatalf("EncodeResponse failed: %v", err)
	}
	if bytes.Contains(payload, []byte(":null")) || bytes.Contains(payload, []byte("\n")) || bytes.HasPrefix(payload, []byte{0xef, 0xbb, 0xbf}) {
		t.Fatalf("response is not compact schema-safe JSON: %q", payload)
	}
	if !bytes.Contains(payload, []byte(`"events":[]`)) || !bytes.Contains(payload, []byte(`"results":[`)) {
		t.Fatalf("required arrays missing: %s", payload)
	}
}

func TestEncodeResponseUsesExactEventUnionShapes(t *testing.T) {
	resp := &Response{
		ProtocolVersion: 1,
		HelperVersion:   "task1-placeholder",
		ParserVersion:   "mvdan.cc/sh/v3@v3.13.1",
		Results: []Result{{
			ID: 0,
			Events: []Event{
				{Kind: "command_site", Ordinal: 0, StartByte: 0, EndByte: 1},
				{Kind: "refusal", Code: "syntax-error", StartByte: 2, EndByte: 3},
			},
			WorkUnits: 1,
		}},
	}
	payload, err := EncodeResponse(resp)
	if err != nil {
		t.Fatalf("EncodeResponse failed: %v", err)
	}
	want := `{"protocol_version":1,"helper_version":"task1-placeholder","parser_version":"mvdan.cc/sh/v3@v3.13.1","results":[{"id":0,"events":[{"kind":"command_site","ordinal":0,"start_byte":0,"end_byte":1,"assignments":[],"argv":[]},{"kind":"refusal","code":"syntax-error","start_byte":2,"end_byte":3}],"work_units":1}]}`
	if string(payload) != want {
		t.Fatalf("encoded response\n got: %s\nwant: %s", payload, want)
	}
}

func TestRunEmptyInputIsStableAndSilent(t *testing.T) {
	for attempt := 0; attempt < 2; attempt++ {
		var stdout bytes.Buffer
		var stderr bytes.Buffer
		if code := run(strings.NewReader(""), &stdout, &stderr); code != 2 {
			t.Fatalf("attempt %d: exit code = %d, want 2", attempt, code)
		}
		if stdout.Len() != 0 || stderr.Len() != 0 {
			t.Fatalf("attempt %d: stdout=%q stderr=%q", attempt, stdout.Bytes(), stderr.Bytes())
		}
	}
}

func batchRequest(count int) []byte {
	var body strings.Builder
	body.WriteString(`{"protocol_version":1,"sources":[`)
	for id := 0; id < count; id++ {
		if id > 0 {
			body.WriteByte(',')
		}
		fmt.Fprintf(&body, `{"id":%d,"source":"a"}`, id)
	}
	body.WriteString(`]}`)
	return []byte(body.String())
}
