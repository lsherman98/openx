package observability

import (
	"encoding/json"
	"regexp"
	"strings"
	"unicode"
)

const redacted = "[REDACTED]"

var (
	// Strip every query and fragment from URLs, not just known token keys.
	urlLike = regexp.MustCompile(`(?:https?://|/)[^\s<>"']+`)
	// Best-effort protection for common credentials accidentally placed in text.
	bearerToken          = regexp.MustCompile(`(?i)\bbearer\s+[^\s,;"']+`)
	credentialAssignment = regexp.MustCompile(`(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie)\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)`)
)

func sensitiveKey(key string) bool {
	key = strings.Map(func(value rune) rune {
		if unicode.IsLetter(value) || unicode.IsDigit(value) {
			return unicode.ToLower(value)
		}
		return -1
	}, key)
	for _, name := range []string{"password", "passwd", "secret", "token", "authorization", "cookie", "credential", "apikey"} {
		if strings.Contains(key, name) {
			return true
		}
	}
	switch key {
	case "body", "requestbody", "responsebody", "payload", "query", "querystring", "queryparams", "headers", "requestheaders", "responseheaders":
		return true
	default:
		return false
	}
}

func sanitizeString(value string) string {
	value = urlLike.ReplaceAllStringFunc(value, func(match string) string {
		if position := strings.IndexAny(match, "?#"); position >= 0 {
			return match[:position]
		}
		return match
	})
	value = bearerToken.ReplaceAllString(value, "Bearer "+redacted)
	return credentialAssignment.ReplaceAllString(value, "$1="+redacted)
}

func sanitizeMap(values map[string]any, depth int) map[string]any {
	result := make(map[string]any, len(values))
	for key, value := range values {
		if sensitiveKey(key) {
			result[key] = redacted
		} else {
			result[key] = sanitizeValue(value, depth+1)
		}
	}
	return result
}

func sanitizeValue(value any, depth int) any {
	if depth > 12 {
		return redacted
	}
	switch typed := value.(type) {
	case nil, bool, float64, float32, int, int32, int64, uint, uint32, uint64:
		return typed
	case string:
		return sanitizeString(typed)
	case error:
		return sanitizeString(typed.Error())
	case map[string]any:
		return sanitizeMap(typed, depth)
	case []any:
		result := make([]any, len(typed))
		for index, item := range typed {
			result[index] = sanitizeValue(item, depth+1)
		}
		return result
	default:
		// Typed maps, structs, JSONMap, and slog groups can also contain
		// credentials. Normalize them before traversing their keys.
		data, err := json.Marshal(value)
		if err != nil {
			return redacted
		}
		var normalized any
		if err := json.Unmarshal(data, &normalized); err != nil {
			return redacted
		}
		return sanitizeValue(normalized, depth+1)
	}
}
