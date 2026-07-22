// Package main emits frozen wire facts from certified shell syntax.
package main

import (
	"errors"
	"sort"
	"strconv"
	"strings"

	"mvdan.cc/sh/v3/expand"
	"mvdan.cc/sh/v3/syntax"
)

var errInvalidEmission = errors.New("invalid syntax facts")

type wordExpansionContext uint8

const (
	argvExpansion wordExpansionContext = iota
	assignmentExpansion
)

type orderedEvent struct {
	event           Event
	originalOrdinal int
}

func certifySource(source Source) (Result, error) {
	if firstNUL := strings.IndexByte(source.Source, 0); firstNUL >= 0 {
		return Result{
			ID: source.ID,
			Events: []Event{{
				Kind:      "refusal",
				Code:      "unsupported-construct",
				StartByte: firstNUL,
				EndByte:   firstNUL + 1,
			}},
			WorkUnits: 2,
		}, nil
	}
	statements, parseRefusal := parseStatements(source.Source)
	sites, walkRefusals, work := walk(statements, source.Source)
	events, err := emitCommandSites(sites, source.Source)
	if err != nil {
		return Result{}, err
	}
	combined := make([]orderedEvent, 0, len(events)+len(walkRefusals)+1)
	for index, event := range events {
		combined = append(combined, orderedEvent{event: event, originalOrdinal: index})
	}
	terminalSeen := false
	for index := range walkRefusals {
		refusal := &walkRefusals[index]
		scope := reasonScopes[refusal.code]
		if terminalSeen || scope != "terminal" && scope != "subtree-local" {
			return Result{}, errInvalidEmission
		}
		if err := validateRawSpan(refusal.startByte, refusal.endByte, len(source.Source)); err != nil {
			return Result{}, errInvalidEmission
		}
		combined = append(combined, orderedEvent{
			event: Event{
				Kind:      "refusal",
				Code:      refusal.code,
				StartByte: refusal.startByte,
				EndByte:   refusal.endByte,
			},
			originalOrdinal: len(combined),
		})
		terminalSeen = scope == "terminal"
	}
	if !terminalSeen && parseRefusal != nil {
		if reasonScopes[parseRefusal.code] != "terminal" {
			return Result{}, errInvalidEmission
		}
		if err := validateRawSpan(parseRefusal.startByte, parseRefusal.endByte, len(source.Source)); err != nil {
			return Result{}, err
		}
		combined = append(combined, orderedEvent{
			event: Event{
				Kind:      "refusal",
				Code:      parseRefusal.code,
				StartByte: parseRefusal.startByte,
				EndByte:   parseRefusal.endByte,
			},
			originalOrdinal: len(combined),
		})
	}
	sort.SliceStable(combined, func(left, right int) bool {
		x, y := combined[left], combined[right]
		if x.event.StartByte != y.event.StartByte {
			return x.event.StartByte < y.event.StartByte
		}
		xKind, yKind := eventKindOrdinal(x.event.Kind), eventKindOrdinal(y.event.Kind)
		if xKind != yKind {
			return xKind < yKind
		}
		return x.originalOrdinal < y.originalOrdinal
	})
	resultEvents := make([]Event, len(combined))
	for index, event := range combined {
		resultEvents[index] = event.event
	}
	if err := validateEventOrdering(resultEvents); err != nil {
		return Result{}, err
	}
	return Result{ID: source.ID, Events: resultEvents, WorkUnits: work + 1}, nil
}

func validateEventOrdering(events []Event) error {
	earliestLaterSiteStart := 0
	hasLaterSite := false
	for index := len(events) - 1; index >= 0; index-- {
		event := events[index]
		if event.Kind == "refusal" {
			scope := reasonScopes[event.Code]
			if scope == "terminal" && index != len(events)-1 {
				return errInvalidEmission
			}
			if scope == "subtree-local" && hasLaterSite && earliestLaterSiteStart < event.EndByte {
				return errInvalidEmission
			}
		}
		if event.Kind == "command_site" && (!hasLaterSite || event.StartByte < earliestLaterSiteStart) {
			earliestLaterSiteStart = event.StartByte
			hasLaterSite = true
		}
	}
	return nil
}

func eventKindOrdinal(kind string) int {
	if kind == "command_site" {
		return 0
	}
	return 1
}

func validateRawSpan(start, end, srcLen int) error {
	if start < 0 || start > end || end > srcLen {
		return errInvalidEmission
	}
	return nil
}

