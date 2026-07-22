// Package main implements the shell-parser helper wire protocol.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"unicode/utf8"
)

const (
	// TODO(task2): source from limits_gen.
	maxSourcesPerBatch       = 4_096
	helperSourceCapBytes     = 4_194_304
	aggregateRequestCapBytes = 8_388_608
	jsonMaxDepth             = 64
)

// Request is one batch of shell sources to certify.
type Request struct {
	// ProtocolVersion identifies the frozen wire protocol.
	ProtocolVersion int `json:"protocol_version"`
	// Sources contains the ordered source batch.
	Sources []Source `json:"sources"`
}

// Source is one opaque, ordered shell source.
type Source struct {
	// ID is the source's contiguous batch identifier.
	ID int `json:"id"`
	// Source is the Bash source text.
	Source string `json:"source"`
}

// Response is one complete certification batch response.
type Response struct {
	// ProtocolVersion identifies the frozen wire protocol.
	ProtocolVersion int `json:"protocol_version"`
	// HelperVersion identifies the helper implementation.
	HelperVersion string `json:"helper_version"`
	// ParserVersion identifies the pinned parser module.
	ParserVersion string `json:"parser_version"`
	// Results contains one ordered result per request source.
	Results []Result `json:"results"`
}

// Result contains the syntax facts for one source.
type Result struct {
	// ID matches the source's batch identifier.
	ID int `json:"id"`
	// Events contains the ordered syntax events.
	Events []Event `json:"events"`
	// WorkUnits records deterministic helper work.
	WorkUnits int `json:"work_units"`
}

// Event is one command-site or refusal event from the frozen tagged union.
type Event struct {
	// Kind selects the command_site or refusal union member.
	Kind string `json:"kind"`
	// Ordinal orders a command site within its source.
	Ordinal int `json:"ordinal"`
	// Code identifies a refusal reason.
	Code string `json:"code"`
	// StartByte is the inclusive UTF-8 byte offset.
	StartByte int `json:"start_byte"`
	// EndByte is the exclusive UTF-8 byte offset.
	EndByte int `json:"end_byte"`
	// Assignments contains command-site assignment facts.
	Assignments []Assignment `json:"assignments"`
	// Argv contains command-site word facts.
	Argv []Word `json:"argv"`
}

// Assignment is one assignment prefix on a command site.
type Assignment struct {
	// Name is the assignment name.
	Name string `json:"name"`
	// ValueKnown reports whether the value is static.
	ValueKnown bool `json:"value_known"`
	// StartByte is the inclusive UTF-8 byte offset.
	StartByte int `json:"start_byte"`
	// EndByte is the exclusive UTF-8 byte offset.
	EndByte int `json:"end_byte"`
}

// Word is one command-site argument word.
type Word struct {
	// Text is the static text, or nil when dynamic.
	Text *string `json:"text"`
	// Single reports whether the word has one shell part.
	Single bool `json:"single"`
	// StartByte is the inclusive UTF-8 byte offset.
	StartByte int `json:"start_byte"`
	// EndByte is the exclusive UTF-8 byte offset.
	EndByte int `json:"end_byte"`
}

// MarshalJSON emits exactly one event member of the frozen tagged union.
func (event Event) MarshalJSON() ([]byte, error) {
	switch event.Kind {
	case "command_site":
		assignments := event.Assignments
		if assignments == nil {
			assignments = []Assignment{}
		}
		argv := event.Argv
		if argv == nil {
			argv = []Word{}
		}
		return json.Marshal(struct {
			Kind        string       `json:"kind"`
			Ordinal     int          `json:"ordinal"`
			StartByte   int          `json:"start_byte"`
			EndByte     int          `json:"end_byte"`
			Assignments []Assignment `json:"assignments"`
			Argv        []Word       `json:"argv"`
		}{
			Kind:        event.Kind,
			Ordinal:     event.Ordinal,
			StartByte:   event.StartByte,
			EndByte:     event.EndByte,
			Assignments: assignments,
			Argv:        argv,
		})
	case "refusal":
		return json.Marshal(struct {
			Kind      string `json:"kind"`
			Code      string `json:"code"`
			StartByte int    `json:"start_byte"`
			EndByte   int    `json:"end_byte"`
		}{
			Kind:      event.Kind,
			Code:      event.Code,
			StartByte: event.StartByte,
			EndByte:   event.EndByte,
		})
	default:
		return nil, errors.New("event has invalid kind")
	}
}

