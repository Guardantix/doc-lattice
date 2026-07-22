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
	statements, parseRefusal := parseStatements(source.Source)
	sites, walkRefusals, work := walk(statements, source.Source)
	events, err := emitCommandSites(sites, source.Source)
	if err != nil {
		return Result{}, err
	}
	combined := make([]orderedEvent, 0, len(events)+1)
	for index, event := range events {
		combined = append(combined, orderedEvent{event: event, originalOrdinal: index})
	}
	if len(walkRefusals) > 1 {
		return Result{}, errInvalidEmission
	}
	var terminal *rawRefusal
	if len(walkRefusals) == 1 {
		terminal = &walkRefusals[0]
	} else {
		terminal = parseRefusal
	}
	if terminal != nil {
		if err := validateRawSpan(terminal.startByte, terminal.endByte, len(source.Source)); err != nil {
			return Result{}, err
		}
		combined = append(combined, orderedEvent{
			event: Event{
				Kind:      "refusal",
				Code:      terminal.code,
				StartByte: terminal.startByte,
				EndByte:   terminal.endByte,
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
		if event.event.Kind == "refusal" && index != len(combined)-1 {
			return Result{}, errInvalidEmission
		}
	}
	return Result{ID: source.ID, Events: resultEvents, WorkUnits: work + 1}, nil
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
		text, known := literalWord(raw, src)
		word := Word{Single: wordIsSingle(raw, src), StartByte: wordStart, EndByte: wordEnd}
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
	if word == nil {
		return "", true
	}
	if wordHasActiveTilde(word, src, context) || context == argvExpansion && (wordHasActiveGlob(word, src) || wordHasActiveBrace(word, src)) {
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
			value, _, err := expand.Format(nil, part.Value, nil)
			if err != nil {
				return "", false
			}
			value, _, _ = strings.Cut(value, "\x00")
			return value, true
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
				decoded, close, ok := decodeANSIQuoted(raw, nextIndex+1)
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

func decodeANSIQuoted(raw string, start int) (string, int, bool) {
	for close := start; close < len(raw); close++ {
		if raw[close] == '\\' {
			close++
			continue
		}
		if raw[close] != '\'' {
			continue
		}
		value, _, err := expand.Format(nil, raw[start:close], nil)
		if err != nil {
			return "", 0, false
		}
		value, _, _ = strings.Cut(value, "\x00")
		return value, close, true
	}
	return "", 0, false
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
	if word == nil || len(word.Parts) == 0 || wordHasActiveGlob(word, src) || wordHasActiveBrace(word, src) {
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
		case *syntax.ArithmExp:
			if part == nil {
				return false
			}
		case *syntax.ParamExp:
			if !unquotedScalarParameterIsSingle(part) {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func unquotedScalarParameterIsSingle(parameter *syntax.ParamExp) bool {
	if parameter == nil || !parameter.Dollar.IsValid() || parameter.Flags != nil ||
		parameter.Excl || parameter.Width || parameter.IsSet || parameter.Param == nil ||
		parameter.NestedParam != nil || len(parameter.Modifiers) > 0 || parameter.Slice != nil ||
		parameter.Repl != nil || parameter.Names != 0 || parameter.Exp != nil {
		return false
	}
	if !parameter.Param.Pos().IsValid() || !parameter.Param.End().IsValid() ||
		parameter.Param.Pos().Offset() > parameter.Param.End().Offset() {
		return false
	}
	if parameter.Short {
		return !parameter.Rbrace.IsValid() && !parameter.Length && parameter.Param.Value == "#" && parameter.Index == nil
	}
	if !parameter.Rbrace.IsValid() || !parameter.Length || !validBashParameterToken(parameter.Param.Value) {
		return false
	}
	if parameter.Index == nil {
		return true
	}
	if !syntax.ValidName(parameter.Param.Value) {
		return false
	}
	index, ok := parameter.Index.(*syntax.Word)
	if !ok || index == nil || syntaxNodeIsNil(index) || len(index.Parts) == 0 {
		return false
	}
	for _, part := range index.Parts {
		if part == nil || syntaxNodeIsNil(part) {
			return false
		}
		start, end := part.Pos(), part.End()
		if !start.IsValid() || !end.IsValid() || start.Offset() > end.Offset() {
			return false
		}
	}
	return true
}

func validBashParameterToken(value string) bool {
	if syntax.ValidName(value) {
		return true
	}
	if value == "" {
		return false
	}
	digits := true
	for index := 0; index < len(value); index++ {
		if value[index] < '0' || value[index] > '9' {
			digits = false
			break
		}
	}
	return digits || len(value) == 1 && strings.ContainsRune("@*#$?!-", rune(value[0]))
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

func wordHasActiveGlob(word *syntax.Word, src string) bool {
	tokens, ok := wordTokens(word, src)
	if !ok {
		return true
	}
	for index, token := range tokens {
		if !token.unquoted {
			continue
		}
		switch token.value {
		case '*', '?':
			return true
		case '[':
			for close := index + 1; close < len(tokens); close++ {
				if tokens[close].unquoted && tokens[close].value == ']' {
					return true
				}
			}
		}
	}
	return false
}

func wordHasActiveBrace(word *syntax.Word, src string) bool {
	tokens, ok := wordTokens(word, src)
	if !ok {
		return true
	}
	open := make([]int, 0, 2)
	for index, token := range tokens {
		if !token.unquoted {
			continue
		}
		switch token.value {
		case '{':
			open = append(open, index)
		case '}':
			if len(open) == 0 {
				continue
			}
			start := open[len(open)-1]
			open = open[:len(open)-1]
			if braceBodyIsActive(tokens[start+1 : index]) {
				return true
			}
		}
	}
	return false
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

func braceBodyIsActive(body []wordToken) bool {
	depth := 0
	separators := make([]int, 0, 2)
	for index, token := range body {
		if !token.unquoted {
			continue
		}
		switch token.value {
		case '{':
			depth++
		case '}':
			if depth > 0 {
				depth--
			}
		case ',':
			if depth == 0 {
				return true
			}
		case '.':
			if depth == 0 && index+1 < len(body) && body[index+1].unquoted && body[index+1].value == '.' {
				separators = append(separators, index)
			}
		}
	}
	if len(separators) < 1 || len(separators) > 2 {
		return false
	}
	for index, separator := range separators {
		if separator < 0 || separator+1 >= len(body) || index > 0 && separator < separators[index-1]+2 {
			return false
		}
	}
	elements := [][]wordToken{body[:separators[0]]}
	for index, separator := range separators {
		end := len(body)
		if index+1 < len(separators) {
			end = separators[index+1]
		}
		elements = append(elements, body[separator+2:end])
	}
	firstChar, firstOK := braceSequenceEndpoint(elements[0])
	secondChar, secondOK := braceSequenceEndpoint(elements[1])
	if !firstOK || !secondOK || firstChar != secondChar {
		return false
	}
	if len(elements) == 3 {
		value, ok := unquotedBraceElement(elements[2])
		if !ok {
			return false
		}
		_, err := strconv.Atoi(value)
		return err == nil
	}
	return true
}

func braceSequenceEndpoint(raw []wordToken) (isChar bool, ok bool) {
	value, ok := unquotedBraceElement(raw)
	if !ok {
		return false, false
	}
	if _, err := strconv.Atoi(value); err == nil {
		return false, true
	}
	return true, len(value) == 1 && isAssignmentNameStart(value[0]) && value[0] != '_'
}

func unquotedBraceElement(raw []wordToken) (string, bool) {
	var value strings.Builder
	for _, token := range raw {
		if !token.unquoted {
			return "", false
		}
		value.WriteByte(token.value)
	}
	return value.String(), true
}