func emitCommandSites(sites []commandSite, src string) ([]Event, error) {
	events := make([]Event, 0, len(sites))
	for ordinal, site := range sites {
		event, err := emitCommandSite(site, ordinal, src)
		if err != nil {
			return nil, err
		}
		events = append(events, event)
	}
	return events, nil
}

func emitCommandSite(site commandSite, ordinal int, src string) (Event, error) {
	if site.call == nil || (len(site.call.Assigns) == 0 && len(site.call.Args) == 0) {
		return Event{}, errInvalidEmission
	}
	if len(site.assignments) > maxAssignmentsPerSite || len(site.argv) > maxArgvWordsPerSite {
		return Event{}, errInvalidEmission
	}
	start, end, err := validatedSpan(site.call, len(src))
	if err != nil {
		return Event{}, err
	}
	event := Event{
		Kind:        "command_site",
		Ordinal:     ordinal,
		StartByte:   start,
		EndByte:     end,
		Assignments: make([]Assignment, 0, len(site.assignments)),
		Argv:        make([]Word, 0, len(site.argv)),
	}
	for _, raw := range site.assignments {
		if raw == nil || raw.Name == nil || !validAssignmentName(raw.Name.Value) {
			return Event{}, errInvalidEmission
		}
		assignStart, assignEnd, err := validatedSpan(raw, len(src))
		if err != nil {
			return Event{}, err
		}
		event.Assignments = append(event.Assignments, Assignment{
			Name:       raw.Name.Value,
			ValueKnown: wordIsStaticLiteral(raw.Value, src),
			StartByte:  assignStart,
			EndByte:    assignEnd,
		})
	}
	for _, raw := range site.argv {
		if raw == nil || len(raw.Parts) == 0 {
			return Event{}, errInvalidEmission
		}
		wordStart, wordEnd, err := validatedSpan(raw, len(src))
		if err != nil {
			return Event{}, err
		}
		classification := classifyWordExpansion(raw, src)
		text, known := literalWordInContextClassified(raw, src, argvExpansion, classification)
		word := Word{Single: wordIsSingleClassified(raw, classification), StartByte: wordStart, EndByte: wordEnd}
		if known {
			word.Text = &text
			word.Single = true
		}
		event.Argv = append(event.Argv, word)
	}
	return event, nil
}

func validAssignmentName(name string) bool {
	if name == "" || !isAssignmentNameStart(name[0]) {
		return false
	}
	for index := 1; index < len(name); index++ {
		if !isAssignmentNameStart(name[index]) && (name[index] < '0' || name[index] > '9') {
			return false
		}
	}
	return true
}

func isAssignmentNameStart(char byte) bool {
	return char == '_' || char >= 'a' && char <= 'z' || char >= 'A' && char <= 'Z'
}

func validatedSpan(node syntax.Node, srcLen int) (start, end int, err error) {
	defer func() {
		if recover() != nil {
			start, end, err = 0, 0, errInvalidEmission
		}
	}()
	if node == nil {
		return 0, 0, errInvalidEmission
	}
	startPos, endPos := node.Pos(), node.End()
	if !startPos.IsValid() || !endPos.IsValid() {
		return 0, 0, errInvalidEmission
	}
	startOffset, endOffset := uint64(startPos.Offset()), uint64(endPos.Offset())
	if startOffset > endOffset || endOffset > uint64(srcLen) {
		return 0, 0, errInvalidEmission
	}
	return int(startOffset), int(endOffset), nil
}

func wordIsStaticLiteral(word *syntax.Word, src string) bool {
	if word == nil {
		return true
	}
	_, known := literalWordInContext(word, src, assignmentExpansion)
	return known
}

func literalWord(word *syntax.Word, src string) (string, bool) {
	return literalWordInContext(word, src, argvExpansion)
}

func literalWordInContext(word *syntax.Word, src string, context wordExpansionContext) (string, bool) {
	return literalWordInContextClassified(word, src, context, classifyWordExpansion(word, src))
}

func literalWordInContextClassified(word *syntax.Word, src string, context wordExpansionContext, classification wordExpansionClassification) (string, bool) {
	if word == nil {
		return "", true
	}
	if !classification.valid || wordHasActiveTilde(word, src, context) ||
		context == argvExpansion && (classification.activeGlob || classification.activeBrace) {
		return "", false
	}
	var value strings.Builder
	for _, part := range word.Parts {
		partValue, ok := literalWordPart(part, src, false, context)
		if !ok {
			return "", false
		}
		value.WriteString(partValue)
	}
	return value.String(), true
}