type rawRequest struct {
	ProtocolVersion json.RawMessage `json:"protocol_version"`
	Sources         json.RawMessage `json:"sources"`
}

type rawSource struct {
	ID     json.RawMessage `json:"id"`
	Source json.RawMessage `json:"source"`
}

type jsonFrame struct {
	kind         json.Delim
	keys         map[string]struct{}
	expectingKey bool
	currentKey   string
	sourcesArray bool
	elementCount int
}

// DecodeRequest strictly decodes one frozen-protocol request document.
func DecodeRequest(data []byte) (*Request, error) {
	if len(data) > aggregateRequestCapBytes {
		return nil, errors.New("request exceeds aggregate byte limit")
	}
	if !utf8.Valid(data) {
		return nil, errors.New("request is not valid UTF-8")
	}
	if hasLoneSurrogateEscape(data) {
		return nil, errors.New("request contains a lone Unicode surrogate")
	}
	if err := inspectJSONStructure(data); err != nil {
		return nil, err
	}

	var raw rawRequest
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&raw); err != nil {
		return nil, errors.New("request does not match the protocol schema")
	}
	if err := requireDecoderEOF(decoder); err != nil {
		return nil, err
	}
	if string(raw.ProtocolVersion) != "1" {
		return nil, errors.New("protocol_version must be integer 1")
	}
	if len(raw.Sources) == 0 || raw.Sources[0] != '[' {
		return nil, errors.New("sources must be an array")
	}

	var rawSources []json.RawMessage
	if err := json.Unmarshal(raw.Sources, &rawSources); err != nil {
		return nil, errors.New("sources must be an array")
	}
	if len(rawSources) == 0 {
		return nil, errors.New("sources must not be empty")
	}
	if len(rawSources) > maxSourcesPerBatch {
		return nil, errors.New("sources exceeds maximum element count")
	}

	request := &Request{
		ProtocolVersion: 1,
		Sources:         make([]Source, len(rawSources)),
	}
	for index, rawValue := range rawSources {
		source, err := decodeSource(rawValue, index)
		if err != nil {
			return nil, err
		}
		request.Sources[index] = source
	}
	return request, nil
}

func decodeSource(data []byte, expectedID int) (Source, error) {
	var raw rawSource
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&raw); err != nil {
		return Source{}, errors.New("source does not match the protocol schema")
	}
	if err := requireDecoderEOF(decoder); err != nil {
		return Source{}, errors.New("source does not match the protocol schema")
	}
	id, ok := parseExactJSONInteger(raw.ID)
	if !ok || id != int64(expectedID) {
		return Source{}, errors.New("source ids must be contiguous integers starting at 0")
	}
	if len(raw.Source) == 0 || raw.Source[0] != '"' {
		return Source{}, errors.New("source text must be a string")
	}
	var sourceText string
	if err := json.Unmarshal(raw.Source, &sourceText); err != nil {
		return Source{}, errors.New("source text must be a string")
	}
	if len(sourceText) > helperSourceCapBytes {
		return Source{}, errors.New("source text exceeds byte limit")
	}
	return Source{ID: expectedID, Source: sourceText}, nil
}

func parseExactJSONInteger(data []byte) (int64, bool) {
	if len(data) == 0 {
		return 0, false
	}
	digitStart := 0
	if data[0] == '-' {
		digitStart = 1
	}
	if digitStart == len(data) {
		return 0, false
	}
	for _, digit := range data[digitStart:] {
		if digit < '0' || digit > '9' {
			return 0, false
		}
	}
	value, err := strconv.ParseInt(string(data), 10, 64)
	return value, err == nil
}

func requireDecoderEOF(decoder *json.Decoder) error {
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return errors.New("request must contain exactly one JSON document")
	}
	return nil
}

func inspectJSONStructure(data []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	frames := make([]jsonFrame, 0, jsonMaxDepth)
	rootStarted := false
	rootComplete := false

	for {
		token, err := decoder.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return errors.New("request is not valid JSON")
		}
		if rootComplete {
			return errors.New("request must contain exactly one JSON document")
		}

		if delimiter, ok := token.(json.Delim); ok {
			switch delimiter {
			case '{', '[':
				isSources, err := beginContainerValue(frames)
				if err != nil {
					return err
				}
				if len(frames) == 0 {
					if rootStarted {
						return errors.New("request must contain exactly one JSON document")
					}
					rootStarted = true
				}
				if len(frames)+1 > jsonMaxDepth {
					return errors.New("request exceeds maximum JSON depth")
				}
				frame := jsonFrame{kind: delimiter, sourcesArray: isSources}
				if delimiter == '{' {
					frame.keys = make(map[string]struct{})
					frame.expectingKey = true
				}
				frames = append(frames, frame)
			case '}', ']':
				if len(frames) == 0 || frames[len(frames)-1].kind+2 != delimiter {
					return errors.New("request is not valid JSON")
				}
				frames = frames[:len(frames)-1]
				if len(frames) == 0 {
					rootComplete = true
				} else {
					finishObjectValue(&frames[len(frames)-1])
				}
			}
			continue
		}

		if len(frames) == 0 {
			if rootStarted {
				return errors.New("request must contain exactly one JSON document")
			}
			rootStarted = true
			rootComplete = true
			continue
		}

		frame := &frames[len(frames)-1]
		if frame.kind == '{' && frame.expectingKey {
			key, ok := token.(string)
			if !ok {
				return errors.New("request is not valid JSON")
			}
			if _, exists := frame.keys[key]; exists {
				return errors.New("request contains a duplicate object field")
			}
			frame.keys[key] = struct{}{}
			frame.currentKey = key
			frame.expectingKey = false
			continue
		}
		if err := countArrayValue(frame); err != nil {
			return err
		}
		finishObjectValue(frame)
	}

	if !rootComplete || len(frames) != 0 {
		return errors.New("request is not valid JSON")
	}
	return nil
}

func beginContainerValue(frames []jsonFrame) (bool, error) {
	if len(frames) == 0 {
		return false, nil
	}
	parent := &frames[len(frames)-1]
	if err := countArrayValue(parent); err != nil {
		return false, err
	}
	return len(frames) == 1 && parent.kind == '{' && !parent.expectingKey && parent.currentKey == "sources", nil
}

func countArrayValue(frame *jsonFrame) error {
	if frame.kind != '[' {
		return nil
	}
	frame.elementCount++
	if frame.sourcesArray && frame.elementCount > maxSourcesPerBatch {
		return errors.New("sources exceeds maximum element count")
	}
	return nil
}

func finishObjectValue(frame *jsonFrame) {
	if frame.kind == '{' {
		frame.expectingKey = true
		frame.currentKey = ""
	}
}

func hasLoneSurrogateEscape(data []byte) bool {
	inString := false
	for index := 0; index < len(data); index++ {
		switch data[index] {
		case '"':
			inString = !inString
		case '\\':
			if !inString || index+1 >= len(data) {
				continue
			}
			if data[index+1] != 'u' {
				index++
				continue
			}
			code, ok := decodeHexQuad(data, index+2)
			if !ok {
				continue
			}
			switch {
			case code >= 0xd800 && code <= 0xdbff:
				if index+11 >= len(data) || data[index+6] != '\\' || data[index+7] != 'u' {
					return true
				}
				low, lowOK := decodeHexQuad(data, index+8)
				if !lowOK || low < 0xdc00 || low > 0xdfff {
					return true
				}
				index += 11
			case code >= 0xdc00 && code <= 0xdfff:
				return true
			default:
				index += 5
			}
		}
	}
	return false
}

func decodeHexQuad(data []byte, start int) (uint16, bool) {
	if start+4 > len(data) {
		return 0, false
	}
	var value uint16
	for _, digit := range data[start : start+4] {
		value <<= 4
		switch {
		case digit >= '0' && digit <= '9':
			value += uint16(digit - '0')
		case digit >= 'a' && digit <= 'f':
			value += uint16(digit-'a') + 10
		case digit >= 'A' && digit <= 'F':
			value += uint16(digit-'A') + 10
		default:
			return 0, false
		}
	}
	return value, true
}

// EncodeResponse encodes one compact response without a BOM or trailing newline.
func EncodeResponse(response *Response) ([]byte, error) {
	if response == nil {
		return nil, errors.New("response is nil")
	}
	normalized := *response
	if normalized.Results == nil {
		normalized.Results = []Result{}
	} else {
		normalized.Results = append([]Result(nil), normalized.Results...)
	}
	for index := range normalized.Results {
		if normalized.Results[index].Events == nil {
			normalized.Results[index].Events = []Event{}
		}
	}
	return json.Marshal(normalized)
}