func literalWordPart(part syntax.WordPart, src string, quoted bool, context wordExpansionContext) (string, bool) {
	switch part := part.(type) {
	case *syntax.Lit:
		if part != nil {
			return decodedLiteral(part, src, quoted)
		}
	case *syntax.SglQuoted:
		if part != nil {
			if !part.Dollar {
				return part.Value, true
			}
			decoded := decodeANSIValue(part.Value)
			if !decoded.known {
				return "", false
			}
			return decoded.value, true
		}
	case *syntax.DblQuoted:
		if part == nil || part.Dollar {
			return "", false
		}
		var value strings.Builder
		for _, nested := range part.Parts {
			nestedValue, ok := literalWordPart(nested, src, true, context)
			if !ok {
				return "", false
			}
			value.WriteString(nestedValue)
		}
		return value.String(), true
	case *syntax.ExtGlob:
		if part != nil && context == assignmentExpansion {
			return literalAssignmentExtGlob(part, src)
		}
	}
	return "", false
}

func literalAssignmentExtGlob(extglob *syntax.ExtGlob, src string) (string, bool) {
	classification := classifyExtGlob(extglob, src)
	return classification.value, classification.known
}

type extGlobClassification struct {
	value     string
	known     bool
	execution bool
	unsafe    bool
}

type extGlobContextKind uint8

const (
	extGlobModernArithmetic extGlobContextKind = iota
	extGlobBracketArithmetic
	extGlobParameter
)

type extGlobParameterPhase uint8

const (
	extGlobParameterHead extGlobParameterPhase = iota
	extGlobParameterWord
	extGlobParameterArithmetic
)

type extGlobScanContext struct {
	kind           extGlobContextKind
	quote          int
	depth          int
	parameterPhase extGlobParameterPhase
	parameterName  bool
}

type extGlobScanContexts []extGlobScanContext

func (contexts *extGlobScanContexts) push(kind extGlobContextKind, quote int) {
	*contexts = append(*contexts, extGlobScanContext{kind: kind, quote: quote})
}

func (contexts *extGlobScanContexts) advance(raw string, index, quote int) int {
	if len(*contexts) == 0 {
		return index
	}
	context := &(*contexts)[len(*contexts)-1]
	if context.quote != quote {
		return index
	}
	char := raw[index]
	switch context.kind {
	case extGlobModernArithmetic:
		switch char {
		case '(':
			context.depth++
		case ')':
			if context.depth > 0 {
				context.depth--
				return index
			}
			next, nextIndex := logicalNextNonArithmeticWhitespace(raw, index+1)
			if next == ')' {
				*contexts = (*contexts)[:len(*contexts)-1]
				return nextIndex
			}
		}
	case extGlobBracketArithmetic:
		switch char {
		case '[':
			context.depth++
		case ']':
			if context.depth > 0 {
				context.depth--
			} else {
				*contexts = (*contexts)[:len(*contexts)-1]
			}
		}
	case extGlobParameter:
		if char == '}' {
			*contexts = (*contexts)[:len(*contexts)-1]
			return index
		}
		if context.parameterPhase != extGlobParameterHead {
			return index
		}
		if char == '[' && context.parameterName {
			contexts.push(extGlobBracketArithmetic, quote)
			return index
		}
		if char == ':' {
			next, _ := logicalNext(raw, index+1)
			if strings.ContainsRune("-=+?", rune(next)) {
				context.parameterPhase = extGlobParameterWord
			} else {
				context.parameterPhase = extGlobParameterArithmetic
			}
			return index
		}
		if context.parameterName && strings.ContainsRune("-=+?/%^,@", rune(char)) {
			context.parameterPhase = extGlobParameterWord
			return index
		}
		if char != '!' && char != '#' {
			context.parameterName = true
		}
	}
	return index
}

func (contexts extGlobScanContexts) arithmetic() bool {
	for index := len(contexts) - 1; index >= 0; index-- {
		context := contexts[index]
		switch context.kind {
		case extGlobModernArithmetic, extGlobBracketArithmetic:
			return true
		case extGlobParameter:
			return context.parameterPhase == extGlobParameterArithmetic
		}
	}
	return false
}

func classifyExtGlob(extglob *syntax.ExtGlob, src string) extGlobClassification {
	if extglob == nil {
		return extGlobClassification{}
	}
	start, end, err := validatedSpan(extglob, len(src))
	if err != nil {
		return extGlobClassification{}
	}
	raw := src[start:end]
	const (
		unquoted = iota
		singleQuoted
		doubleQuoted
	)
	quote := unquoted
	known := true
	var contexts extGlobScanContexts
	var value strings.Builder
	for index := 0; index < len(raw); index++ {
		char := raw[index]
		if char != '\\' && char != '\'' && char != '"' {
			if consumed := contexts.advance(raw, index, quote); consumed != index {
				index = consumed
				continue
			}
		}
		switch quote {
		case singleQuoted:
			if char == '\'' {
				quote = unquoted
			} else {
				value.WriteByte(char)
			}
			continue
		case doubleQuoted:
			if char == '"' {
				quote = unquoted
				continue
			}
			if char == '`' {
				return extGlobClassification{execution: true}
			}
			if char == '$' {
				next, nextIndex := logicalNext(raw, index+1)
				if next == '(' {
					after, afterIndex := logicalNext(raw, nextIndex+1)
					if after == '(' {
						known = false
						contexts.push(extGlobModernArithmetic, quote)
						index = afterIndex
						continue
					}
					return extGlobClassification{execution: true}
				}
				if next == '[' {
					known = false
					contexts.push(extGlobBracketArithmetic, quote)
					index = nextIndex
					continue
				}
				if next == '{' {
					known = false
					contexts.push(extGlobParameter, quote)
					index = nextIndex
					continue
				}
				if next != '"' && dollarExpansionByte(next) {
					known = false
					continue
				}
			}
			if char == '\\' && index+1 < len(raw) {
				next := raw[index+1]
				if next == '\n' {
					index++
					continue
				}
				if next == '$' || next == '`' || next == '"' || next == '\\' {
					value.WriteByte(next)
					index++
					continue
				}
			}
			value.WriteByte(char)
			continue
		}
		switch char {
		case '\'':
			quote = singleQuoted
		case '"':
			quote = doubleQuoted
		case '`':
			return extGlobClassification{execution: true}
		case '$':
			next, nextIndex := logicalNext(raw, index+1)
			if next == '\'' {
				decoded, close, ok, unsafe := decodeANSIQuoted(raw, nextIndex+1)
				if unsafe {
					return extGlobClassification{unsafe: true}
				}
				if !ok {
					return extGlobClassification{}
				}
				value.WriteString(decoded)
				index = close
			} else {
				if next == '(' {
					after, afterIndex := logicalNext(raw, nextIndex+1)
					if after == '(' {
						known = false
						contexts.push(extGlobModernArithmetic, quote)
						index = afterIndex
						continue
					}
					return extGlobClassification{execution: true}
				}
				if next == '[' {
					known = false
					contexts.push(extGlobBracketArithmetic, quote)
					index = nextIndex
					continue
				}
				if next == '{' {
					known = false
					contexts.push(extGlobParameter, quote)
					index = nextIndex
					continue
				}
				if dollarExpansionByte(next) {
					known = false
					continue
				}
				value.WriteByte(char)
			}
		case '\\':
			if index+1 >= len(raw) {
				return extGlobClassification{}
			}
			index++
			if raw[index] != '\n' {
				value.WriteByte(raw[index])
			}
		case '<', '>':
			next, _ := logicalNext(raw, index+1)
			if next == '(' && !contexts.arithmetic() {
				return extGlobClassification{execution: true}
			}
			value.WriteByte(char)
		default:
			value.WriteByte(char)
		}
	}
	if quote != unquoted {
		known = false
	}
	if !known {
		return extGlobClassification{}
	}
	return extGlobClassification{value: value.String(), known: true}
}

func logicalNext(raw string, index int) (byte, int) {
	for index < len(raw) {
		if raw[index] == '\\' && index+1 < len(raw) && raw[index+1] == '\n' {
			index += 2
			continue
		}
		return raw[index], index
	}
	return 0, index
}

func logicalNextNonArithmeticWhitespace(raw string, index int) (byte, int) {
	for index < len(raw) {
		if raw[index] == '\\' && index+1 < len(raw) && raw[index+1] == '\n' {
			index += 2
			continue
		}
		switch raw[index] {
		case ' ', '\t', '\n', '\r', '\v', '\f':
			index++
			continue
		}
		return raw[index], index
	}
	return 0, index
}

func dollarExpansionByte(next byte) bool {
	return next == '{' || next == '(' || next == '[' || next == '"' || next == '_' || next >= 'a' && next <= 'z' || next >= 'A' && next <= 'Z' || next >= '0' && next <= '9' || strings.ContainsRune("@*#?-$!", rune(next))
}

type ansiDecodeResult struct {
	value  string
	known  bool
	unsafe bool
}

func decodeANSIValue(raw string) ansiDecodeResult {
	value, _, err := expand.Format(nil, raw, nil)
	if err != nil {
		return ansiDecodeResult{}
	}
	if strings.IndexByte(value, 0) >= 0 {
		return ansiDecodeResult{unsafe: true}
	}
	return ansiDecodeResult{value: value, known: true}
}

func decodeANSIQuoted(raw string, start int) (string, int, bool, bool) {
	for close := start; close < len(raw); close++ {
		if raw[close] == '\\' {
			close++
			continue
		}
		if raw[close] != '\'' {
			continue
		}
		decoded := decodeANSIValue(raw[start:close])
		if decoded.unsafe {
			return "", close, false, true
		}
		if !decoded.known {
			return "", 0, false, false
		}
		return decoded.value, close, true, false
	}
	return "", 0, false, false
}

func decodedLiteral(literal *syntax.Lit, src string, doubleQuoted bool) (string, bool) {
	if literal == nil || !literal.Pos().IsValid() || !literal.End().IsValid() {
		return "", false
	}
	start, end := int(literal.Pos().Offset()), int(literal.End().Offset())
	if start < 0 || start > end || end > len(src) {
		return "", false
	}
	raw := src[start:end]
	var decoded strings.Builder
	for index := 0; index < len(raw); index++ {
		if raw[index] != '\\' || index+1 >= len(raw) {
			decoded.WriteByte(raw[index])
			continue
		}
		next := raw[index+1]
		if next == '\n' {
			index++
			continue
		}
		if !doubleQuoted || next == '$' || next == '`' || next == '"' || next == '\\' {
			decoded.WriteByte(next)
			index++
			continue
		}
		decoded.WriteByte(raw[index])
	}
	return decoded.String(), true
}

func wordIsSingle(word *syntax.Word, src string) bool {
	return wordIsSingleClassified(word, classifyWordExpansion(word, src))
}

func wordIsSingleClassified(word *syntax.Word, classification wordExpansionClassification) bool {
	if word == nil || len(word.Parts) == 0 || !classification.valid ||
		classification.activeGlob || classification.activeBrace {
		return false
	}
	for _, part := range word.Parts {
		switch part := part.(type) {
		case *syntax.Lit:
			if part == nil {
				return false
			}
		case *syntax.SglQuoted:
			if part == nil {
				return false
			}
		case *syntax.DblQuoted:
			if part == nil || !doubleQuotedIsSingle(part) {
				return false
			}
		case *syntax.ProcSubst:
			if part == nil {
				return false
			}
		case *syntax.ArithmExp, *syntax.ParamExp:
			return false
		default:
			return false
		}
	}
	return true
}

func wordHasActiveTilde(word *syntax.Word, src string, context wordExpansionContext) bool {
	eligible := true
	assignmentLike := context == assignmentExpansion
	prefixValid := true
	var prefix strings.Builder
	for _, part := range word.Parts {
		literal, ok := part.(*syntax.Lit)
		if !ok || literal == nil {
			eligible = false
			if !assignmentLike {
				prefixValid = false
			}
			continue
		}
		if !literal.Pos().IsValid() || !literal.End().IsValid() {
			return true
		}
		start, end := int(literal.Pos().Offset()), int(literal.End().Offset())
		if start < 0 || start > end || end > len(src) {
			return true
		}
		raw := src[start:end]
		for index := 0; index < len(raw); index++ {
			if raw[index] == '\\' {
				if index+1 < len(raw) && raw[index+1] == '\n' {
					index++
					continue
				}
				if index+1 < len(raw) {
					index++
				}
				eligible = false
				if !assignmentLike {
					prefixValid = false
				}
				continue
			}
			if raw[index] == '~' && eligible {
				return true
			}
			if context == argvExpansion && !assignmentLike {
				if raw[index] == '=' {
					assignmentLike = prefixValid && validAssignmentOperatorPrefix(prefix.String())
					eligible = assignmentLike
				} else {
					prefix.WriteByte(raw[index])
					eligible = false
				}
				continue
			}
			if raw[index] == ':' {
				eligible = assignmentLike
			} else {
				eligible = false
			}
		}
	}
	return false
}

func validAssignmentOperatorPrefix(prefix string) bool {
	if validAssignmentName(prefix) {
		return true
	}
	return len(prefix) > 1 && prefix[len(prefix)-1] == '+' && validAssignmentName(prefix[:len(prefix)-1])
}

func doubleQuotedIsSingle(quoted *syntax.DblQuoted) bool {
	if quoted == nil {
		return false
	}
	for _, part := range quoted.Parts {
		switch part := part.(type) {
		case *syntax.Lit:
			if part == nil {
				return false
			}
		case *syntax.ParamExp:
			if quotedParameterMayExpandToMany(part) {
				return false
			}
		case *syntax.CmdSubst, *syntax.ArithmExp, *syntax.ProcSubst:
			continue
		default:
			return false
		}
	}
	return true
}

func quotedParameterMayExpandToMany(parameter *syntax.ParamExp) bool {
	return quotedParameterMayExpandToManyAtDepth(parameter, 0)
}

func quotedParameterMayExpandToManyAtDepth(parameter *syntax.ParamExp, depth int) bool {
	if parameter == nil || parameter.Param == nil {
		return true
	}
	if depth > visitorDepthCap {
		return true
	}
	if parameter.Excl {
		if parameter.Names == syntax.NamesPrefix {
			return false
		}
		if index, ok := parameter.Index.(*syntax.Word); ok && index != nil && index.Lit() == "*" {
			return false
		}
		return true
	}
	if parameter.Length || parameter.Width || parameter.IsSet {
		return false
	}
	if parameter.Param.Value == "@" || parameter.Names == syntax.NamesPrefixWords {
		return true
	}
	index, ok := parameter.Index.(*syntax.Word)
	if ok && index != nil && index.Lit() == "@" {
		return true
	}
	if parameter.NestedParam != nil && quotedWordPartMayExpandToMany(parameter.NestedParam, depth+1) {
		return true
	}
	if ok && quotedWordMayExpandToMany(index, depth+1) {
		return true
	}
	if parameter.Repl != nil && (quotedWordMayExpandToMany(parameter.Repl.Orig, depth+1) || quotedWordMayExpandToMany(parameter.Repl.With, depth+1)) {
		return true
	}
	return parameter.Exp != nil && quotedWordMayExpandToMany(parameter.Exp.Word, depth+1)
}

func quotedWordMayExpandToMany(word *syntax.Word, depth int) bool {
	if word == nil {
		return false
	}
	if depth > visitorDepthCap {
		return true
	}
	for _, part := range word.Parts {
		if quotedWordPartMayExpandToMany(part, depth+1) {
			return true
		}
	}
	return false
}

func quotedWordPartMayExpandToMany(part syntax.WordPart, depth int) bool {
	if part == nil || syntaxNodeIsNil(part) || depth > visitorDepthCap {
		return true
	}
	switch part := part.(type) {
	case *syntax.ParamExp:
		return quotedParameterMayExpandToManyAtDepth(part, depth+1)
	case *syntax.DblQuoted:
		for _, nested := range part.Parts {
			if quotedWordPartMayExpandToMany(nested, depth+1) {
				return true
			}
		}
	}
	return false
}

type wordToken struct {
	value    byte
	unquoted bool
}

type wordExpansionClassification struct {
	valid                   bool
	activeGlob, activeBrace bool
	tokenCount, operations  int
}

type braceEndpointState uint8

const (
	braceEndpointValid braceEndpointState = 1 << iota
	braceEndpointInteger
	braceEndpointDigit
	braceEndpointNegative
	braceEndpointOverflow
	braceEndpointCharacter
	braceEndpointValue
)

type braceFrameFlags uint8

const (
	braceFrameComma braceFrameFlags = 1 << iota
	braceFrameInvalidSequence
)

type braceAnalysisFrame struct {
	magnitudes [3]uint64
	states     [3]braceEndpointState
	separators uint8
	flags      braceFrameFlags
}

func newBraceEndpointState() braceEndpointState {
	return braceEndpointValid | braceEndpointInteger
}

func newBraceAnalysisFrame() braceAnalysisFrame {
	frame := braceAnalysisFrame{}
	frame.states[0] = newBraceEndpointState()
	return frame
}

func braceIntegerLimit(negative bool) uint64 {
	if strconv.IntSize == 32 {
		if negative {
			return uint64(1) << 31
		}
		return (uint64(1) << 31) - 1
	}
	if negative {
		return uint64(1) << 63
	}
	return (uint64(1) << 63) - 1
}

func (frame *braceAnalysisFrame) appendValue(value byte) {
	if frame.separators > 2 {
		frame.flags |= braceFrameInvalidSequence
		return
	}
	index := int(frame.separators)
	state := frame.states[index]
	if state&braceEndpointValid == 0 {
		return
	}
	hasValue := state&braceEndpointValue != 0
	if !hasValue && isAssignmentNameStart(value) && value != '_' {
		state |= braceEndpointCharacter
	} else {
		state &^= braceEndpointCharacter
	}
	if state&braceEndpointInteger != 0 {
		switch {
		case !hasValue && (value == '+' || value == '-'):
			if value == '-' {
				state |= braceEndpointNegative
			}
		case value >= '0' && value <= '9':
			state |= braceEndpointDigit
			digit := uint64(value - '0')
			limit := braceIntegerLimit(state&braceEndpointNegative != 0)
			magnitude := frame.magnitudes[index]
			if magnitude > (limit-digit)/10 {
				state |= braceEndpointOverflow
			} else {
				frame.magnitudes[index] = magnitude*10 + digit
			}
		default:
			state &^= braceEndpointInteger
		}
	}
	state |= braceEndpointValue
	frame.states[index] = state
}

func (frame *braceAnalysisFrame) invalidateElement() {
	if frame.separators > 2 {
		frame.flags |= braceFrameInvalidSequence
		return
	}
	frame.states[frame.separators] &^= braceEndpointValid
}

func (frame *braceAnalysisFrame) addSeparator() {
	frame.separators++
	if frame.separators > 2 {
		frame.flags |= braceFrameInvalidSequence
		return
	}
	frame.states[frame.separators] = newBraceEndpointState()
}

func (frame braceAnalysisFrame) active() bool {
	if frame.flags&braceFrameComma != 0 {
		return true
	}
	if frame.flags&braceFrameInvalidSequence != 0 || frame.separators < 1 || frame.separators > 2 {
		return false
	}
	firstChar, firstOK := braceAnalysisEndpoint(frame.states[0])
	secondChar, secondOK := braceAnalysisEndpoint(frame.states[1])
	if !firstOK || !secondOK || firstChar != secondChar {
		return false
	}
	return frame.separators != 2 || braceAnalysisInteger(frame.states[2])
}

func braceAnalysisEndpoint(state braceEndpointState) (isChar bool, ok bool) {
	if braceAnalysisInteger(state) {
		return false, true
	}
	return true, state&braceEndpointValid != 0 && state&braceEndpointCharacter != 0
}

func braceAnalysisInteger(state braceEndpointState) bool {
	required := braceEndpointValid | braceEndpointInteger | braceEndpointDigit
	return state&required == required && state&braceEndpointOverflow == 0
}

const (
	braceStackInitialBlock = 8
	braceStackMaxBlock     = 4096
)

type braceAnalysisStack struct {
	blocks [][]braceAnalysisFrame
	used   int
	length int
}

func (stack *braceAnalysisStack) len() int {
	return stack.length
}

func (stack *braceAnalysisStack) top() *braceAnalysisFrame {
	if stack.length == 0 {
		return nil
	}
	return &stack.blocks[len(stack.blocks)-1][stack.used-1]
}

func (stack *braceAnalysisStack) push(frame braceAnalysisFrame) {
	if len(stack.blocks) == 0 || stack.used == len(stack.blocks[len(stack.blocks)-1]) {
		blockSize := braceStackInitialBlock
		if len(stack.blocks) > 0 {
			blockSize = min(len(stack.blocks[len(stack.blocks)-1])*2, braceStackMaxBlock)
		}
		stack.blocks = append(stack.blocks, make([]braceAnalysisFrame, blockSize))
		stack.used = 0
	}
	stack.blocks[len(stack.blocks)-1][stack.used] = frame
	stack.used++
	stack.length++
}

func (stack *braceAnalysisStack) pop() (braceAnalysisFrame, bool) {
	if stack.length == 0 {
		return braceAnalysisFrame{}, false
	}
	lastBlock := len(stack.blocks) - 1
	stack.used--
	frame := stack.blocks[lastBlock][stack.used]
	stack.blocks[lastBlock][stack.used] = braceAnalysisFrame{}
	stack.length--
	if stack.used == 0 {
		stack.blocks[lastBlock] = nil
		stack.blocks = stack.blocks[:lastBlock]
		if lastBlock > 0 {
			stack.used = len(stack.blocks[lastBlock-1])
		}
	}
	return frame, true
}

func classifyWordExpansion(word *syntax.Word, src string) wordExpansionClassification {
	if word == nil {
		return wordExpansionClassification{valid: true}
	}
	tokens, ok := wordTokens(word, src)
	classification := wordExpansionClassification{valid: ok, tokenCount: len(tokens)}
	if !ok {
		return classification
	}
	bracketOpen := false
	var braceFrames braceAnalysisStack
	for index := 0; index < len(tokens); index++ {
		token := tokens[index]
		classification.operations++
		if token.unquoted {
			switch token.value {
			case '*', '?':
				classification.activeGlob = true
				return classification
			case '[':
				bracketOpen = true
			case ']':
				if bracketOpen {
					classification.activeGlob = true
					return classification
				}
			}
		}
		if !token.unquoted {
			if frame := braceFrames.top(); frame != nil {
				frame.invalidateElement()
			}
			continue
		}
		switch token.value {
		case '{':
			braceFrames.push(newBraceAnalysisFrame())
			classification.operations++
		case '}':
			frame, ok := braceFrames.pop()
			if !ok {
				continue
			}
			classification.operations++
			if frame.active() {
				classification.activeBrace = true
				return classification
			}
			if parent := braceFrames.top(); parent != nil {
				parent.invalidateElement()
			}
		case ',':
			if frame := braceFrames.top(); frame != nil {
				frame.flags |= braceFrameComma
			}
		case '.':
			frame := braceFrames.top()
			if frame == nil {
				continue
			}
			if index+1 < len(tokens) && tokens[index+1].unquoted && tokens[index+1].value == '.' {
				frame.addSeparator()
				if index+2 < len(tokens) && tokens[index+2].unquoted && tokens[index+2].value == '.' {
					frame.flags |= braceFrameInvalidSequence
				}
				index++
				classification.operations++
			} else {
				frame.appendValue(token.value)
			}
		default:
			if frame := braceFrames.top(); frame != nil {
				frame.appendValue(token.value)
			}
		}
	}
	return classification
}

func wordTokens(word *syntax.Word, src string) ([]wordToken, bool) {
	tokens := make([]wordToken, 0, len(word.Parts))
	for _, part := range word.Parts {
		if !appendWordPartTokens(&tokens, part, src, false) {
			return nil, false
		}
	}
	return tokens, true
}

func appendWordPartTokens(tokens *[]wordToken, part syntax.WordPart, src string, quoted bool) bool {
	switch part := part.(type) {
	case *syntax.Lit:
		if part == nil {
			return false
		}
		literal := part
		if !literal.Pos().IsValid() || !literal.End().IsValid() {
			return false
		}
		start, end := int(literal.Pos().Offset()), int(literal.End().Offset())
		if start < 0 || start > end || end > len(src) {
			return false
		}
		raw := src[start:end]
		for index := 0; index < len(raw); index++ {
			if raw[index] == '\\' && index+1 < len(raw) {
				index++
				if raw[index] == '\n' {
					continue
				}
				*tokens = append(*tokens, wordToken{value: raw[index]})
				continue
			}
			*tokens = append(*tokens, wordToken{value: raw[index], unquoted: !quoted})
		}
	case *syntax.SglQuoted:
		if part == nil {
			return false
		}
		for index := 0; index < len(part.Value); index++ {
			*tokens = append(*tokens, wordToken{value: part.Value[index]})
		}
	case *syntax.DblQuoted:
		if part == nil {
			return false
		}
		for _, nested := range part.Parts {
			if !appendWordPartTokens(tokens, nested, src, true) {
				return false
			}
		}
	default:
		if part == nil || syntaxNodeIsNil(part) {
			return false
		}
		*tokens = append(*tokens, wordToken{value: 'x'})
	}
	return true
}
